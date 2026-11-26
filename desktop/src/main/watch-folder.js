"use strict";

const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

const SUPPORTED_EXTS = new Set([".csv", ".tsv", ".ofx", ".qfx", ".qif", ".json", ".pdf"]);
const IGNORED_SUFFIXES = [".crdownload", ".part", ".tmp", ".download", ".partial"];
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const DEBOUNCE_MS = 1200;

/**
 * Optional, 100% local Auto-Import Watch Folder for zero-3rd-party statement syncing
 * (e.g., Navy Federal Credit Union CSV/PDF downloads, Plaid JSON/CSV exports, OFX/QIF).
 *
 * Disabled by default. When enabled by the user, watches a single chosen local folder,
 * uploads newly completed statement files to the local loopback ChronoX API, and moves
 * successfully processed files into an `Imported/` archive subfolder (never deletes files).
 */
class WatchFolderController {
  constructor({ getBackendUrl, getCookieHeader, getCsrfToken, onImported, onLog }) {
    this.getBackendUrl = getBackendUrl;
    this.getCookieHeader = getCookieHeader;
    this.getCsrfToken = getCsrfToken;
    this.onImported = onImported;
    this.onLog = onLog || (() => {});

    this.enabled = false;
    this.folderPath = "";
    this.institution = "auto";
    this.skipPending = true;
    this.syncBalance = false;

    this._watcher = null;
    this._timers = new Map();
    this._processing = new Set();
    this._activeScanPromise = null;
    this._importedFilesCount = 0;
    this._lastImportSummary = null;
    this._lastError = null;
  }

  configure({ enabled, folderPath, institution, skipPending, syncBalance }) {
    this.enabled = Boolean(enabled);
    this.folderPath = typeof folderPath === "string" ? folderPath.trim() : "";
    this.institution = ["auto", "generic", "navy_federal", "plaid"].includes(institution)
      ? institution
      : "auto";
    this.skipPending = typeof skipPending === "boolean" ? skipPending : true;
    this.syncBalance = Boolean(syncBalance);

    this.stop();
    this._lastError = null;

    if (!this.enabled || !this.folderPath) {
      return this.getStatus();
    }

    try {
      if (!fs.existsSync(this.folderPath) || !fs.statSync(this.folderPath).isDirectory()) {
        this._lastError = "Selected watch folder does not exist.";
        return this.getStatus();
      }
      this._watcher = fs.watch(this.folderPath, { persistent: false }, (_eventType, filename) => {
        if (!filename || typeof filename !== "string") return;
        this._scheduleFile(filename);
      });
      this._watcher.on("error", (err) => {
        this._lastError = `Watch folder error: ${err.message}`;
        this.onLog("warn", this._lastError);
      });
      this.onLog("info", `Auto-Import Watch Folder active: ${this.folderPath} (${this.institution})`);
      // Perform an initial quiet scan of any statements already waiting in the folder.
      this.scanNow().catch(() => {});
    } catch (err) {
      this._lastError = `Could not watch folder: ${err.message}`;
      this.onLog("warn", this._lastError);
    }

    return this.getStatus();
  }

  stop() {
    if (this._watcher) {
      try {
        this._watcher.close();
      } catch {
        // Ignore close races.
      }
      this._watcher = null;
    }
    for (const timer of this._timers.values()) {
      clearTimeout(timer);
    }
    this._timers.clear();
  }

  getStatus() {
    return {
      enabled: this.enabled,
      watching: Boolean(this._watcher),
      folderPath: this.folderPath,
      institution: this.institution,
      skipPending: this.skipPending,
      syncBalance: this.syncBalance,
      importedFilesCount: this._importedFilesCount,
      lastImportSummary: this._lastImportSummary,
      lastError: this._lastError,
    };
  }

  _isCandidateFilename(name) {
    if (!name || name.startsWith(".") || name.startsWith("~")) return false;
    const low = name.toLowerCase();
    for (const suffix of IGNORED_SUFFIXES) {
      if (low.endsWith(suffix)) return false;
    }
    const ext = path.extname(low);
    return SUPPORTED_EXTS.has(ext);
  }

  _scheduleFile(filename) {
    if (!this._isCandidateFilename(filename)) return;
    if (this._timers.has(filename)) {
      clearTimeout(this._timers.get(filename));
    }
    const timer = setTimeout(() => {
      this._timers.delete(filename);
      this._processFile(path.join(this.folderPath, filename)).catch(() => {});
    }, DEBOUNCE_MS);
    timer.unref();
    this._timers.set(filename, timer);
  }

  async scanNow() {
    if (this._activeScanPromise) {
      return this._activeScanPromise;
    }
    this._activeScanPromise = this._doScan().finally(() => {
      this._activeScanPromise = null;
    });
    return this._activeScanPromise;
  }

  async _doScan() {
    if (!this.folderPath || !fs.existsSync(this.folderPath)) {
      return { ok: false, processed: 0, error: "Watch folder is not configured." };
    }
    let entries = [];
    try {
      entries = fs.readdirSync(this.folderPath);
    } catch (err) {
      this._lastError = `Could not read watch folder: ${err.message}`;
      return { ok: false, processed: 0, error: this._lastError };
    }

    let processed = 0;
    let totalImported = 0;
    let totalDuplicates = 0;
    let locked = false;

    for (const name of entries) {
      if (!this._isCandidateFilename(name)) continue;
      const fullPath = path.join(this.folderPath, name);
      const res = await this._processFile(fullPath);
      if (res && res.locked) {
        locked = true;
        break;
      }
      if (res && res.ok) {
        processed += 1;
        totalImported += res.importedCount || 0;
        totalDuplicates += res.duplicateCount || 0;
      }
    }

    return {
      ok: !locked,
      locked,
      processed,
      importedCount: totalImported,
      duplicateCount: totalDuplicates,
      status: this.getStatus(),
    };
  }

