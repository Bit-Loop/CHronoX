"""Comprehensive desktop Electron security, IPC contract, icon, and cross-platform packaging tests."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = REPO_ROOT / "desktop"
DESKTOP_SRC = DESKTOP_DIR / "src"
ICONS_DIR = DESKTOP_DIR / "build" / "icons"
WEB_DIR = REPO_ROOT / "chronox_finance" / "web"

# Raw Unicode emoji / pictograph / variation-selector ranges forbidden across first-party UI sources.
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE0F"
    "]"
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_targets(target_entries: object) -> Dict[str, Set[str]]:
    """Normalize electron-builder target declarations into {target_name: set(arches)}."""
    out: Dict[str, Set[str]] = {}
    assert isinstance(target_entries, list), f"Expected list of targets, got {type(target_entries)}"
    for item in target_entries:
        if isinstance(item, str):
            out.setdefault(item, set())
        elif isinstance(item, dict):
            name = str(item.get("target", ""))
            arches = item.get("arch", [])
            if isinstance(arches, str):
                arches = [arches]
            out.setdefault(name, set()).update(str(a) for a in arches)
    return out


class TestElectronSecurityInvariants:
    """Verify strict Electron BrowserWindow hardening, navigation guards, and preload isolation."""

    def test_all_browser_windows_enforce_hardened_web_preferences(self) -> None:
        index_js = _read_text(DESKTOP_SRC / "main" / "index.js")
        bw_blocks = re.findall(r"new BrowserWindow\(\{[\s\S]*?\n  \}\);", index_js)
        assert len(bw_blocks) == 3, f"Expected 3 BrowserWindows (splash, main, hud), found {len(bw_blocks)}"

        required_flags = [
            "contextIsolation: true",
            "nodeIntegration: false",
            "sandbox: true",
            "webSecurity: true",
            "allowRunningInsecureContent: false",
            "webviewTag: false",
        ]
        for idx, block in enumerate(bw_blocks):
            for flag in required_flags:
                assert flag in block, f"BrowserWindow #{idx} missing hardened flag: {flag}"

    def test_navigation_popup_permission_and_ipc_sender_guards(self) -> None:
        index_js = _read_text(DESKTOP_SRC / "main" / "index.js")
        required_guards = [
            "app.requestSingleInstanceLock()",
            'app.on("web-contents-created"',
            '"will-attach-webview"',
            "setWindowOpenHandler",
            '"will-navigate"',
            "setPermissionRequestHandler",
            "setPermissionCheckHandler",
            "isAllowedBackendUrl",
            'parsed.protocol !== "https:"',
            "assertMainSender",
            "assertSplashSender",
            "assertHudSender",
        ]
        for token in required_guards:
            assert token in index_js, f"Missing security guard in main/index.js: {token}"

        # Ensure zero deprecated/unsafe Electron remote module usage across desktop/src
        for js_file in sorted(DESKTOP_SRC.rglob("*.js")):
            txt = _read_text(js_file)
            assert "@electron/remote" not in txt, f"Forbidden @electron/remote in {js_file}"
            assert "enableRemoteModule" not in txt, f"Forbidden enableRemoteModule in {js_file}"
            assert "nodeIntegration: true" not in txt, f"Forbidden nodeIntegration: true in {js_file}"
            assert "contextIsolation: false" not in txt, f"Forbidden contextIsolation: false in {js_file}"
            assert "sandbox: false" not in txt, f"Forbidden sandbox: false in {js_file}"
            assert "webSecurity: false" not in txt, f"Forbidden webSecurity: false in {js_file}"

    def test_preloads_expose_frozen_bridges_and_zero_raw_electron_primitives(self) -> None:
        main_preload = _read_text(DESKTOP_SRC / "preload" / "preload.js")
        hud_preload = _read_text(DESKTOP_SRC / "preload" / "hud-preload.js")

        assert 'contextBridge.exposeInMainWorld("chronoxDesktop"' in main_preload
        assert "Object.freeze(" in main_preload
        assert 'contextBridge.exposeInMainWorld("chronoxSplash"' in hud_preload
        assert 'contextBridge.exposeInMainWorld("chronoxHud"' in hud_preload
        assert "Object.freeze(" in hud_preload

        for label, src in [("preload.js", main_preload), ("hud-preload.js", hud_preload)]:
            assert "require(\"fs\")" not in src and "require('fs')" not in src, f"{label} imports fs"
            assert "child_process" not in src, f"{label} imports child_process"
            assert "ipcRenderer.sendSync" not in src, f"{label} uses blocking sendSync"
            assert "exposeInMainWorld(\"ipcRenderer\"" not in src, f"{label} leaks raw ipcRenderer"

    def test_ipc_channels_and_preload_methods_are_100_percent_aligned(self) -> None:
        index_js = _read_text(DESKTOP_SRC / "main" / "index.js")
        preload_js = _read_text(DESKTOP_SRC / "preload" / "preload.js")
        hud_preload_js = _read_text(DESKTOP_SRC / "preload" / "hud-preload.js")
        web_js_files = [WEB_DIR / "app.js"] + sorted((WEB_DIR / "js").glob("*.js"))
        web_js_combined = "\n".join(_read_text(p) for p in web_js_files)
        splash_js = _read_text(DESKTOP_SRC / "splash" / "splash.js")
        hud_js = _read_text(DESKTOP_SRC / "quick-capture" / "quick-capture.js")

        main_handles = set(re.findall(r'ipcMain\.handle\(\s*"([^"]+)"', index_js))
        main_sends = set(re.findall(r'(?:sendToMain|\.webContents\.send)\(\s*"([^"]+)"', index_js))
        preload_invokes = set(re.findall(r'ipcRenderer\.invoke\(\s*"([^"]+)"', preload_js + "\n" + hud_preload_js))
        preload_subs = set(re.findall(r'subscribe\(\s*"([^"]+)"', preload_js + "\n" + hud_preload_js))

        assert preload_invokes <= main_handles, (
            f"Preload invokes unhandled IPC channels: {sorted(preload_invokes - main_handles)}"
        )
        assert preload_subs <= main_sends, (
            f"Preload subscribes to unsent IPC channels: {sorted(preload_subs - main_sends)}"
        )

        # Verify every window.chronoxDesktop property called across app.js + web/js/*.js exists on preload.js
        preload_props = set(re.findall(r"^\s+([A-Za-z0-9_]+)\s*:", preload_js, re.M))
        used_desktop_props = set(re.findall(r"\bdesktop\.([A-Za-z0-9_]+)", web_js_combined))
        assert len(used_desktop_props) >= 15
        assert used_desktop_props <= preload_props, (
            f"Web frontend references missing chronoxDesktop methods: {sorted(used_desktop_props - preload_props)}"
        )

        # Verify splash.js & quick-capture.js bridge methods exist on hud-preload.js
        splash_props = set(re.findall(r"bridge\.([A-Za-z0-9_]+)", splash_js))
        hud_props = set(re.findall(r"bridge\.([A-Za-z0-9_]+)", hud_js))
        exposed_hud_props = set(re.findall(r"^\s+([A-Za-z0-9_]+)\s*:", hud_preload_js, re.M))
        assert splash_props <= exposed_hud_props
        assert hud_props <= exposed_hud_props

    def test_backend_manager_scrubs_secrets_and_binds_loopback_only(self) -> None:
        bm_js = _read_text(DESKTOP_SRC / "main" / "backend-manager.js")
        assert 'this.host = "127.0.0.1"' in bm_js
        assert "scrubSecrets" in bm_js
        assert "AIza[REDACTED]" in bm_js
        assert "sk-[REDACTED]" in bm_js
        assert "chronox_session=" in bm_js
        assert "taskkill" in bm_js
        assert "SIGKILL" in bm_js


class TestDesktopAndWebHygieneContract:
    """Enforce zero XSS sinks, strict CSP, DOM ID integrity, reduced motion, and zero raw emojis."""

    def test_zero_xss_sinks_inline_handlers_external_cdns_and_raw_emojis(self) -> None:
        source_files = sorted(
            [p for p in DESKTOP_SRC.rglob("*") if p.is_file()]
            + [p for p in WEB_DIR.rglob("*") if p.is_file()]
        )
        assert len(source_files) >= 12

        forbidden_js_patterns: List[Tuple[str, str]] = [
            (r"\binnerHTML\b", "innerHTML"),
            (r"\bouterHTML\b", "outerHTML"),
            (r"\binsertAdjacentHTML\b", "insertAdjacentHTML"),
            (r"\bdocument\.write\b", "document.write"),
            (r"\beval\s*\(", "eval()"),
            (r"\bnew\s+Function\b", "new Function"),
        ]
        forbidden_html_patterns: List[Tuple[str, str]] = [
            (r"\bon[a-z]+\s*=", "inline on* event attribute"),
            (r"https?://(?:cdn\.|fonts\.googleapis|unpkg\.com|cdnjs\.cloudflare)", "external CDN"),
        ]

        violations: List[str] = []
        for path in source_files:
            rel = path.relative_to(REPO_ROOT)
            text = _read_text(path)

            if path.suffix == ".js":
                for pattern, label in forbidden_js_patterns:
                    if re.search(pattern, text):
                        violations.append(f"{rel}: forbidden {label}")
            if path.suffix == ".html":
                for pattern, label in forbidden_html_patterns:
                    if re.search(pattern, text, re.I):
                        violations.append(f"{rel}: forbidden {label}")
            emoji_match = EMOJI_PATTERN.search(text)
            if emoji_match:
                ch = emoji_match.group(0)
                violations.append(f"{rel}: raw Unicode emoji/pictograph U+{ord(ch):04X}")

        assert violations == [], "Hygiene violations:\n" + "\n".join(violations)

    def test_strict_csp_and_dom_ids_and_reduced_motion_on_all_surfaces(self) -> None:
        for html_path in [
            DESKTOP_SRC / "splash" / "splash.html",
            DESKTOP_SRC / "quick-capture" / "quick-capture.html",
        ]:
            html = _read_text(html_path)
            assert 'http-equiv="Content-Security-Policy"' in html
            assert "default-src 'self'" in html
            assert "script-src 'self'" in html
            assert "unsafe-inline" not in html
            assert "unsafe-eval" not in html

        dom_pairs = [
            (DESKTOP_SRC / "splash" / "splash.html", [DESKTOP_SRC / "splash" / "splash.js"], r'getElementById\("([^"]+)"\)'),
            (
                DESKTOP_SRC / "quick-capture" / "quick-capture.html",
                [DESKTOP_SRC / "quick-capture" / "quick-capture.js"],
                r'getElementById\("([^"]+)"\)',
            ),
            (
                WEB_DIR / "index.html",
                [WEB_DIR / "app.js"] + sorted((WEB_DIR / "js").glob("*.js")),
                r'\$\("([^"]+)"\)',
            ),
        ]
        for html_path, js_paths, pattern in dom_pairs:
            html_ids = set(re.findall(r'\bid="([^"]+)"', _read_text(html_path)))
            dynamic_ids: Set[str] = set()
            for jp in js_paths:
                jtxt = _read_text(jp)
                dynamic_ids.update(re.findall(r'\bid\s*:\s*"([^"]+)"', jtxt))
                dynamic_ids.update(re.findall(r'\.id\s*=\s*"([^"]+)"', jtxt))
            valid_ids = html_ids | dynamic_ids
            for jp in js_paths:
                js_ids = set(re.findall(pattern, _read_text(jp)))
                missing = sorted(js_ids - valid_ids)
                assert missing == [], f"{jp.name} references missing IDs in {html_path.name}: {missing}"

        for css_path in [
            DESKTOP_SRC / "splash" / "splash.css",
            DESKTOP_SRC / "quick-capture" / "quick-capture.css",
            WEB_DIR / "styles.css",
        ]:
            assert "prefers-reduced-motion" in _read_text(css_path), f"Missing reduced motion in {css_path}"


class TestSyntaxAndBrandIcons:
    """Validate JS/MJS syntax, Bash syntax, and binary icon headers/dimensions."""

    def test_node_check_passes_for_all_desktop_and_web_js(self) -> None:
        node_bin = shutil.which("node")
        if not node_bin:
            pytest.skip("node binary not available")

        js_files = (
            sorted(DESKTOP_SRC.rglob("*.js"))
            + sorted((DESKTOP_DIR / "scripts").glob("*.mjs"))
            + [WEB_DIR / "app.js"]
            + sorted((WEB_DIR / "js").glob("*.js"))
        )
        assert (DESKTOP_SRC / "main" / "watch-folder.js") in js_files
        assert len(js_files) >= 16
        for js_file in js_files:
            res = subprocess.run(  # noqa: S603
                [node_bin, "--check", str(js_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"node --check failed for {js_file}:\n{res.stderr}"

    def test_bash_syntax_passes_for_install_script_and_pkgbuild(self) -> None:
        bash_bin = shutil.which("bash")
        if not bash_bin:
            pytest.skip("bash binary not available")

        install_sh = REPO_ROOT / "scripts" / "install-linux.sh"
        pkgbuild = DESKTOP_DIR / "packaging" / "arch" / "PKGBUILD"
        assert os.access(install_sh, os.X_OK), "scripts/install-linux.sh must be executable"

        for script in [install_sh, pkgbuild]:
            res = subprocess.run(  # noqa: S603
                [bash_bin, "-n", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"bash -n failed for {script}:\n{res.stderr}"

    def test_multi_size_png_ico_icns_svg_and_tray_icons_are_valid(self) -> None:
        expected_pngs = {
            "16x16.png": (16, 16),
            "32x32.png": (32, 32),
            "48x48.png": (48, 48),
            "64x64.png": (64, 64),
            "128x128.png": (128, 128),
            "256x256.png": (256, 256),
            "512x512.png": (512, 512),
            "icon.png": (512, 512),
            "tray-dark.png": (32, 32),
            "tray-light.png": (32, 32),
            "trayTemplate.png": (22, 22),
            "trayTemplate@2x.png": (44, 44),
        }
        for filename, expected_dims in expected_pngs.items():
            path = ICONS_DIR / filename
            assert path.is_file(), f"Missing icon asset: {path}"
            data = path.read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n", f"Invalid PNG signature in {filename}"
            width, height = struct.unpack(">II", data[16:24])
            assert (width, height) == expected_dims, f"{filename}: got {(width, height)}, expected {expected_dims}"

        # Validate Windows multi-resolution ICO header & embedded resolutions
        ico_path = ICONS_DIR / "icon.ico"
        assert ico_path.is_file(), "Missing icon.ico"
        ico_data = ico_path.read_bytes()
        reserved, img_type, count = struct.unpack("<HHH", ico_data[:6])
        assert reserved == 0 and img_type == 1 and count >= 5
        ico_sizes = set()
        for idx in range(count):
            offset = 6 + idx * 16
            w_byte, h_byte = ico_data[offset], ico_data[offset + 1]
            ico_sizes.add((256 if w_byte == 0 else w_byte, 256 if h_byte == 0 else h_byte))
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= ico_sizes

        # Validate macOS ICNS and source SVG
        icns_path = ICONS_DIR / "icon.icns"
        assert icns_path.is_file() and icns_path.read_bytes()[:4] == b"icns"
        svg_text = _read_text(ICONS_DIR / "icon.svg")
        assert "<svg" in svg_text and "</svg>" in svg_text


class TestCrossPlatformPackagingConfiguration:
    """Validate electron-builder.yml, package.json, PKGBUILD, release workflow, and sidecar entrypoint."""

    def test_electron_builder_declares_all_x64_and_arm64_targets(self) -> None:
        cfg = yaml.safe_load(_read_text(DESKTOP_DIR / "electron-builder.yml"))
        assert isinstance(cfg, dict)
        assert cfg["appId"] == "com.chronox.finance"
        assert cfg["productName"] == "ChronoX Finance"
        assert cfg["asar"] is True

        # Sidecar extraResources matches backend-manager.js packaged path
        extra = cfg.get("extraResources", [])
        assert any(
            isinstance(item, dict) and item.get("from") == "build/backend" and item.get("to") == "backend"
            for item in extra
        )

        # Windows x64 + arm64: nsis + portable
        win_targets = _extract_targets(cfg["win"]["target"])
        assert win_targets.get("nsis") == {"x64", "arm64"}
        assert win_targets.get("portable") == {"x64", "arm64"}
        assert cfg["win"]["icon"] == "build/icons/icon.ico"

        # Linux x64 + arm64: deb + pacman + AppImage
        linux_targets = _extract_targets(cfg["linux"]["target"])
        assert linux_targets.get("deb") == {"x64", "arm64"}
        assert linux_targets.get("pacman") == {"x64", "arm64"}
        assert linux_targets.get("AppImage") == {"x64", "arm64"}
        assert cfg["linux"]["category"] == "Finance"
        assert cfg["linux"].get("syncDesktopName") is True
        assert cfg["linux"]["desktop"]["entry"]["StartupWMClass"] == "chronox-finance"
        assert len(cfg["deb"]["depends"]) >= 5
        assert len(cfg["pacman"]["depends"]) >= 5

        # macOS arm64 + x64: dmg
        mac_targets = _extract_targets(cfg["mac"]["target"])
        assert mac_targets.get("dmg") == {"arm64", "x64"}

    def test_package_json_pkgbuild_installers_ci_and_gitignore(self) -> None:
        pkg = json.loads(_read_text(DESKTOP_DIR / "package.json"))
        assert pkg["main"] == "src/main/index.js"
        assert pkg.get("desktopName") == "chronox-finance.desktop"
        assert "demo" not in pkg["scripts"], "User-facing desktop demo script must remain purged"
        for script_name in (
            "start",
            "dev",
            "icons",
            "build:backend",
            "pack",
            "dist:deb",
            "dist:pacman",
            "dist:appimage",
            "dist:linux",
            "dist:win",
            "dist:mac",
            "check",
        ):
            assert script_name in pkg["scripts"], f"Missing npm script: {script_name}"
        assert "watch-folder.js" in pkg["scripts"]["check"]
        assert (DESKTOP_SRC / "main" / "watch-folder.js").is_file()
        assert "electron" in pkg["devDependencies"]
        assert "electron-builder" in pkg["devDependencies"]

        # PyInstaller backend.spec bundles cryptography + connectors + imaplib
        backend_spec = _read_text(DESKTOP_DIR / "packaging" / "backend.spec")
        for token in (
            'collect_dynamic_libs("cryptography")',
            'collect_data_files("cryptography")',
            '"cryptography.x509"',
            '"cryptography.x509.oid"',
            '"cryptography.hazmat.primitives.ciphers.aead"',
            '"cryptography.hazmat.primitives.asymmetric.ed25519"',
            '"cryptography.hazmat.primitives.asymmetric.rsa"',
            '"imaplib"',
            'collect_submodules("chronox_finance.connectors")',
        ):
            assert token in backend_spec, f"backend.spec missing required bundle token: {token}"

        # Arch PKGBUILD & desktop entry
        pkgbuild = _read_text(DESKTOP_DIR / "packaging" / "arch" / "PKGBUILD")
        assert f"pkgver={pkg['version']}" in pkgbuild
        assert "arch=('x86_64' 'aarch64')" in pkgbuild
        assert "/opt/${pkgname}" in pkgbuild
        assert "/usr/bin/${pkgname}" in pkgbuild
        assert "/home/" not in pkgbuild

        desktop_entry = _read_text(DESKTOP_DIR / "packaging" / "arch" / "chronox-finance.desktop")
        assert "[Desktop Entry]" in desktop_entry
        assert "StartupWMClass=chronox-finance" in desktop_entry
        assert "Finance" in desktop_entry

        # Release CI matrix
        ci = yaml.safe_load(_read_text(REPO_ROOT / ".github" / "workflows" / "release-desktop.yml"))
        assert isinstance(ci, dict)
        jobs = ci["jobs"]
        assert {"smoke-check", "build-linux", "build-windows", "build-macos", "release"} <= set(jobs.keys())
        linux_arches = {item["arch"] for item in jobs["build-linux"]["strategy"]["matrix"]["include"]}
        win_arches = {item["arch"] for item in jobs["build-windows"]["strategy"]["matrix"]["include"]}
        mac_arches = {item["arch"] for item in jobs["build-macos"]["strategy"]["matrix"]["include"]}
        assert linux_arches == {"x64", "arm64"}
        assert win_arches == {"x64", "arm64"}
        assert mac_arches == {"x64", "arm64"}

        # Windows PowerShell installer exists with expected switches (and zero Demo switch)
        ps1 = _read_text(REPO_ROOT / "scripts" / "install-windows.ps1")
        assert "BuildInstaller" in ps1 and "Launch" in ps1 and "arm64" in ps1.lower()
        assert "[switch]$Demo" not in ps1

        # Root .gitignore ignores build output/node_modules while keeping desktop/build/icons tracked
        gitignore = _read_text(REPO_ROOT / ".gitignore")
        assert "desktop/dist/" in gitignore or "dist/" in gitignore
        assert "node_modules/" in gitignore
        assert "!desktop/build/icons/**" in gitignore

    def test_backend_sidecar_entrypoint_resolves_web_dir_and_parses_flags(self, tmp_path: Path) -> None:
        spec = importlib.util.spec_from_file_location(
            "chronox_backend_entry", DESKTOP_DIR / "scripts" / "backend_entry.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        web_dir = module.resolve_web_dir()
        assert web_dir is not None and (web_dir / "index.html").is_file()

        sample_db = str(tmp_path / "sidecar_test.sqlite3")
        args = module.parse_args(
            ["--host", "127.0.0.1", "--port", "19421", "--db", sample_db, "--mode", "hybrid", "--provider", "auto"]
        )
        assert args.host == "127.0.0.1"
        assert args.port == 19421
        assert args.db == sample_db
        assert args.mode == "hybrid"
        assert args.provider == "auto"

        # Purged --demo flag must be rejected
        with pytest.raises(SystemExit):
            module.parse_args(["--host", "127.0.0.1", "--port", "19421", "--demo"])
