"use strict";

const { EventEmitter } = require("events");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const MAX_LOG_LINES = 250;
const HEALTH_POLL_INTERVAL_MS = 200;
const HEALTH_REQUEST_TIMEOUT_MS = 1500;
const STARTUP_TIMEOUT_MS = 30000;
const SHUTDOWN_GRACE_MS = 2500;

/**
 * Scrub accidental secrets, API keys, bearer tokens, and session cookies
 * before storing any line in the diagnostics ring buffer.
 */
function scrubSecrets(line) {
  let out = String(line ?? "");
  // Google Gemini API keys
  out = out.replace(/AIza[0-9A-Za-z_-]{16,}/g, "AIza[REDACTED]");
  // OpenAI / OpenCode / sk- style tokens
  out = out.replace(/\bsk-[0-9A-Za-z_-]{12,}/g, "sk-[REDACTED]");
  out = out.replace(/\bopencode[_-][0-9A-Za-z_-]{10,}/gi, "opencode_[REDACTED]");
  // Authorization Bearer headers
  out = out.replace(/(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, "$1[REDACTED]");
  // Signed session cookies
  out = out.replace(/(chronox_session=)[^\s;,"']+/gi, "$1[REDACTED]");
  // Generic key/token/secret/password assignments
  out = out.replace(
    /\b((?:gemini|opencode|openai|codex|plaid|schwab|coinbase|robinhood|imap|polygon|api|auth|access|refresh|session|recovery|csrf)[_-]?(?:api[_-]?)?(?:key|token|secret)|password|passcode)\s*([:=])\s*["']?[^\s"',;}{]+["']?/gi,
    "$1$2[REDACTED]"
  );
  return out;
}

/**
 * Allocate an available ephemeral TCP port bound strictly to 127.0.0.1.
 */
function findFreePort(host = "127.0.0.1") {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, host, () => {
      const addr = server.address();
      const port = addr && typeof addr === "object" ? addr.port : 0;
      server.close((err) => {
        if (err) reject(err);
        else if (!port) reject(new Error("Failed to allocate an ephemeral localhost port."));
        else resolve(port);
      });
    });
  });
}

/**
 * Probe GET http://127.0.0.1:<port>/api/health with a tight timeout.
 */
function probeHealth(host, port) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        hostname: host,
        port,
        path: "/api/health",
        timeout: HEALTH_REQUEST_TIMEOUT_MS,
        headers: { Accept: "application/json" },
      },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (raw.length < 32768) raw += chunk;
        });
        res.on("end", () => {
          if (res.statusCode !== 200) {
            resolve({ ok: false, statusCode: res.statusCode });
            return;
          }
          try {
            const data = JSON.parse(raw);
            resolve({
              ok: Boolean(data && (data.status === "ok" || data.db_ok === true)),
              data,
            });
          } catch {
            resolve({ ok: false, error: "Invalid health JSON" });
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "timeout" });
    });
    req.on("error", () => {
      resolve({ ok: false, error: "connect_error" });
    });
  });
}

function fileExistsExecutable(candidate) {
  try {
    if (!candidate || !fs.existsSync(candidate)) return false;
    const stat = fs.statSync(candidate);
    return stat.isFile();
  } catch {
    return false;
  }
}

class BackendManager extends EventEmitter {
  constructor(options) {
    super();
    this.app = options.app;
    this.host = "127.0.0.1";
    this.port = null;
    this.dbPath = options.dbPath;
    this.repoRoot = options.repoRoot || path.resolve(__dirname, "..", "..", "..");
    this.desktopRoot = options.desktopRoot || path.resolve(__dirname, "..", "..");

    this.proc = null;
    this.backendMode = "uninitialized";
    this.logs = [];
    this.partialLines = { stdout: "", stderr: "" };
    this.state = {
      stage: "starting",
      label: "Starting local vault",
      detail: "Preparing ChronoX engine...",
      progress: 5,
      error: null,
    };
    this._startingPromise = null;
    this._stoppingPromise = null;
    this._intentionalStop = false;
  }

  get baseUrl() {
    return this.port ? `http://${this.host}:${this.port}` : null;
  }

  get pid() {
    return this.proc && !this.proc.killed ? this.proc.pid : null;
  }

  getLogs() {
    return {
      lines: this.logs.slice(),
      backendMode: this.backendMode,
      backendUrl: this.baseUrl || "",
      pid: this.pid,
    };
  }

