#!/usr/bin/env bash
# ==============================================================================
# ChronoX Finance Desktop — One-Command Linux Setup & Packaging Script
# Supports: Debian/Ubuntu (.deb), Arch/CachyOS/EndeavourOS/Manjaro (.pacman),
#           Fedora/openSUSE/Generic (.AppImage), on x86_64 (x64) & aarch64 (arm64)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DESKTOP_DIR="${REPO_ROOT}/desktop"
VENV_DIR="${REPO_ROOT}/.venv"

BUILD_DEB=false
BUILD_PACMAN=false
BUILD_APPIMAGE=false
WITH_BACKEND=false
DEV_MODE=false
LAUNCH_APP=false
INSTALL_LAUNCHER=true

log()  { printf '\033[1;32m[chronox]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chronox]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[chronox]\033[0m %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
ChronoX Finance Desktop — Linux Setup & Packaging Installer

Usage:
  ./scripts/install-linux.sh [OPTIONS]

Options:
  --dev            Set up .venv, desktop deps, icons & user desktop launcher (default)
  --launch         After setup, launch ChronoX Finance desktop app
  --deb            Build Debian/Ubuntu .deb installer in desktop/dist/
  --pacman         Build Arch/CachyOS/EndeavourOS .pacman package in desktop/dist/
  --appimage       Build universal Linux .AppImage in desktop/dist/
  --all-packages   Build .deb, .pacman, and .AppImage in desktop/dist/
  --with-backend   Install PyInstaller in .venv & bundle zero-Python sidecar binary
  --no-launcher    Skip creating ~/.local/share/applications/chronox-finance.desktop
  -h, --help       Show this help text

Examples:
  ./scripts/install-linux.sh --dev --launch
  ./scripts/install-linux.sh --deb --with-backend
  ./scripts/install-linux.sh --pacman --with-backend
  ./scripts/install-linux.sh --appimage --with-backend
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)           DEV_MODE=true ;;
    --launch)        LAUNCH_APP=true ;;
    --deb)           BUILD_DEB=true ;;
    --pacman)        BUILD_PACMAN=true ;;
    --appimage)      BUILD_APPIMAGE=true ;;
    --all-packages)  BUILD_DEB=true; BUILD_PACMAN=true; BUILD_APPIMAGE=true ;;
    --with-backend)  WITH_BACKEND=true ;;
    --no-launcher)   INSTALL_LAUNCHER=false ;;
    -h|--help)       usage; exit 0 ;;
    *)
      err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${BUILD_DEB}" == false && "${BUILD_PACMAN}" == false && "${BUILD_APPIMAGE}" == false ]]; then
  DEV_MODE=true
fi

# --- 1. Detect CPU Architecture ---
RAW_ARCH="$(uname -m)"
case "${RAW_ARCH}" in
  x86_64|amd64)  EB_ARCH="x64" ;;
  aarch64|arm64) EB_ARCH="arm64" ;;
  *)
    err "Unsupported Linux CPU architecture: ${RAW_ARCH} (supported: x86_64, aarch64)"
    exit 1
    ;;
esac

# --- 2. Detect Linux Distro ---
DISTRO_ID="linux"
DISTRO_LIKE=""
DISTRO_NAME="Linux"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  DISTRO_ID="${ID:-linux}"
  DISTRO_LIKE="${ID_LIKE:-}"
  DISTRO_NAME="${PRETTY_NAME:-${NAME:-Linux}}"
fi

PKG_FAMILY="generic"
case " ${DISTRO_ID} ${DISTRO_LIKE} " in
  *" arch "*|*" cachyos "*|*" endeavouros "*|*" manjaro "*|*" garuda "*)
    PKG_FAMILY="arch"
    ;;
  *" debian "*|*" ubuntu "*|*" pop "*|*" linuxmint "*)
    PKG_FAMILY="debian"
    ;;
  *" fedora "*|*" rhel "*|*" rocky "*|*" almalinux "*)
    PKG_FAMILY="fedora"
    ;;
  *" suse "*|*" opensuse "*)
    PKG_FAMILY="suse"
    ;;
esac

log "Detected ${DISTRO_NAME} (${RAW_ARCH} -> ${EB_ARCH}, family=${PKG_FAMILY})"

print_prereq_hint() {
  case "${PKG_FAMILY}" in
    debian)
      echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip nodejs npm libgtk-3-0 libnss3 libnotify4 libsecret-1-0"
      ;;
    arch)
      echo "  sudo pacman -S --needed python nodejs npm gtk3 nss libnotify libsecret xdg-utils"
      ;;
    fedora)
      echo "  sudo dnf install -y python3 python3-pip nodejs npm gtk3 nss libnotify libsecret"
      ;;
    suse)
      echo "  sudo zypper install -y python312 nodejs20 npm20 gtk3 mozilla-nss libnotify4 libsecret-1-0"
      ;;
    *)
      echo "  Install Python 3.11+ (3.12 recommended) and Node.js 20+ via your package manager."
      ;;
  esac
}

