// The interface every Salesforce-touching call goes through.
//
// Today each method is a stub that throws NotPortedError. They exist so the
// UI copy in ui/ can be wired end to end now, and so the port of
// flowtool/sfdc.py, agent_publish.py and portal_*.py can be done method by
// method without touching the UI. The mapping from UI paths to methods is
// ORG_ROUTES below; the full endpoint table is in
// docs/store/ARCHITECTURE.md, section 3.
//
// When ported, a method gets { apiHost, sid } from getOrgSession() and calls
// https://<apiHost>/services/... with fetch (allowed by host_permissions).
// The session id must never be passed to backend() in api.js.

export class NotPortedError extends Error {
  constructor(method) {
    super(`"${method}" talks to Salesforce and is not implemented in the extension yet.`);
    this.name = "NotPortedError";
    this.status = 501;
  }
}

// { apiHost, orgId, sid } for the active tab, or throws with a short reason
// ("no-tab", "not-salesforce", "no-session").
export async function getOrgSession() {
  const res = await chrome.runtime.sendMessage({ type: "org:getSession" });
  if (!res || !res.ok) throw new Error(`No Salesforce session: ${(res && res.error) || "no response"}`);
  return res;
}

export async function describeActiveOrg() {
  const res = await chrome.runtime.sendMessage({ type: "org:describe" });
  if (!res || !res.ok) return { status: "error", error: (res && res.error) || "no response" };
  return res.org;
}

function stub(name) {
  return async () => {
    throw new NotPortedError(name);
  };
}

// Lists (sfdc.py: list_flows 390, list_apex_classes 707, list_lwc_components 765,
// list_permission_sets 862, list_apex_triggers 923; object_import / prompt_import).
export const listFlows = stub("listFlows");
export const listApexClasses = stub("listApexClasses");
export const listApexTriggers = stub("listApexTriggers");
export const listLwc = stub("listLwc");
export const listPermissionSets = stub("listPermissionSets");
export const listPromptTemplates = stub("listPromptTemplates");
export const listCustomObjects = stub("listCustomObjects");
export const listCustomFields = stub("listCustomFields");

// Retrieve (sfdc.py: retrieve_* 548-962, retrieve_org_summary_zip 657).
export const retrieve = stub("retrieve");
export const retrieveOrgSummaryZip = stub("retrieveOrgSummaryZip");

// Validate / deploy a bundle { files, types, apiVersion } (sfdc.py: validate_bundle 986),
// and look up the Setup link afterwards (component_setup_url 465).
export const validate = stub("validate");
export const deploy = stub("deploy");
export const setupUrl = stub("setupUrl");

// Employee-agent publish (agent_publish.py: publish_employee_agent 251).
export const publishAgent = stub("publishAgent");

// Portal / site builder (portal_preflight.py, portal_site.py, ...).
export const portalPreflight = stub("portalPreflight");
export const portalApply = stub("portalApply");

// UI path -> org method. Matched against the path with the leading slash
// removed and the query string stripped. Anything not matched goes to the
// hosted backend.
export const ORG_ROUTES = [
  [/^api\/flows$/, "listFlows"],
  [/^api\/apex-classes$/, "listApexClasses"],
  [/^api\/apex-triggers$/, "listApexTriggers"],
  [/^api\/lwc-components$/, "listLwc"],
  [/^api\/permission-sets$/, "listPermissionSets"],
  [/^api\/prompt-templates$/, "listPromptTemplates"],
  [/^api\/custom-objects$/, "listCustomObjects"],
  [/^api\/custom-fields$/, "listCustomFields"],
  [/^api\/survey\//, "retrieve"],
  [/^api\/org-summary\//, "retrieveOrgSummaryZip"],
  [/^api\/import\//, "retrieve"],
  [/^api\/validate\//, "validate"],
  [/^api\/deploy\//, "deploy"],
  [/^api\/plan\/validate\//, "validate"],
  [/^api\/plan\/deploy\//, "deploy"],
  [/^api\/plan\/agent\/publish\//, "publishAgent"],
  [/^api\/portal\/(preflight|plan)$/, "portalPreflight"],
  [/^api\/portal\/(apply|status)$/, "portalApply"],
];

const METHODS = {
  listFlows, listApexClasses, listApexTriggers, listLwc, listPermissionSets,
  listPromptTemplates, listCustomObjects, listCustomFields, retrieve,
  retrieveOrgSummaryZip, validate, deploy, setupUrl, publishAgent,
  portalPreflight, portalApply,
};

export function orgMethodFor(path) {
  const clean = String(path).replace(/^\/+/, "").split("?")[0];
  const hit = ORG_ROUTES.find(([re]) => re.test(clean));
  return hit ? METHODS[hit[1]] : null;
}