  getState() {
    return { ...this.state };
  }

  _pushLog(stream, text) {
    const clean = scrubSecrets(text).replace(/\r+$/, "");
    if (!clean.trim()) return;
    const entry = {
      ts: new Date().toISOString(),
      stream,
      text: clean.length > 2000 ? `${clean.slice(0, 2000)}…` : clean,
    };
    this.logs.push(entry);
    if (this.logs.length > MAX_LOG_LINES) {
      this.logs.splice(0, this.logs.length - MAX_LOG_LINES);
    }
  }

  _ingestStreamChunk(stream, chunk) {
    const combined = this.partialLines[stream] + chunk.toString("utf8");
    const lines = combined.split(/\r?\n/);
    this.partialLines[stream] = lines.pop() || "";
    for (const line of lines) {
      this._pushLog(stream, line);
    }
  }

  _emitStatus(partial) {
    this.state = { ...this.state, ...partial };
    this.emit("status", { ...this.state });
  }

  /**
   * Resolve how to launch the ChronoX Finance Python backend in priority order:
   * (a) Packaged standalone PyInstaller sidecar in resources/backend/
   * (b) Repo virtualenv (.venv/bin/python or .venv/Scripts/python.exe)
   * (c) System python3 / python with chronox_finance importable
   */
  resolveBackendCommand(port) {
    const exeName = process.platform === "win32" ? "chronox-backend.exe" : "chronox-backend";
    const sidecarCandidates = [];

    if (process.resourcesPath) {
      // One-file and one-dir PyInstaller layouts under packaged resources/backend/
      sidecarCandidates.push(path.join(process.resourcesPath, "backend", exeName));
      sidecarCandidates.push(path.join(process.resourcesPath, "backend", "chronox-backend", exeName));
    }
    // Local pre-built sidecar inside desktop/build/backend/ (when testing locally)
    sidecarCandidates.push(path.join(this.desktopRoot, "build", "backend", exeName));
    sidecarCandidates.push(path.join(this.desktopRoot, "build", "backend", "chronox-backend", exeName));

    for (const sidecarPath of sidecarCandidates) {
      if (fileExistsExecutable(sidecarPath)) {
        const args = ["--host", this.host, "--port", String(port), "--db", this.dbPath];
        return {
          mode: "packaged-sidecar",
          command: sidecarPath,
          args,
          cwd: path.dirname(sidecarPath),
          env: this._buildEnv(port, false),
        };
      }
    }

    const hasRepoSource = fs.existsSync(path.join(this.repoRoot, "chronox_finance", "api.py"));
    const venvCandidates =
      process.platform === "win32"
        ? [
            path.join(this.repoRoot, ".venv", "Scripts", "python.exe"),
            path.join(this.repoRoot, "venv", "Scripts", "python.exe"),
          ]
        : [
            path.join(this.repoRoot, ".venv", "bin", "python"),
            path.join(this.repoRoot, ".venv", "bin", "python3"),
            path.join(this.repoRoot, "venv", "bin", "python"),
          ];

    if (hasRepoSource) {
      for (const venvPython of venvCandidates) {
        if (fileExistsExecutable(venvPython)) {
          const args = [
            "-m",
            "chronox_finance.cli",
            "--db",
            this.dbPath,
            "serve",
            "--host",
            this.host,
            "--port",
            String(port),
          ];
          return {
            mode: "repo-venv",
            command: venvPython,
            args,
            cwd: this.repoRoot,
            env: this._buildEnv(port, true),
          };
        }
      }
    }

    const systemCandidates =
      process.platform === "win32"
        ? [
            { cmd: "python", prefix: [] },
            { cmd: "py", prefix: ["-3"] },
            { cmd: "python3", prefix: [] },
          ]
        : [
            { cmd: "python3", prefix: [] },
            { cmd: "python", prefix: [] },
          ];

    const probeEnv = this._buildEnv(port, hasRepoSource);
    for (const candidate of systemCandidates) {
      const check = spawnSync(
        candidate.cmd,
        [...candidate.prefix, "-c", "import chronox_finance, uvicorn"],
        {
          cwd: hasRepoSource ? this.repoRoot : undefined,
          env: probeEnv,
          timeout: 5000,
          stdio: "ignore",
          windowsHide: true,
        }
      );
      if (check.status === 0) {
        const args = [
          ...candidate.prefix,
          "-m",
          "chronox_finance.cli",
          "--db",
          this.dbPath,
          "serve",
          "--host",
          this.host,
          "--port",
          String(port),
        ];
        return {
          mode: "system-python",
          command: candidate.cmd,
          args,
          cwd: hasRepoSource ? this.repoRoot : this.app.getPath("userData"),
          env: probeEnv,
        };
      }
    }

    throw new Error(
      "Could not find the ChronoX backend sidecar or a Python environment with chronox_finance installed. " +
        "Run the setup script or install Python dependencies in .venv."
    );
  }

