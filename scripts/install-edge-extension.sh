#!/usr/bin/env bash
# =============================================================================
# ChronoX Credit Companion — Microsoft Edge / Chromium Installer (Linux)
#
# 1. Generates crisp 16/48/128 PNG icons in `desktop/credit-companion-extension/icons/`.
# 2. Creates or reuses an owner-only RSA-2048 key (`~/.config/ChronoX Finance/edge-extension.pem`),
#    embeds its public DER key in `manifest.json` so the 32-char Chromium Extension ID
#    is 100% deterministic across unpacked and packed `.crx` installs.
# 3. Registers the zero-token Native Messaging Host (`com.chronox.credit_bridge.json`)
#    across all Microsoft Edge (Dev / Beta / Stable / XRDP) and Chromium user profiles.
# 4. Packs `chronox-credit-companion.crx` using an isolated temporary `--user-data-dir`
#    (never disturbing your running Edge session) and stages `External Extensions/<ID>.json`.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_DIR="$REPO_ROOT/desktop/credit-companion-extension"
NATIVE_HOST_PY="$REPO_ROOT/scripts/chronox_native_host.py"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
DATA_DIR="$HOME/.config/ChronoX Finance"
KEY_PEM="$DATA_DIR/edge-extension.pem"
CRX_OUT="$DATA_DIR/chronox-credit-companion.crx"
HOST_LAUNCHER="$DATA_DIR/chronox-native-host-launcher.sh"

mkdir -m 700 -p "$DATA_DIR"
chmod +x "$NATIVE_HOST_PY"

# Create a machine-local launcher wrapper outside git so scripts/chronox_native_host.py stays portable
cat >"$HOST_LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYTHON_BIN" "$NATIVE_HOST_PY" "\$@"
EOF
chmod 700 "$HOST_LAUNCHER"

