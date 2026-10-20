// Runs before the copied web UI. Publishes the extension's api() bridge as
// window.ExtApi, shows the detected org, then loads the UI's classic scripts
// in order (they are same-package files, so the extension CSP allows them).

import { BACKEND_URL } from "../lib/config.js";
import { describeActiveOrg } from "./org-client.js";
import { rawFetch, request, loadStoredKey, saveStoredKey } from "./api.js";

window.ExtApi = { request, rawFetch, loadStoredKey, saveStoredKey, BACKEND_URL };

const el = (id) => document.getElementById(id);

function render(org) {
  const bar = el("extOrgBar");
  const text = el("extOrgText");
  bar.dataset.status = org.status;
  const messages = {
    ok: `Org: ${org.apiHost}`,
    "not-salesforce": "Open a Salesforce tab (Lightning, Setup or a My Domain page) to connect.",
    "no-tab": "Open a Salesforce tab to connect.",
    "no-session": `Not logged in on ${org.host || "this tab"}. Log in to Salesforce, then switch tabs.`,
    error: "Could not read the active tab.",
  };
  text.textContent = messages[org.status] || messages.error;
  el("extOrgDetail").textContent =
    org.status === "ok" ? `${org.orgId} · tab: ${org.host} · session from ${org.sidDomain}` : "";
  el("extBackend").textContent = BACKEND_URL;
}

async function refresh() {
  try {
    render(await describeActiveOrg());
  } catch {
    render({ status: "error" });
  }
}

refresh();
chrome.tabs.onActivated.addListener(refresh);
chrome.tabs.onUpdated.addListener((_id, change) => {
  if (change.url || change.status === "complete") refresh();
});
chrome.windows.onFocusChanged.addListener(refresh);

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`failed to load ${src}`));
    document.body.appendChild(s);
  });
}

(async () => {
  for (const src of ["ui/vendor/mermaid.min.js", "ui/lwc-preview.js", "ui/app.js"]) {
    await loadScript(src);
  }
})().catch((e) => {
  el("extOrgText").textContent = String(e.message || e);
});
