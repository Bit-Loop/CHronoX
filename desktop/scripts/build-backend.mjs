#!/usr/bin/env node
/**
 * Cross-platform PyInstaller sidecar builder for ChronoX Finance Desktop.
 *
 * Detects the repo `.venv` (or system Python), verifies PyInstaller is installed,
 * bundles `desktop/scripts/backend_entry.py` + `chronox_finance/web/` via
 * `desktop/packaging/backend.spec`, and places the resulting sidecar at:
 *
 *   desktop/build/backend/chronox-backend      (Linux / macOS)
 *   desktop/build/backend/chronox-backend.exe  (Windows)
 *
 * Options:
 *   --onefile      Build a single-file binary (sets CHRONOX_PYINSTALLER_ONEFILE=1)
 *   --clean        Remove intermediate PyInstaller build cache before building
 *   --check-only   Verify Python & PyInstaller availability without building
 */

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DESKTOP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(DESKTOP_DIR, "..");
const SPEC_PATH = path.join(DESKTOP_DIR, "packaging", "backend.spec");
const DIST_ROOT = path.join(DESKTOP_DIR, "build");
const BACKEND_OUT_DIR = path.join(DIST_ROOT, "backend");
const WORK_DIR = path.join(DESKTOP_DIR, "build", "pyinstaller-work");

const IS_WINDOWS = process.platform === "win32";
const BINARY_NAME = IS_WINDOWS ? "chronox-backend.exe" : "chronox-backend";

const args = new Set(process.argv.slice(2));
const onefile =
  args.has("--onefile") ||
  ["1", "true", "yes"].includes(
    (process.env.CHRONOX_PYINSTALLER_ONEFILE || "").trim().toLowerCase()
  );
const cleanBuild = args.has("--clean");
const checkOnly = args.has("--check-only");

function log(msg) {
  console.log(`[build-backend] ${msg}`);
}

function fail(msg, code = 1) {
  console.error(`[build-backend] ERROR: ${msg}`);
  process.exit(code);
}

function canRun(cmd, testArgs = ["--version"]) {
  const res = spawnSync(cmd, testArgs, {
    stdio: "pipe",
    encoding: "utf8",
    windowsHide: true,
  });
  return res.status === 0;
}