  _buildEnv(port, includeRepoPythonPath) {
    const env = {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
      CHRONOX_FINANCE_HOST: this.host,
      CHRONOX_FINANCE_PORT: String(port),
      CHRONOX_FINANCE_DB_PATH: this.dbPath,
    };
    if (includeRepoPythonPath) {
      env.PYTHONPATH = env.PYTHONPATH
        ? `${this.repoRoot}${path.delimiter}${env.PYTHONPATH}`
        : this.repoRoot;
    }
    return env;
  }

  async start() {
    if (this._startingPromise) return this._startingPromise;
    this._startingPromise = this._startInternal().finally(() => {
      this._startingPromise = null;
    });
    return this._startingPromise;
  }

  async _startInternal() {
    this._intentionalStop = false;
    this.partialLines = { stdout: "", stderr: "" };

    try {
      fs.mkdirSync(path.dirname(this.dbPath), { recursive: true });
    } catch (err) {
      const msg = `Unable to create local vault directory: ${err.message}`;
      this._pushLog("system", msg);
      this._emitStatus({ stage: "error", label: "Vault directory error", detail: msg, progress: 0, error: msg });
      throw err;
    }

    this._emitStatus({
      stage: "starting",
      label: "Starting local vault",
      detail: "Allocating secure localhost port...",
      progress: 18,
      error: null,
    });

    const port = await findFreePort(this.host);
    this.port = port;

    let launchSpec;
    try {
      launchSpec = this.resolveBackendCommand(port);
    } catch (err) {
      this._pushLog("system", err.message);
      this._emitStatus({
        stage: "error",
        label: "Backend runtime missing",
        detail: err.message,
        progress: 0,
        error: err.message,
      });
      throw err;
    }

    this.backendMode = launchSpec.mode;
    this._pushLog(
      "system",
      `Launching backend (${launchSpec.mode}) on ${this.baseUrl} with DB ${this.dbPath}`
    );

    this._emitStatus({
      stage: "starting",
      label: "Launching ChronoX engine",
      detail: `Starting ${launchSpec.mode === "packaged-sidecar" ? "bundled sidecar" : "local Python engine"} on 127.0.0.1:${port}`,
      progress: 38,
      error: null,
    });

    let earlyExitError = null;
    const useDetachedGroup = process.platform !== "win32";
    const child = spawn(launchSpec.command, launchSpec.args, {
      cwd: launchSpec.cwd,
      env: launchSpec.env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: useDetachedGroup,
      windowsHide: true,
    });
    this.proc = child;
    this._detachedGroup = useDetachedGroup;

    child.stdout.on("data", (chunk) => this._ingestStreamChunk("stdout", chunk));
    child.stderr.on("data", (chunk) => this._ingestStreamChunk("stderr", chunk));

    child.on("error", (err) => {
      earlyExitError = new Error(`Failed to spawn backend process: ${err.message}`);
      this._pushLog("system", earlyExitError.message);
    });

    child.on("exit", (code, signal) => {
      if (this.partialLines.stdout) {
        this._pushLog("stdout", this.partialLines.stdout);
        this.partialLines.stdout = "";
      }
      if (this.partialLines.stderr) {
        this._pushLog("stderr", this.partialLines.stderr);
        this.partialLines.stderr = "";
      }
      const exitNote = `Backend exited (code=${code}, signal=${signal || "none"})`;
      this._pushLog("system", exitNote);

      if (this.proc === child) {
        this.proc = null;
      }

      if (!this._intentionalStop) {
        earlyExitError = new Error(this._buildFailureSummary(exitNote));
        this.emit("unexpected-exit", { code, signal, message: earlyExitError.message });
      }
    });

    // Poll /api/health until ready or timeout
    const startedAt = Date.now();
    let attempt = 0;

    while (Date.now() - startedAt < STARTUP_TIMEOUT_MS) {
      if (earlyExitError) {
        this._emitStatus({
          stage: "error",
          label: "Backend failed to start",
          detail: earlyExitError.message,
          progress: 0,
          error: earlyExitError.message,
        });
        throw earlyExitError;
      }

      attempt += 1;
      const pct = Math.min(92, 46 + Math.round(attempt * 3.5));
      this._emitStatus({
        stage: "probing",
        label: "Checking readiness",
        detail: "Verifying SQLite vault & quant math engine...",
        progress: pct,
        error: null,
      });

      const res = await probeHealth(this.host, port);
      if (res.ok) {
        this._pushLog("system", `Backend healthy on ${this.baseUrl} (mode=${res.data?.intelligence_mode || "unknown"})`);
        this._emitStatus({
          stage: "ready",
          label: "Opening ChronoX",
          detail: "Local vault verified and ready",
          progress: 100,
          error: null,
        });
        return {
          url: this.baseUrl,
          port: this.port,
          mode: this.backendMode,
          health: res.data,
        };
      }

      await new Promise((r) => setTimeout(r, HEALTH_POLL_INTERVAL_MS));
    }

    const timeoutMsg = this._buildFailureSummary(
      `Timed out after ${Math.round(STARTUP_TIMEOUT_MS / 1000)}s waiting for /api/health.`
    );
    this._pushLog("system", timeoutMsg);
    await this.stop();
    this._emitStatus({
      stage: "error",
      label: "Startup timed out",
      detail: timeoutMsg,
      progress: 0,
      error: timeoutMsg,
    });
    throw new Error(timeoutMsg);
  }

