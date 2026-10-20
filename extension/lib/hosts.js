// Pure host-name helpers (no chrome.* calls) so they can be unit-tested.

import { SALESFORCE_SUFFIXES } from "./config.js";

export function isSalesforceHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  return SALESFORCE_SUFFIXES.some((s) => h === s || h.endsWith("." + s));
}

// The tab you are looking at is rarely the host the API wants. A Lightning
// tab (acme.lightning.force.com), Setup (acme.my.salesforce-setup.com) and
// Visualforce (acme--c.vf.force.com) all belong to the same org as
// acme.my.salesforce.com, which is the host that serves /services/data and
// /services/Soap. Returns the best guess, or null for anything that is not
// a Salesforce host. Unrecognised Salesforce hosts are returned unchanged.
export function toApiHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  if (!isSalesforceHost(h)) return null;
  let m;
  // acme.lightning.force.com / acme.sandbox.lightning.force.com
  if ((m = h.match(/^(.+)\.lightning\.force\.com$/))) return `${m[1]}.my.salesforce.com`;
  // acme.my.salesforce-setup.com
  if ((m = h.match(/^(.+)\.salesforce-setup\.com$/))) {
    return `${m[1].replace(/\.my$/, "")}.my.salesforce.com`;
  }
  // acme--c.vf.force.com / acme--c.sandbox.vf.force.com
  if ((m = h.match(/^([^.]+?)--[^.]+\.((?:[a-z0-9-]+\.)*)vf\.force\.com$/))) {
    return `${m[1]}.${m[2]}my.salesforce.com`;
  }
  return h;
}

// The org id is the part of a `sid` before "!". Cookies for the same org
// share it; cookies for another org open in another tab do not.
export function orgIdFromSid(sidValue) {
  const s = String(sidValue || "");
  const i = s.indexOf("!");
  return i > 0 ? s.slice(0, i) : null;
}
