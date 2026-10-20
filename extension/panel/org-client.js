// Serves every Salesforce-touching call of the web UI inside the extension.
//
// api.js asks orgMethodFor(path): a function means "answer this locally"
// (list / retrieve / validate / deploy against the org with the browser
// session id), null means "send it to the hosted backend". Responses keep the
// shapes server.py returned, including the start + /status polling pairs, so
// panel/ui/app.js is unchanged. See docs/store/ARCHITECTURE.md 3.1 and the
// backend contract in extension/README.md.
//
// The session id never leaves this file's callees: backend() is only ever
// handed bundles, IR/XML, failure strings and flow XML.

import { Org, SfError, failureStrings } from "./org/sfdc.js";
import { bytesToBase64 } from "./org/zip.js";

export class NotAvailableError extends Error {
  constructor(what) {
    super(`${what} is not available in the extension yet.`);
    this.name = "NotAvailableError";
    this.status = 501;
  }
}

// { apiHost, orgId, sid } for the active tab; throws with a short reason.
export async function getOrgSession() {
  const res = await chrome.runtime.sendMessage({ type: "org:getSession" });
  if (!res || !res.ok) throw new SfError(`No Salesforce session: ${(res && res.error) || "no response"}. Open a logged-in Salesforce tab.`, 400);
  return res;
}

export async function describeActiveOrg() {
  const res = await chrome.runtime.sendMessage({ type: "org:describe" });
  if (!res || !res.ok) return { status: "error", error: (res && res.error) || "no response" };
  return res.org;
}

// Which component type a bundle's package.xml type means for setup links.
const SETUP_KIND = {
  Flow: "flow", ApexClass: "apex", ApexTrigger: "trigger", CustomObject: "object",
  CustomField: "field", LightningComponentBundle: "lwc", PermissionSet: "permission_set",
};

export function componentsFromTypes(types) {
  const out = [];
  for (const [type, members] of Object.entries(types || {})) {
    const kind = SETUP_KIND[type];
    if (!kind) continue;
    for (const m of members) {
      if (kind === "field") {
        const [object, name] = m.split(".");
        out.push({ type: kind, name, object, step: m });
      } else out.push({ type: kind, name: m, step: m });
    }
  }
  return out;
}

const enc = encodeURIComponent;