# --- 3. Check Python >= 3.11 ---
PYTHON_SYS=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON_SYS="${candidate}"
      break
    fi
  fi
done

if [[ -z "${PYTHON_SYS}" ]]; then
  err "Python 3.11+ (3.12 recommended) is required but was not found."
  echo "Install prerequisites for your system:" >&2
  print_prereq_hint >&2
  exit 1
fi
log "Using Python: $("${PYTHON_SYS}" --version)"

# --- 4. Check Node.js >= 20 & npm ---
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  err "Node.js 20+ and npm are required but were not found."
  echo "Install prerequisites for your system:" >&2
  print_prereq_hint >&2
  exit 1
fi

NODE_MAJOR="$(node -p 'parseInt(process.versions.node.split(".")[0], 10)')"
if (( NODE_MAJOR < 20 )); then
  err "Node.js 20+ is required (found $(node -v))."
  print_prereq_hint >&2
  exit 1
fi
log "Using Node.js $(node -v) / npm $(npm -v)"

# --- 5. Set up Python Virtual Environment ---
VENV_PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  log "Creating Python virtual environment at ${VENV_DIR}..."
  if command -v uv >/dev/null 2>&1; then
    uv venv "${VENV_DIR}" --python "${PYTHON_SYS}"
  else
    "${PYTHON_SYS}" -m venv "${VENV_DIR}"
  fi
fi

log "Installing ChronoX Finance Python dependencies into .venv..."
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "${VENV_PYTHON}" -e "${REPO_ROOT}"
else
  "${VENV_PYTHON}" -m pip install --upgrade pip >/dev/null
  "${VENV_PYTHON}" -m pip install -e "${REPO_ROOT}"
fi

if [[ "${WITH_BACKEND}" == true ]]; then
  log "Ensuring PyInstaller is installed in .venv..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "${VENV_PYTHON}" pyinstaller
  else
    "${VENV_PYTHON}" -m pip install pyinstaller
  fi
fi

# --- 6. Install Electron Desktop Dependencies & Icons ---
log "Installing Electron desktop dependencies in desktop/..."
(
  cd "${DESKTOP_DIR}"
  if [[ -f package-lock.json ]]; then
    npm ci --no-audit --no-fund
  else
    npm install --no-audit --no-fund
  fi
  log "Generating ChronoX Finance multi-size app & tray icons..."
  npm run icons
)

# --- 7. Optional Standalone Backend Sidecar Bundle ---
if [[ "${WITH_BACKEND}" == true ]]; then
  log "Bundling zero-dependency Python sidecar binary (chronox-backend)..."
  (cd "${DESKTOP_DIR}" && CHRONOX_PYTHON="${VENV_PYTHON}" npm run build:backend)
elif [[ "${BUILD_DEB}" == true || "${BUILD_PACMAN}" == true || "${BUILD_APPIMAGE}" == true ]]; then
  if "${VENV_PYTHON}" -m PyInstaller --version >/dev/null 2>&1; then
    log "PyInstaller detected in .venv — bundling standalone sidecar binary..."
    (cd "${DESKTOP_DIR}" && CHRONOX_PYTHON="${VENV_PYTHON}" npm run build:backend)
  else
    warn "PyInstaller is not installed in .venv; package will fall back to local Python unless you pass --with-backend."
  fi
fi

# --- 8. Build Requested Linux Packages ---
build_target() {
  local target="$1"
  log "Building Linux ${target} package for ${EB_ARCH}..."
  (
    cd "${DESKTOP_DIR}"
    npx electron-builder \
      --config electron-builder.yml \
      --linux "${target}" \
      "--${EB_ARCH}" \
      --publish never
  )
}

if [[ "${BUILD_DEB}" == true ]]; then
  build_target deb
fi
if [[ "${BUILD_PACMAN}" == true ]]; then
  if ! command -v bsdtar >/dev/null 2>&1; then
    err "'bsdtar' is required to build .pacman packages (used by electron-builder / fpm)."
    case "${PKG_FAMILY}" in
      debian) echo "  Install it with: sudo apt update && sudo apt install -y libarchive-tools" >&2 ;;
      arch)   echo "  Install it with: sudo pacman -S --needed libarchive" >&2 ;;
      fedora) echo "  Install it with: sudo dnf install -y bsdtar" >&2 ;;
      suse)   echo "  Install it with: sudo zypper install -y bsdtar" >&2 ;;
      *)      echo "  Install 'libarchive' / 'libarchive-tools' (providing bsdtar) via your package manager." >&2 ;;
    esac
    exit 1
  fi
  build_target pacman