# Generate RSA-2048 keypair once (0600, never stored in git)
if [[ ! -f "$KEY_PEM" ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$KEY_PEM" >/dev/null 2>&1
  chmod 600 "$KEY_PEM"
fi

# Compute deterministic Chromium 32-char Extension ID + Base64 public key & write manifest + icons
EXT_ID="$("$PYTHON_BIN" - "$EXT_DIR" "$KEY_PEM" <<'PY'
import base64
import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ext_dir = Path(sys.argv[1])
pem_path = Path(sys.argv[2])

der_pub = subprocess.check_output(
    ["openssl", "rsa", "-in", str(pem_path), "-pubout", "-outform", "DER"],
    stderr=subprocess.DEVNULL,
)
pub_b64 = base64.b64encode(der_pub).decode("ascii")
hex32 = hashlib.sha256(der_pub).hexdigest()[:32]
alpha_map = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
ext_id = hex32.translate(alpha_map)

# Generate crisp zero-dependency PNG icons (16, 48, 128)
icons_dir = ext_dir / "icons"
icons_dir.mkdir(parents=True, exist_ok=True)


def make_png(size: int) -> bytes:
    rows = []
    cx = (size - 1) / 2.0
    cy = (size - 1) / 2.0
    r_outer = size * 0.36
    r_inner = size * 0.24
    for y in range(size):
        row = bytearray([0])  # filter byte 0
        for x in range(size):
            dx = x - cx
            dy = y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            # Dark industrial rounded tile
            bg = (11, 15, 20, 255)
            # Emerald ring + cyan clock hand
            if r_inner <= dist <= r_outer:
                px = (16, 185, 129, 255)
            elif abs(dx) <= max(1.0, size * 0.06) and -size * 0.20 <= dy <= size * 0.03:
                px = (6, 182, 212, 255)
            elif 0 <= dx <= size * 0.18 and abs(dy - dx * 0.55) <= max(1.0, size * 0.055):
                px = (6, 182, 212, 255)
            else:
                px = bg
            row.extend(px)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


for sz in (16, 48, 128):
    (icons_dir / f"icon{sz}.png").write_bytes(make_png(sz))

manifest = {
    "manifest_version": 3,
    "name": "ChronoX Credit Companion",
    "version": "1.1.0",
    "description": "Zero-config local bridge syncing Credit Karma (TransUnion + Equifax) and Experian into ChronoX Finance.",
    "key": pub_b64,
    "permissions": ["nativeMessaging", "activeTab", "scripting", "storage", "tabs"],
    "host_permissions": [
        "https://*.creditkarma.com/*",
        "https://*.experian.com/*",
        "http://127.0.0.1:8749/*",
    ],
    "background": {
        "service_worker": "service_worker.js",
    },
    "action": {
        "default_title": "ChronoX Credit Companion",
        "default_popup": "popup.html",
        "default_icon": {
            "16": "icons/icon16.png",
            "48": "icons/icon48.png",
            "128": "icons/icon128.png",
        },
    },
    "icons": {
        "16": "icons/icon16.png",
        "48": "icons/icon48.png",
        "128": "icons/icon128.png",
    },
    "content_scripts": [
        {
            "matches": [
                "https://www.creditkarma.com/*",
                "https://creditkarma.com/*",
                "https://usa.experian.com/*",
            ],
            "js": ["extractor.js", "content_script.js"],
            "run_at": "document_idle",
        }
    ],
}
(ext_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(ext_id)
PY
)"

# Register Native Messaging Host in all Edge & Chromium profiles (effective immediately, zero browser restart needed)
BROWSER_CONFIG_DIRS=(
  "$HOME/.config/microsoft-edge-dev"
  "$HOME/.config/edge-dev-xrdp"
  "$HOME/.config/microsoft-edge"
  "$HOME/.config/microsoft-edge-beta"
  "$HOME/.config/chromium"
  "$HOME/.config/google-chrome"
)

for cfg_dir in "${BROWSER_CONFIG_DIRS[@]}"; do
  nm_dir="$cfg_dir/NativeMessagingHosts"
  mkdir -p "$nm_dir"
  cat >"$nm_dir/com.chronox.credit_bridge.json" <<EOF
{
  "name": "com.chronox.credit_bridge",
  "description": "ChronoX Finance Local Credit Companion Native Messaging Host",
  "path": "$HOST_LAUNCHER",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXT_ID/"
  ]
}
EOF
done

# Pack signed .crx3 using an isolated temp profile so running Edge windows are never touched
EDGE_BIN=""
for cand in /usr/bin/microsoft-edge-dev /usr/bin/microsoft-edge-stable /usr/bin/microsoft-edge /usr/bin/microsoft-edge-beta /usr/bin/chromium; do
  if [[ -x "$cand" ]]; then
    EDGE_BIN="$cand"
    break
  fi
done

if [[ -n "$EDGE_BIN" ]]; then
  TMP_PACK_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_PACK_DIR"' EXIT
  cp -a "$EXT_DIR" "$TMP_PACK_DIR/ext"
  cp "$KEY_PEM" "$TMP_PACK_DIR/ext.pem"
  "$EDGE_BIN" \
    --headless=new \
    --no-sandbox \
    --user-data-dir="$TMP_PACK_DIR/profile" \
    --pack-extension="$TMP_PACK_DIR/ext" \
    --pack-extension-key="$TMP_PACK_DIR/ext.pem" >/dev/null 2>&1 || true

  if [[ -f "$TMP_PACK_DIR/ext.crx" ]]; then
    cp "$TMP_PACK_DIR/ext.crx" "$CRX_OUT"
    chmod 644 "$CRX_OUT"
    for cfg_dir in "$HOME/.config/microsoft-edge-dev" "$HOME/.config/edge-dev-xrdp" "$HOME/.config/microsoft-edge" "$HOME/.config/microsoft-edge-beta"; do
      ext_json_dir="$cfg_dir/External Extensions"
      mkdir -p "$ext_json_dir"
      cat >"$ext_json_dir/$EXT_ID.json" <<EOF
{
  "external_crx": "$CRX_OUT",
  "external_version": "1.1.0"
}
EOF
    done
  fi
fi

echo "Extension ID:     $EXT_ID"
echo "Unpacked Source:  $EXT_DIR"
echo "Packed CRX:       $CRX_OUT"
echo "Native Host:      $NATIVE_HOST_PY"