  async _processFile(filePath) {
    if (this._processing.has(filePath)) return null;
    if (!fs.existsSync(filePath)) return null;

    let stat;
    try {
      stat = fs.statSync(filePath);
    } catch {
      return null;
    }
    if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_FILE_BYTES) {
      return null;
    }

    const baseUrl = this.getBackendUrl();
    if (!baseUrl) return null;

    this._processing.add(filePath);
    try {
      const filename = path.basename(filePath);
      const fileBuffer = fs.readFileSync(filePath);
      const cookieHeader = this.getCookieHeader ? await this.getCookieHeader(baseUrl) : "";
      const csrfToken = this.getCsrfToken ? await this.getCsrfToken() : "";

      const resp = await this._uploadMultipart(baseUrl, {
        filename,
        fileBuffer,
        cookieHeader,
        csrfToken,
      });

      if (resp.status === 401 || resp.status === 423) {
        this._lastError = "Sync paused: vault locked — unlock ChronoX to finish auto-importing statements.";
        return { ok: false, locked: true };
      }

      if (resp.status < 200 || resp.status >= 300 || !resp.data) {
        const msg = (resp.data && resp.data.detail) || `Import failed (HTTP ${resp.status})`;
        this._lastError = `${filename}: ${msg}`;
        return { ok: false, error: this._lastError };
      }

      const importedCount = Number(resp.data.imported_count || 0);
      const duplicateCount = Number(resp.data.duplicate_count || 0);
      const pendingSkipped = Number(resp.data.pending_skipped || 0);
      const totalParsed =
        resp.data.total_parsed != null
          ? Number(resp.data.total_parsed)
          : importedCount + duplicateCount;
      const warnings = Array.isArray(resp.data.warnings) ? resp.data.warnings : [];

      if (
        resp.data.parse_failed === true ||
        (totalParsed === 0 && importedCount === 0 && duplicateCount === 0 && pendingSkipped === 0)
      ) {
        const warnMsg =
          warnings[0] ||
          "Statement contained zero readable transaction rows; file left in watch folder.";
        this._lastError = `${filename}: ${warnMsg}`;
        this.onLog("warn", this._lastError);
        return { ok: false, error: this._lastError };
      }

      // Archive the processed file cleanly into `<watchFolder>/Imported/`
      const importedDir = path.join(this.folderPath, "Imported");
      fs.mkdirSync(importedDir, { recursive: true });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      const destPath = path.join(importedDir, `${stamp}_${filename}`);
      try {
        fs.renameSync(filePath, destPath);
      } catch {
        fs.copyFileSync(filePath, destPath);
        fs.unlinkSync(filePath);
      }

      const detectedInstitution = String(resp.data.detected_institution || "generic");
      const formatDetected = String(resp.data.format_detected || "statement").toUpperCase();

      this._importedFilesCount += 1;
      this._lastError = null;
      this._lastImportSummary = {
        filename,
        archivedAs: path.basename(destPath),
        importedCount,
        duplicateCount,
        detectedInstitution,
        formatDetected,
        at: new Date().toISOString(),
      };

      if (this.onImported) {
        this.onImported(this._lastImportSummary);
      }
      return { ok: true, importedCount, duplicateCount };
    } catch (err) {
      this._lastError = err && err.message ? err.message : "Auto-import failed";
      return { ok: false, error: this._lastError };
    } finally {
      this._processing.delete(filePath);
    }
  }

  _uploadMultipart(baseUrl, { filename, fileBuffer, cookieHeader, csrfToken }) {
    return new Promise((resolve, reject) => {
      const target = new URL("/api/v1/import/statement", baseUrl);
      const boundary = `----ChronoXDrop${crypto.randomBytes(12).toString("hex")}`;

      const parts = [];
      const addField = (name, value) => {
        parts.push(
          Buffer.from(
            `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${String(value)}\r\n`,
            "utf8"
          )
        );
      };

      addField("institution", this.institution);
      addField("skip_pending", this.skipPending ? "true" : "false");
      addField("detect_transfers", "true");
      addField("sync_balance", this.syncBalance ? "true" : "false");

      const safeName = filename.replace(/["\r\n]/g, "_");
      const contentType = safeName.toLowerCase().endsWith(".pdf")
        ? "application/pdf"
        : "application/octet-stream";

      parts.push(
        Buffer.from(
          `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${safeName}"\r\nContent-Type: ${contentType}\r\n\r\n`,
          "utf8"
        )
      );
      parts.push(fileBuffer);
      parts.push(Buffer.from(`\r\n--${boundary}--\r\n`, "utf8"));

      const body = Buffer.concat(parts);
      const headers = {
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
        "Content-Length": body.length,
        Origin: target.origin,
      };
      if (cookieHeader) {
        headers.Cookie = cookieHeader;
      }
      if (csrfToken) {
        headers["X-ChronoX-CSRF"] = csrfToken;
      }

      const req = http.request(
        {
          hostname: target.hostname,
          port: target.port,
          path: target.pathname,
          method: "POST",
          headers,
          timeout: 15000,
        },
        (res) => {
          let raw = "";
          res.setEncoding("utf8");
          res.on("data", (chunk) => {
            raw += chunk;
          });
          res.on("end", () => {
            let parsed = null;
            try {
              parsed = raw ? JSON.parse(raw) : null;
            } catch {
              parsed = null;
            }
            resolve({ status: res.statusCode || 0, data: parsed });
          });
        }
      );

      req.on("timeout", () => req.destroy(new Error("Auto-import request timed out")));
      req.on("error", reject);
      req.write(body);
      req.end();
    });
  }
}

module.exports = { WatchFolderController, SUPPORTED_EXTS };
