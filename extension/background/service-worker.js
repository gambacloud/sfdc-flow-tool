// Service worker: opens the side panel and answers questions about the org
// of the active tab. Nothing here talks to the hosted backend.

import { describeTab, publicView } from "./sid.js";

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

// Only this extension's own pages may ask for anything.
function fromOwnPage(sender) {
  return Boolean(
    sender && sender.id === chrome.runtime.id && typeof sender.url === "string" &&
      sender.url.startsWith(`chrome-extension://${chrome.runtime.id}/`),
  );
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!fromOwnPage(sender)) return false;
  (async () => {
    try {
      if (msg && msg.type === "org:describe") {
        sendResponse({ ok: true, org: publicView(await describeTab(await activeTab())) });
      } else if (msg && msg.type === "org:getSession") {
        // For the future org client (list/retrieve/deploy) running in an
        // extension page: the API host plus the session id.
        const info = await describeTab(await activeTab());
        if (info.status !== "ok") sendResponse({ ok: false, error: info.status });
        else sendResponse({ ok: true, apiHost: info.apiHost, orgId: info.orgId, sid: info._sid });
      } else {
        sendResponse({ ok: false, error: "unknown-message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true; // async response
});
