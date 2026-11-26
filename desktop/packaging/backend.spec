# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller specification for the ChronoX Finance zero-dependency sidecar.

Produces:
  desktop/build/backend/chronox-backend      (Linux / macOS)
  desktop/build/backend/chronox-backend.exe  (Windows)

Defaults to fast-booting `--onedir` layout (`backend/chronox-backend(.exe)` +
`backend/_internal/...`) to avoid temporary-directory extraction latency and
Windows Defender false positives. Set `CHRONOX_PYINSTALLER_ONEFILE=1` to build
a single-file binary instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
DESKTOP_DIR = SPEC_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent
ENTRY_SCRIPT = DESKTOP_DIR / "scripts" / "backend_entry.py"
WEB_ASSETS_DIR = REPO_ROOT / "chronox_finance" / "web"

ONEFILE = os.getenv("CHRONOX_PYINSTALLER_ONEFILE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

binaries = []
binaries += collect_dynamic_libs("cryptography")

datas = [
    (str(WEB_ASSETS_DIR), "chronox_finance/web"),
]
datas += collect_data_files("certifi")
datas += collect_data_files("cryptography")

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    "h11",
    "httpx",
    "httpcore",
    "certifi",
    "dotenv",
    "multipart",
    "python_multipart",
    "pydantic",
    "pydantic_core",
    "starlette.routing",
    "starlette.middleware",
    "starlette.staticfiles",
    "email.mime.multipart",
    "email.mime.text",
    "email.header",
    "email.policy",
    "imaplib",
    "cryptography",
    "cryptography.exceptions",
    "cryptography.x509",
    "cryptography.x509.oid",
    "cryptography.hazmat.bindings._rust",
    "cryptography.hazmat.backends.openssl",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.asymmetric.utils",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cffi",
    "_cffi_backend",
]
hiddenimports += collect_submodules("chronox_finance")
hiddenimports += collect_submodules("chronox_finance.connectors")

# Exclude heavy optional trading / ML / test stacks not used by ChronoX Finance
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "sklearn",
    "scipy",
    "pandas",
    "numpy",
    "matplotlib",
    "tkinter",
    "PIL",
    "psycopg2",
    "redis",
    "aiohttp",
    "pytest",
    "ruff",
    "IPython",
    "notebook",
]

block_cipher = None

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="chronox-backend",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="chronox-backend",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="backend",
    )
