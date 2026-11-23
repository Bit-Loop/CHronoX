# ChronoX Finance — Arch Linux Packaging Guide

Supported architectures: **`x86_64`** and **`aarch64`** (Arch Linux, CachyOS, EndeavourOS, Manjaro, Garuda).

---

## Option 1: Install Prebuilt `.pacman` Package (Fastest)

Download the release `.pacman` artifact matching your CPU architecture (`x64` for `x86_64`, `arm64` for `aarch64`), or build it locally with `./scripts/install-linux.sh --pacman --with-backend`:

```bash
sudo pacman -U chronox-finance-*-linux-x64.pacman
# Or on ARM64 / aarch64:
# sudo pacman -U chronox-finance-*-linux-arm64.pacman
```

Launch from your application launcher (**ChronoX Finance**) or terminal:

```bash
chronox-finance
# Or launch preloaded with the 90-day ADHD demo workspace:
chronox-finance --demo
```

---

## Option 2: Build & Install via `PKGBUILD` (`makepkg`)

### Build from GitHub

```bash
cd desktop/packaging/arch
makepkg -si
```

### Build Offline from This Local Repository Checkout

```bash
cd desktop/packaging/arch
CHRONOX_LOCAL_REPO=1 makepkg -si
```

This builds an isolated zero-dependency Python sidecar (`chronox-backend`), packages the Electron app into `/opt/chronox-finance`, symlinks `/usr/bin/chronox-finance`, and installs the desktop entry and hicolor icons (`16x16` through `512x512` + scalable SVG).

---

## Wayland & HiDPI Custom Flags

The `/usr/bin/chronox-finance` launcher automatically reads optional Electron flags from `~/.config/chronox-finance-flags.conf` (one flag per line, `#` comments ignored). For native Wayland on Hyprland, Sway, KDE Plasma, or GNOME:

```text
# ~/.config/chronox-finance-flags.conf
--enable-features=UseOzonePlatform
--ozone-platform-hint=auto
```

---

## Local Data & Uninstall

- **SQLite Ledger & Settings**: `~/.config/ChronoX Finance/finance.sqlite3` (preserved across upgrades and uninstalls)
- **Uninstall package**:

```bash
sudo pacman -Rns chronox-finance
```
