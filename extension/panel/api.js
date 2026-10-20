// Replacement for the web UI's api() helper (flowtool/static/app.js:353).
// Same contract: resolves to parsed JSON, rejects with an Error carrying
// `.status`. Paths are the UI's own ("api/design/start"); org-touching ones
// are routed to org-client.js, the rest go to the hosted backend.

import { BACKEND_URL } from "../lib/config.js";
import { orgMethodFor } from "./org-client.js";

const CLIENT_ID = (() => {
  const key = "flowtoolClientId";
  try {
    let id = sessionStorage.getItem(key);
    if (!id) {
      id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      sessionStorage.setItem(key, id);
    }
    return id;
  } catch {
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
})();

// BYO model-provider keys live in chrome.storage.local, never on our server.
// The UI still passes `api_key` in request bodies exactly as the web version
// does; this only supplies the value the UI field is prefilled from.
export async function loadStoredKey(provider) {
  const k = `apiKey.${provider}`;
  const got = await chrome.storage.local.get(k);
  return got[k] || "";
}
export async function saveStoredKey(provider, value) {
  const k = `apiKey.${provider}`;
  if (value) await chrome.storage.local.set({ [k]: value });
  else await chrome.storage.local.remove(k);
}

function backendUrl(path) {
  return `${BACKEND_URL}/${String(path).replace(/^\/+/, "")}`;
}

// Raw fetch to the backend (used where the UI wants a Response, e.g. XML
// downloads). Never attach Salesforce credentials here.
export function rawFetch(path, init) {
  return fetch(backendUrl(path), init);
}

export async function request(path, body) {
  const orgMethod = orgMethodFor(path);
  if (orgMethod) return orgMethod(body);

  const response = await fetch(backendUrl(path), {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = { detail: text };
  }
  if (!response.ok) {
    const error = new Error(payload.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}
