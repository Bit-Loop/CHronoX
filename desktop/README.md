# ChronoX Finance Desktop App

First-party Electron desktop shell for **ChronoX Finance** across **Windows (`x64` & `arm64`)**, **Linux — Debian/Ubuntu (`.deb`), Arch/CachyOS/EndeavourOS (`.pacman` & `PKGBUILD`), and universal `.AppImage` (`x64` & `arm64`)**, and **macOS (`arm64` & `x64`)**.

## Desktop Capabilities

- **Zero-Friction Local Backend Sidecar**: Automatically launches the bundled `chronox-backend` PyInstaller sidecar (or falls back to repo `.venv` in dev) on an ephemeral `127.0.0.1` port and shuts down cleanly with zero orphan processes.
- **Persistent Native Data Vault**: Stores your SQLite ledger inside your OS application data folder (`%APPDATA%\ChronoX Finance\finance.sqlite3` on Windows, `~/.config/ChronoX Finance/finance.sqlite3` on Linux, `~/Library/Application Support/ChronoX Finance/finance.sqlite3` on macOS).
- **Frameless Native Window Chrome**: Adaptive Obsidian Dark & Porcelain Light titlebar integration (`titleBarOverlay` on Windows, `hiddenInset` on macOS, custom vector controls on Linux).
- **Global ADHD Quick-Capture HUD (`Ctrl+Shift+Space` / `Cmd+Shift+Space`)**: Floating Spotlight-style capture box to log expenses, paychecks, or impulse cool-offs from anywhere without context switching.
- **Live System Tray**: Displays today's **Safe to Spend** figure, switches themes and Intelligence Modes (`Quant Math`, `Hybrid`, `AI Only`), and supports optional minimize-to-tray and launch-at-login.
- **Hardened Electron Security**: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, `webSecurity: true`, strict CSP, and navigation/popup guards limited to `http://127.0.0.1:<port>`.

---

## One-Command Local Setup

### Linux (Debian/Ubuntu, Arch/CachyOS/EndeavourOS, Fedora — `x86_64` & `arm64`)

```bash
# Set up .venv, Electron deps, icons, and desktop app launcher:
./scripts/install-linux.sh --dev

# Set up and immediately launch ChronoX Finance:
./scripts/install-linux.sh --dev --launch
```

### Windows (`x64` & `ARM64` — PowerShell 5.1 / 7+)

```powershell
# Set up .venv, Electron deps, and Windows .ico icons:
.\scripts\install-windows.ps1

# Set up and immediately launch ChronoX Finance:
.\scripts\install-windows.ps1 -Launch
```

### Or via `make`

```bash
make dev
make desktop-install
make desktop         # Launch ChronoX Finance Desktop (mandatory vault lock)
```

---

## Building Self-Contained Installers

### 1. Bundle the Zero-Python Backend Sidecar (Recommended for Distribution)

Install PyInstaller in `.venv` once, then bundle `chronox-backend` (or `chronox-backend.exe`) into `desktop/build/backend/`:

```bash
# Linux / macOS
uv pip install --python .venv/bin/python pyinstaller
make desktop-backend

# Windows PowerShell
.venv\Scripts\python.exe -m pip install pyinstaller
cd desktop; npm run build:backend
```

`electron-builder` automatically packages `desktop/build/backend/**` into `resources/backend/` so end users do **not** need Python installed.

### 2. Platform Packages

| Platform | Architectures | Build Command | Output (`desktop/dist/`) |
| --- | --- | --- | --- |
| **Debian / Ubuntu** | `x64`, `arm64` | `./scripts/install-linux.sh --deb --with-backend` or `make desktop-deb` | `chronox-finance-*-linux-<arch>.deb` |
| **Arch / CachyOS / EndeavourOS** | `x64`, `arm64` | `./scripts/install-linux.sh --pacman --with-backend` or `make desktop-pacman` | `chronox-finance-*-linux-<arch>.pacman` |
| **Arch `PKGBUILD` (`makepkg`)** | `x86_64`, `aarch64` | `cd desktop/packaging/arch && CHRONOX_LOCAL_REPO=1 makepkg -si` | Native Arch package in `/opt/chronox-finance` |
| **Universal Linux** | `x64`, `arm64` | `./scripts/install-linux.sh --appimage --with-backend` or `make desktop-appimage` | `chronox-finance-*-linux-<arch>.AppImage` |
| **Windows Installer + Portable** | `x64`, `arm64` | `.\scripts\install-windows.ps1 -BuildInstaller` or `make desktop-win` | `*-win-<arch>-setup.exe` & `*-win-<arch>-portable.exe` |
| **macOS** | `arm64`, `x64` | `make desktop-mac` | `chronox-finance-*-mac-<arch>.dmg` |

### Installing Built Packages

- **Debian / Ubuntu**: `sudo apt install ./desktop/dist/chronox-finance-*-linux-*.deb`
- **Arch / CachyOS / EndeavourOS**: `sudo pacman -U ./desktop/dist/chronox-finance-*-linux-*.pacman` (see [packaging/arch/README.md](packaging/arch/README.md) for Wayland flags & `makepkg`)
- **AppImage**: `chmod +x desktop/dist/chronox-finance-*.AppImage && ./desktop/dist/chronox-finance-*.AppImage`
- **Windows**: Run `chronox-finance-*-win-*-setup.exe` (customizable NSIS installer) or `chronox-finance-*-win-*-portable.exe` (no installation required)

---

## Automated Multi-Arch Releases (GitHub Actions)

Tagging a release (e.g., `git tag v1.1.0 && git push origin v1.1.0`) triggers [`.github/workflows/release-desktop.yml`](../.github/workflows/release-desktop.yml), which runs the full static/security/pytest gate and builds native PyInstaller + Electron packages across:

- `ubuntu-latest` (`x64`: `.deb`, `.pacman`, `.AppImage`)
- `ubuntu-24.04-arm` (`arm64`: `.deb`, `.pacman`, `.AppImage`)
- `windows-latest` (`x64`: NSIS + Portable)
- `windows-11-arm` (`arm64`: NSIS + Portable)
- `macos-latest` (`arm64`) & `macos-15-intel` (`x64`): `.dmg`

All assets are published with combined `SHA256SUMS.txt`.
