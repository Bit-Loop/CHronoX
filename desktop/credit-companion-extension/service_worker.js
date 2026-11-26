"use strict";

const NATIVE_HOST_NAME = "com.chronox.credit_bridge";
const FALLBACK_HTTP_URL = "http://127.0.0.1:8749";

function sendNative(message) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message, (response) => {
        const lastErr = chrome.runtime.lastError;
        if (lastErr) {
          reject(new Error(lastErr.message || "Native host unavailable"));
          return;
        }
        resolve(response || { ok: false, error: "Empty native host response" });
      });
    } catch (err) {
      reject(err);
    }
  });
}

async function fallbackHttp(action, payload) {
  const cfg = await chrome.storage.local.get(["bridgeUrl", "bridgeToken"]);
  const baseUrl = String(cfg.bridgeUrl || FALLBACK_HTTP_URL).replace(/\/+$/, "");
  if (action === "ping") {
    const r = await fetch(`${baseUrl}/health`);
    const data = await r.json();
    return { ok: r.ok, mode: "http_bridge", summary: data };
  }
  if (!cfg.bridgeToken) {
    throw new Error(
      "Native host not registered. Run `./scripts/install-edge-extension.sh` in ChronoX."
    );
  }
  const r = await fetch(`${baseUrl}/credit/ingest`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${cfg.bridgeToken}`,
    },
    body: JSON.stringify(payload || {}),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return { ok: true, mode: "http_bridge", ...data };
}

async function dispatchBridge(action, payload = null) {
  try {
    return await sendNative({ action, payload });
  } catch (nativeErr) {
    try {
      return await fallbackHttp(action, payload);
    } catch (_httpErr) {
      return {
        ok: false,
        error: nativeErr.message || String(nativeErr),
      };
    }
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  if (msg.type === "CHRONOX_PING") {
    dispatchBridge("ping").then(sendResponse);
    return true;
  }
  if (msg.type === "CHRONOX_SYNC_CREDIT") {
    dispatchBridge("sync_credit", msg.payload || {}).then((res) => {
      if (res && res.ok) {
        chrome.action.setBadgeBackgroundColor({ color: "#10b981" });
        chrome.action.setBadgeText({ text: "OK" });
        setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
      }
      sendResponse(res);
    });
    return true;
  }
  return false;
});
