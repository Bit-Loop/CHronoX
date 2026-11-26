# ChronoX Credit Companion (Edge / Chrome MV3 Extension)

Direct, zero-token, zero-cloud local bridge between your real browser session (**Credit Karma** for TransUnion + Equifax VantageScore 3.0, and **Experian** for FICO Score 8) and ChronoX's local SQLite ledger.

## How It Works

1. **You own auth & 2FA in Edge:** Sign into `creditkarma.com` or `usa.experian.com` normally in Microsoft Edge (completing SMS/authenticator/passkey 2FA in your real browser tab). The extension never runs on login/2FA screens and never touches passwords or cookies.
2. **1-Click Capture (Floating Pill or Toolbar Icon):**
   - Click the floating **`Sync Credit Page → ChronoX`** pill at the bottom-right of any Credit Karma / Experian report or accounts page, **or**
   - Click the ChronoX toolbar icon and press **Capture & Sync Open Tab → ChronoX**.
3. **Direct Native Messaging (`com.chronox.credit_bridge`):**
   - Edge spawns `scripts/chronox_native_host.py` over local `stdin`/`stdout` (restricted strictly to this extension's deterministic RSA public-key ID) and upserts scores, tradelines, hard inquiries, collections, and public records straight into `~/.config/ChronoX Finance/finance.sqlite3`.

## Install / Update in Microsoft Edge

```bash
./scripts/install-edge-extension.sh
```