fi
if [[ "${BUILD_APPIMAGE}" == true ]]; then
  build_target AppImage
fi

if [[ "${BUILD_DEB}" == true || "${BUILD_PACMAN}" == true || "${BUILD_APPIMAGE}" == true ]]; then
  log "Built packages in ${DESKTOP_DIR}/dist:"
  ls -lh "${DESKTOP_DIR}/dist"/*.{deb,pacman,AppImage} 2>/dev/null || true
  if [[ "${BUILD_DEB}" == true && "${PKG_FAMILY}" == "debian" ]]; then
    echo "  Install .deb:     sudo apt install ${DESKTOP_DIR}/dist/chronox-finance-*-linux-${EB_ARCH}.deb"
  fi
  if [[ "${BUILD_PACMAN}" == true && "${PKG_FAMILY}" == "arch" ]]; then
    echo "  Install .pacman:  sudo pacman -U ${DESKTOP_DIR}/dist/chronox-finance-*-linux-${EB_ARCH}.pacman"
  fi
  if [[ "${BUILD_APPIMAGE}" == true ]]; then
    echo "  Run AppImage:     chmod +x ${DESKTOP_DIR}/dist/chronox-finance-*-linux-${EB_ARCH}.AppImage && ${DESKTOP_DIR}/dist/chronox-finance-*-linux-${EB_ARCH}.AppImage"
  fi
fi

# --- 9. Install User Desktop Launcher (Dev / Local Install) ---
if [[ "${DEV_MODE}" == true && "${INSTALL_LAUNCHER}" == true ]]; then
  LOCAL_BIN="${HOME}/.local/bin"
  LOCAL_APPS="${HOME}/.local/share/applications"
  LOCAL_ICONS="${HOME}/.local/share/icons/hicolor"

  mkdir -p "${LOCAL_BIN}" "${LOCAL_APPS}"

  cat > "${LOCAL_BIN}/chronox-finance" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${DESKTOP_DIR}"
exec "${DESKTOP_DIR}/node_modules/.bin/electron" "${DESKTOP_DIR}" "\$@"
EOF
  chmod 0755 "${LOCAL_BIN}/chronox-finance"

  for size in 16 32 48 64 128 256 512; do
    if [[ -f "${DESKTOP_DIR}/build/icons/${size}x${size}.png" ]]; then
      mkdir -p "${LOCAL_ICONS}/${size}x${size}/apps"
      cp -f "${DESKTOP_DIR}/build/icons/${size}x${size}.png" \
        "${LOCAL_ICONS}/${size}x${size}/apps/chronox-finance.png"
    fi
  done
  if [[ -f "${DESKTOP_DIR}/build/icons/icon.svg" ]]; then
    mkdir -p "${LOCAL_ICONS}/scalable/apps"
    cp -f "${DESKTOP_DIR}/build/icons/icon.svg" \
      "${LOCAL_ICONS}/scalable/apps/chronox-finance.svg"
  fi

  cat > "${LOCAL_APPS}/chronox-finance.desktop" <<EOF
[Desktop Entry]
Version=1.5
Type=Application
Name=ChronoX Finance
GenericName=Personal Finance & Auto-Budget
Comment=Industrial local-first personal finance command center with encrypted vault, quant analytics, credit simulator & AI coach
Exec=${LOCAL_BIN}/chronox-finance %U
Icon=chronox-finance
Terminal=false
Categories=Office;Finance;
Keywords=budget;finance;vault;credit;fico;azeo;portfolio;cashflow;money;quant;
StartupNotify=true
StartupWMClass=chronox-finance
MimeType=text/csv;text/tab-separated-values;application/x-ofx;application/vnd.intu.qfx;application/x-qw;
EOF

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${LOCAL_APPS}" 2>/dev/null || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${LOCAL_ICONS}" 2>/dev/null || true
  fi

  log "Installed desktop launcher: ${LOCAL_APPS}/chronox-finance.desktop"
  log "Installed CLI launcher:     ${LOCAL_BIN}/chronox-finance"
fi

# --- 10. Optional Immediate Launch ---
if [[ "${LAUNCH_APP}" == true ]]; then
  log "Launching ChronoX Finance Desktop..."
  (cd "${DESKTOP_DIR}" && npm start)
else
  log "Setup complete! Launch anytime with: make desktop"
fi