  _buildFailureSummary(prefix) {
    const lastErr = [...this.logs].reverse().find((entry) => entry.stream === "stderr");
    return lastErr ? `${prefix} Last error: ${lastErr.text}` : prefix;
  }

  async restart() {
    await this.stop();
    return this.start();
  }

  /**
   * Guarantee clean process-tree teardown on quit with zero orphan Python processes.
   */
  async stop() {
    if (this._stoppingPromise) return this._stoppingPromise;
    this._stoppingPromise = this._stopInternal().finally(() => {
      this._stoppingPromise = null;
    });
    return this._stoppingPromise;
  }

  async _stopInternal() {
    this._intentionalStop = true;
    const child = this.proc;
    if (!child) return;

    const pid = child.pid;
    if (!pid || child.exitCode !== null || child.killed) {
      this.proc = null;
      return;
    }

    this._pushLog("system", `Stopping backend process (pid=${pid})...`);

    await new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        clearTimeout(forceTimer);
        resolve();
      };

      child.once("exit", done);

      const signalTree = (sig) => {
        if (process.platform === "win32") {
          const args = sig === "SIGKILL" ? ["/pid", String(pid), "/T", "/F"] : ["/pid", String(pid), "/T"];
          spawnSync("taskkill", args, {
            stdio: "ignore",
            windowsHide: true,
            timeout: 4000,
          });
          return;
        }
        if (this._detachedGroup) {
          try {
            process.kill(-pid, sig);
            return;
          } catch {
            // Fall back to direct child PID if process group already gone.
          }
        }
        try {
          child.kill(sig);
        } catch {
          // Process may already have exited.
        }
      };

      const forceKill = () => {
        try {
          signalTree("SIGKILL");
        } catch {
          // Best-effort hard kill.
        } finally {
          done();
        }
      };

      const forceTimer = setTimeout(forceKill, SHUTDOWN_GRACE_MS);

      try {
        signalTree("SIGTERM");
      } catch {
        forceKill();
      }
    });

    this.proc = null;
  }

  /**
   * Synchronous last-resort kill hook for process 'exit' events.
   */
  forceKillSync() {
    this._intentionalStop = true;
    const child = this.proc;
    if (!child || !child.pid || child.exitCode !== null) return;
    const pid = child.pid;
    try {
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true,
          timeout: 3000,
        });
      } else if (this._detachedGroup) {
        try {
          process.kill(-pid, "SIGKILL");
        } catch {
          process.kill(pid, "SIGKILL");
        }
      } else {
        process.kill(pid, "SIGKILL");
      }
    } catch {
      // Ignore ESRCH if already exited.
    }
    this.proc = null;
  }
}

module.exports = {
  BackendManager,
  findFreePort,
  scrubSecrets,
};