// deps: newOrg() -> Org; backend(path, body?) -> Promise<json>; newId() -> string
export function createRouter({ newOrg, backend, newId = () => Math.random().toString(36).slice(2) + Date.now().toString(36) }) {
  const jobs = new Map();

  function startJob(key, fn) {
    const job = { done: false };
    jobs.set(key, job);
    fn().then(
      (value) => { job.done = true; job.value = value; },
      (error) => { job.done = true; job.error = error; },
    );
  }
  function jobStatus(key, missing) {
    const job = jobs.get(key);
    if (!job) throw Object.assign(new Error(missing), { status: 400 });
    if (!job.done) return { done: false };
    jobs.delete(key);
    if (job.error) {
      job.error.status = job.error.status || 502;
      throw job.error;
    }
    return job.value;
  }

  const lists = (method) => async () => (await newOrg())[method]();
  // Best effort: the backend keeps last_failures for the repair loop; losing
  // this call must never fail a validate/deploy that already ran.
  const report = async (path, payload) => {
    try { return await backend(path, payload); } catch { return null; }
  };

  function deployRoutes(plan) {
    const sessionPath = (id) => (plan ? `api/plan/session/${enc(id)}` : `api/session/${enc(id)}`);
    const run = (checkOnly) => async (body) => {
      const id = body && body.session_id;
      if (!id) throw Object.assign(new Error("session_id is required."), { status: 400 });
      if (!checkOnly && !body.confirm) {
        throw Object.assign(new Error("Deploying needs an explicit confirmation."), { status: 400 });
      }
      const key = `${plan ? "plan" : "flow"}:${checkOnly ? "validate" : "deploy"}:${id}`;
      const running = jobs.get(key);
      if (running && !running.done) throw Object.assign(new Error("A validate/deploy is already running for this."), { status: 409 });
      startJob(key, async () => {
        const bundle = await backend(`${sessionPath(id)}/bundle`);
        const org = await newOrg();
        const apiVersion = bundle.api_version || org.apiVersion;
        const result = await org.validate({ ...bundle, api_version: apiVersion }, { checkOnly });
        let failures = failureStrings(result);
        let success = result.success;
        const templates = bundle.prompt_templates || (bundle.types && bundle.types.GenAiPromptTemplate) || [];
        if (success && !checkOnly && templates.length) {
          try { await org.activateTemplates(templates, apiVersion); } catch (e) {
            success = false;
            failures = [...failures, `[Error] ${templates.join(", ")}: Deployed, but the prompt template could not be activated (${e.message}). Activate its version in Prompt Builder before a Flow calls it.`];
          }
        }
        const posted = await report(`${sessionPath(id)}/deploy-result`, {
          success, status: result.status, failures, check_only: checkOnly, version: bundle.version ?? null,
        });
        const out = { done: true, success, status: result.status, failures };
        if (checkOnly) return { ...out, checked_version: bundle.version ?? null };
        const sess = await org.session();
        out.instance_url = `https://${sess.apiHost}`;
        if (plan) {
          out.setup_urls = {};
          if (success) {
            const comps = bundle.components || componentsFromTypes(bundle.types);
            await Promise.all(comps.map(async (c) => {
              out.setup_urls[c.step || c.name] = await org.setupUrl(c.type, c.name, c.object);
            }));
          }
          if (posted && posted.plan) out.plan = posted.plan;
        } else {
          const first = (bundle.components || componentsFromTypes(bundle.types))[0];
          out.flow_url = success && first ? await org.setupUrl(first.type, first.name, first.object) : null;
        }
        return out;
      });
      return { started: true };
    };
    const status = (what) => async (_body, q) =>
      jobStatus(`${plan ? "plan" : "flow"}:${what}:${q.get("session_id")}`,
        `No ${what === "validate" ? "validation" : "deploy"} in progress - call start first.`);
    return { run, status };
  }

  const single = deployRoutes(false);
  const planned = deployRoutes(true);

  const routes = [
    [/^api\/flows$/, "POST", lists("listFlows")],
    [/^api\/apex-classes$/, "POST", lists("listApexClasses")],
    [/^api\/apex-triggers$/, "POST", lists("listApexTriggers")],
    [/^api\/lwc-components$/, "POST", lists("listLwc")],
    [/^api\/permission-sets$/, "POST", lists("listPermissionSets")],
    [/^api\/prompt-templates$/, "POST", lists("listPromptTemplates")],
    [/^api\/custom-objects$/, "POST", lists("listCustomObjects")],
    [/^api\/custom-fields$/, "POST", lists("listCustomFields")],

    // Retrieve locally, hand the source/XML to the backend to turn into IR.
    [/^api\/import\/start$/, "POST", async (body) => {
      const job_id = newId();
      startJob(`import:${job_id}`, async () => {
        const org = await newOrg();
        const kind = body.kind || "flow";
        const files = await org.retrieveSource(kind, body.api_name);
        return backend("api/import/parse", {
          kind, api_name: body.api_name, files, provider: body.provider ?? null, model: body.model ?? null,
          effort: body.effort, api_key: body.api_key ?? null, api_version: body.api_version || org.apiVersion,
        });
      });
      return { job_id };
    }],
    [/^api\/import\/status$/, "GET", async (_b, q) => jobStatus(`import:${q.get("job_id")}`, "Unknown import job.")],

    [/^api\/survey\/start$/, "POST", async () => {
      const job_id = newId();
      startJob(`survey:${job_id}`, async () => {
        const flows = await (await newOrg()).retrieveAllFlows();
        const scored = await backend("api/survey/score", { flows });
        return { done: true, report: scored.report };
      });
      return { job_id };
    }],
    [/^api\/survey\/status$/, "GET", async (_b, q) => jobStatus(`survey:${q.get("job_id")}`, "Unknown survey job.")],

    [/^api\/org-summary\/start$/, "POST", async (body) => {
      const job_id = newId();
      startJob(`summary:${job_id}`, async () => {
        const zip = await (await newOrg()).retrieveOrgSummaryZip(body && body.groups ? body.groups : undefined);
        return { done: true, zip_base64: bytesToBase64(zip) };
      });
      return { job_id };
    }],
    [/^api\/org-summary\/status$/, "GET", async (_b, q) => jobStatus(`summary:${q.get("job_id")}`, "Unknown org summary job.")],

    [/^api\/validate\/start$/, "POST", single.run(true)],
    [/^api\/validate\/status$/, "GET", single.status("validate")],
    [/^api\/deploy\/start$/, "POST", single.run(false)],
    [/^api\/deploy\/status$/, "GET", single.status("deploy")],
    [/^api\/plan\/validate\/start$/, "POST", planned.run(true)],
    [/^api\/plan\/validate\/status$/, "GET", planned.status("validate")],
    [/^api\/plan\/deploy\/start$/, "POST", planned.run(false)],
    [/^api\/plan\/deploy\/status$/, "GET", planned.status("deploy")],

    // Follow-ups: need the SFAP JWT exchange / the portal builder ported.
    [/^api\/plan\/agent\/publish\//, null, async () => { throw new NotAvailableError("Publishing an agent"); }],
    [/^api\/portal\/(preflight|plan|apply|status)$/, null, async () => { throw new NotAvailableError("The portal builder"); }],
  ];

  // -> (body) => Promise, or null when the backend should answer.
  function orgMethodFor(path) {
    const [clean, query = ""] = String(path).replace(/^\/+/, "").split("?");
    const hit = routes.find(([re]) => re.test(clean));
    if (!hit) return null;
    const q = new URLSearchParams(query);
    return (body) => hit[2](body, q);
  }

  return { orgMethodFor };
}

let backendRequest = async () => { throw new Error("backend not configured"); };
// api.js hands over its backend request function (the only thing here that
// talks to the hosted server; it never receives credentials).
export function setBackend(fn) { backendRequest = fn; }

const defaultRouter = createRouter({
  newOrg: async () => {
    const org = new Org({ getSession: getOrgSession });
    await org.session();
    return org;
  },
  backend: (path, body) => backendRequest(path, body),
});

export const orgMethodFor = defaultRouter.orgMethodFor;
