// Manual end-to-end check of the unpacked extension against the scratch org
// `forge-scratch` (expires 2026-10-24). Not run by CI.
//
//   node extension/tools/manual_cdp_test.mjs            # headless
//   HEADED=1 node extension/tools/manual_cdp_test.mjs   # visible Chrome
//
// What it does (no dependencies, Node 20+):
//   1. `sf org open --url-only -o forge-scratch` -> one-time login URL (only
//      forge-scratch is ever touched; the URL/token is never printed).
//   2. Starts Chrome with a throwaway profile, --remote-debugging-pipe and
//      --enable-unsafe-extension-debugging, then Extensions.loadUnpacked.
//   3. Logs in on a tab, opens the side panel page as a second tab, keeps the
//      Salesforce tab active, and runs inside the panel page:
//        - api("api/flows"), api("api/apex-classes"), ... through the real
//          ExtApi.request router (served by org-client.js with the browser sid)
//        - validate (SOAP deploy checkOnly) of a tiny CustomObject bundle,
//          through the real router with a stubbed backend bundle endpoint.
//      Validate is checkOnly: nothing is deployed.
//
// Chrome path: CHROME env var, else the default Windows/macOS/Linux location.

import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ORG_ALIAS = "forge-scratch";
const EXT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const mask = (s) =>
  String(s)
    .replace(/([?&](?:sid|otp|token|access_token)=)[^&\s"']+/gi, "$1***")
    .replace(/00D[A-Za-z0-9]{12,15}![A-Za-z0-9._]{20,}/g, "00D***!***")
    .replace(/Bearer\s+\S+/g, "Bearer ***");
const log = (...a) => console.log(mask(a.join(" ")));

function chromePath() {
  const candidates = [
    process.env.CHROME,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
  ].filter(Boolean);
  const hit = candidates.find((c) => existsSync(c));
  if (!hit) throw new Error("Chrome not found; set CHROME=/path/to/chrome");
  return hit;
}

function frontdoorUrl() {
  const out = execFileSync(process.platform === "win32" ? "sf.cmd" : "sf", ["org", "open", "--url-only", "-o", ORG_ALIAS, "--json"], { encoding: "utf8", shell: process.platform === "win32" });
  const url = JSON.parse(out).result?.url;
  if (!url) throw new Error(`sf org open returned no url for ${ORG_ALIAS} (expired? run: sf org list)`);
  return url;
}

class Cdp {
  constructor(proc) {
    this.id = 0;
    this.pending = new Map();
    this.buf = "";
    this.out = proc.stdio[3];
    proc.stdio[4].on("data", (d) => {
      this.buf += d.toString("utf8");
      let i;
      while ((i = this.buf.indexOf("\0")) >= 0) {
        const msg = JSON.parse(this.buf.slice(0, i));
        this.buf = this.buf.slice(i + 1);
        const p = msg.id && this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          msg.error ? p.reject(new Error(`${p.method}: ${msg.error.message}`)) : p.resolve(msg.result);
        }
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.out.write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + "\0");
    });
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const login = frontdoorUrl();
  const profile = mkdtempSync(path.join(tmpdir(), "forge-ext-"));
  const args = [
    `--user-data-dir=${profile}`, "--remote-debugging-pipe", "--enable-unsafe-extension-debugging",
    "--no-first-run", "--no-default-browser-check", "--window-size=1200,900",
    ...(process.env.HEADED ? [] : ["--headless=new"]), "about:blank",
  ];
  const proc = spawn(chromePath(), args, { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });
  const cdp = new Cdp(proc);
  let failures = 0;
  const check = (name, ok, detail = "") => {
    if (!ok) failures++;
    log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
  };

  try {
    const { id: extId } = await cdp.send("Extensions.loadUnpacked", { path: EXT_DIR });
    log("extension loaded:", extId);

    const attach = async (targetId) =>
      (await cdp.send("Target.attachToTarget", { targetId, flatten: true })).sessionId;
    const evalIn = async (sessionId, expression) => {
      const r = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };

    // Salesforce tab: log in with the one-time URL, wait for Lightning/Setup.
    const sf = await cdp.send("Target.createTarget", { url: login });
    const sfSession = await attach(sf.targetId);
    let host = "";
    for (let i = 0; i < 60 && !/salesforce|force\.com/.test(host); i++) {
      await sleep(1000);
      host = await evalIn(sfSession, "location.hostname").catch(() => "");
    }
    check("logged in on a Salesforce host", /salesforce|force\.com/.test(host), host);
    await sleep(4000); // let Lightning settle and set its cookies

    // Panel page as its own tab, then bring the Salesforce tab back to front:
    // the service worker answers for the *active* tab.
    const panel = await cdp.send("Target.createTarget", { url: `chrome-extension://${extId}/panel/index.html`, background: true });
    const panelSession = await attach(panel.targetId);
    await cdp.send("Target.activateTarget", { targetId: sf.targetId });
    await sleep(3000);

    const desc = await evalIn(panelSession, `chrome.runtime.sendMessage({type:"org:describe"})`);
    check("service worker sees an org for the active tab", desc?.ok && desc.org?.status === "ok", desc?.org?.status);

    const run = (code) => evalIn(panelSession, `(async()=>{${code}})()`);
    const lists = await run(`
      const out = {};
      for (const [p, k] of [["api/flows","flows"],["api/apex-classes","classes"],["api/apex-triggers","triggers"],
        ["api/lwc-components","components"],["api/permission-sets","permission_sets"],["api/custom-objects","objects"],
        ["api/custom-fields","fields"],["api/prompt-templates","templates"]]) {
        try { const r = await window.ExtApi.request(p, {}); out[p] = Array.isArray(r[k]) ? r[k].length : "bad shape"; }
        catch (e) { out[p] = "ERR " + e.message.slice(0, 200); }
      }
      return out;`);
    for (const [p, v] of Object.entries(lists)) check(`list ${p}`, typeof v === "number", `-> ${v}`);

    const validate = await run(`
      const { createRouter, getOrgSession } = await import("./org-client.js");
      const { Org } = await import("./org/sfdc.js");
      const bundle = { version: 1, api_version: "64.0",
        files: { "objects/Forge_Ext_Check__c.object":
          '<?xml version="1.0" encoding="UTF-8"?><CustomObject xmlns="http://soap.sforce.com/2006/04/metadata"><label>Forge Ext Check</label><pluralLabel>Forge Ext Checks</pluralLabel><deploymentStatus>Deployed</deploymentStatus><sharingModel>ReadWrite</sharingModel><nameField><label>Name</label><type>Text</type></nameField></CustomObject>' },
        types: { CustomObject: ["Forge_Ext_Check__c"] } };
      const r = createRouter({
        newOrg: async () => { const o = new Org({ getSession: getOrgSession }); await o.session(); return o; },
        backend: async (p) => p.endsWith("/bundle") ? bundle : {},
      });
      await r.orgMethodFor("api/validate/start")({ session_id: "manual" });
      for (let i = 0; i < 60; i++) {
        const s = await r.orgMethodFor("api/validate/status?session_id=manual")();
        if (s.done) return s;
        await new Promise((x) => setTimeout(x, 1500));
      }
      return { timeout: true };`);
    check("validate (checkOnly) via router", validate?.done && validate.success === true,
      `status=${validate?.status} failures=${JSON.stringify(validate?.failures || []).slice(0, 300)}`);

    const stub = await run(`try { await window.ExtApi.request("api/plan/agent/publish/start", {}); return "no error"; } catch (e) { return e.status + " " + e.message; }`);
    check("agent publish is stubbed", /^501 .*not available in the extension yet/.test(stub), stub);
  } finally {
    proc.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* profile may still be locked */ }
  }
  log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(mask(e.stack || e)); process.exit(2); });
