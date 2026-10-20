// Finds the Salesforce session id (`sid` cookie) for the org a tab is on.
//
// Same idea as Salesforce Inspector Reloaded: a tab may sit on a Lightning,
// Setup or Visualforce host, each of which carries its own `sid`, and the
// browser may hold sessions for several orgs at once. The cookie that is
// right for API calls is the one that
//   1. belongs to the same org as the tab (the part of the value before "!",
//      the 00D... org id, matches), and
//   2. is scoped to the API host (acme.my.salesforce.com) when that exists.
// The session id is only ever handed to extension pages and is never sent to
// the hosted backend. See docs/store/ARCHITECTURE.md.
//
// NOT YET VERIFIED: that a Lightning/Setup `sid` is accepted by the Metadata
// SOAP and REST APIs on the API host. That is what the ext-spike work proves.

import { isSalesforceHost, orgIdFromSid, toApiHost } from "../lib/hosts.js";

const bare = (domain) => String(domain || "").replace(/^\./, "");

async function cookieStoreIdForTab(tabId) {
  try {
    const stores = await chrome.cookies.getAllCookieStores();
    const store = stores.find((s) => s.tabIds.includes(tabId));
    return store ? store.id : undefined;
  } catch {
    return undefined;
  }
}

export async function describeTab(tab) {
  if (!tab || !tab.url) return { status: "no-tab" };
  let url;
  try {
    url = new URL(tab.url);
  } catch {
    return { status: "no-tab" };
  }
  if (url.protocol !== "https:" || !isSalesforceHost(url.hostname)) {
    return { status: "not-salesforce", host: url.hostname };
  }
  const apiHost = toApiHost(url.hostname);
  const storeId = await cookieStoreIdForTab(tab.id);
  const own = await chrome.cookies.get({ url: `https://${url.hostname}/`, name: "sid", storeId });
  const orgId = own ? orgIdFromSid(own.value) : null;
  if (!own || !orgId) return { status: "no-session", host: url.hostname, apiHost };

  const candidates = (await chrome.cookies.getAll({ name: "sid", secure: true, storeId }))
    .filter((c) => c.value.startsWith(orgId + "!"))
    .filter((c) => isSalesforceHost(bare(c.domain)));
  const exact = candidates.find((c) => bare(c.domain) === apiHost);
  const chosen = exact || candidates.find((c) => bare(c.domain) === url.hostname) || own;
  return {
    status: "ok",
    host: url.hostname,
    apiHost,
    orgId,
    sidDomain: bare(chosen.domain),
    sidIsApiHost: Boolean(exact),
    // Stripped by publicView() before anything is shown or logged.
    _sid: chosen.value,
  };
}

export function publicView(info) {
  const { _sid, ...rest } = info;
  return rest;
}