function resolvePython() {
  const envPython = process.env.CHRONOX_PYTHON || process.env.PYTHON;
  const candidates = [];

  if (envPython) {
    candidates.push(envPython);
  }

  if (IS_WINDOWS) {
    candidates.push(
      path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
      path.join(REPO_ROOT, "venv", "Scripts", "python.exe"),
      "python",
      "py"
    );
  } else {
    candidates.push(
      path.join(REPO_ROOT, ".venv", "bin", "python"),
      path.join(REPO_ROOT, ".venv", "bin", "python3"),
      path.join(REPO_ROOT, "venv", "bin", "python"),
      "python3",
      "python"
    );
  }

  for (const candidate of candidates) {
    if (path.isAbsolute(candidate) && !fs.existsSync(candidate)) {
      continue;
    }
    if (canRun(candidate, ["--version"])) {
      return candidate;
    }
  }
  return null;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(1)} ${units[idx]}`;
}

function dirSize(targetPath) {
  if (!fs.existsSync(targetPath)) return 0;
  const stat = fs.statSync(targetPath);
  if (stat.isFile()) return stat.size;
  let total = 0;
  for (const entry of fs.readdirSync(targetPath, { withFileTypes: true })) {
    total += dirSize(path.join(targetPath, entry.name));
  }
  return total;
}

function main() {
  if (!fs.existsSync(SPEC_PATH)) {
    fail(`PyInstaller spec not found: ${SPEC_PATH}`);
  }

  const pythonBin = resolvePython();
  if (!pythonBin) {
    fail(
      [
        "No Python 3.11+ interpreter found.",
        "Create the project virtualenv first:",
        IS_WINDOWS
          ? "  .\\scripts\\install-windows.ps1"
          : "  ./scripts/install-linux.sh --dev",
      ].join("\n")
    );
  }

  const pyVer = spawnSync(pythonBin, ["--version"], {
    encoding: "utf8",
    windowsHide: true,
  });
  log(`Using Python: ${pythonBin} (${(pyVer.stdout || pyVer.stderr || "").trim()})`);

  // Verify chronox_finance is importable
  const pkgCheck = spawnSync(
    pythonBin,
    ["-c", "import chronox_finance, fastapi, uvicorn, pydantic"],
    {
      cwd: REPO_ROOT,
      encoding: "utf8",
      windowsHide: true,
    }
  );
  if (pkgCheck.status !== 0) {
    fail(
      [
        "Python environment is missing ChronoX Finance runtime dependencies.",
        "Install them with:",
        `  "${pythonBin}" -m pip install -e "${REPO_ROOT}"`,
      ].join("\n")
    );
  }

  // Verify PyInstaller is installed
  const pyiCheck = spawnSync(pythonBin, ["-m", "PyInstaller", "--version"], {
    encoding: "utf8",
    windowsHide: true,
  });
  if (pyiCheck.status !== 0) {
    const installHint = IS_WINDOWS
      ? `  "${pythonBin}" -m pip install pyinstaller`
      : `  uv pip install --python "${pythonBin}" pyinstaller\n  # or: "${pythonBin}" -m pip install pyinstaller`;
    console.error(
      [
        "",
        "[build-backend] PyInstaller is not installed in the active Python environment.",
        "Install PyInstaller to bundle the standalone zero-Python desktop sidecar:",
        "",
        installHint,
        "",
        "Then re-run: npm run build:backend",
        "(Note: local development via `npm start` does not require PyInstaller — it uses `.venv` directly.)",
        "",
      ].join("\n")
    );
    process.exit(1);
  }

  log(`PyInstaller version: ${(pyiCheck.stdout || "").trim()}`);

  if (checkOnly) {
    log("Environment check passed.");
    return;
  }

  // Clean previous backend bundle output so stale files are never packaged
  if (fs.existsSync(BACKEND_OUT_DIR)) {
    fs.rmSync(BACKEND_OUT_DIR, { recursive: true, force: true });
  }
  if (cleanBuild && fs.existsSync(WORK_DIR)) {
    fs.rmSync(WORK_DIR, { recursive: true, force: true });
  }

  const distPath = onefile ? BACKEND_OUT_DIR : DIST_ROOT;
  fs.mkdirSync(distPath, { recursive: true });
  fs.mkdirSync(WORK_DIR, { recursive: true });

  const env = {
    ...process.env,
    CHRONOX_PYINSTALLER_ONEFILE: onefile ? "1" : "0",
  };

  const pyiArgs = [
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--distpath",
    distPath,
    "--workpath",
    WORK_DIR,
  ];
  if (cleanBuild) {
    pyiArgs.push("--clean");
  }
  pyiArgs.push(SPEC_PATH);

  log(
    `Bundling standalone sidecar (${onefile ? "onefile" : "onedir"}, platform=${process.platform}, arch=${process.arch})...`
  );

  const buildRes = spawnSync(pythonBin, pyiArgs, {
    cwd: REPO_ROOT,
    env,
    stdio: "inherit",
    windowsHide: true,
  });

  if (buildRes.status !== 0) {
    fail(`PyInstaller exited with status ${buildRes.status ?? "unknown"}`);
  }

  const expectedBinary = path.join(BACKEND_OUT_DIR, BINARY_NAME);
  if (!fs.existsSync(expectedBinary)) {
    fail(`Expected sidecar binary was not created at ${expectedBinary}`);
  }

  if (!IS_WINDOWS) {
    fs.chmodSync(expectedBinary, 0o755);
  }

  // Remove intermediate work directory so desktop/build stays clean
  if (fs.existsSync(WORK_DIR)) {
    fs.rmSync(WORK_DIR, { recursive: true, force: true });
  }

  log(`Ready: ${expectedBinary}`);
  log(`Total sidecar bundle size: ${formatBytes(dirSize(BACKEND_OUT_DIR))}`);
}

main();
