// Port of flowtool/sfdc.py (and the org-facing halves of object_import.py,
// prompt_import.py, prompt_activation.py) to fetch. All calls go straight to
// the org's API host with the browser's session id: Bearer for REST/Tooling,
// SessionHeader for the Metadata SOAP API. Nothing here talks to the backend.
//
// Wire format of results matches what server.py returned to the web UI, so
// the UI copy needs no changes.

import { API_VERSION } from "../../lib/config.js";
import { escapeXml, parseXml, find, findAll, childText } from "./xml.js";
import { envelope as apexEnvelope, parseResponse as parseApexResponse, wrapScript } from "./apex.js";
import { METADATA_NS, base64ToBytes, buildDeployZip, bytesToBase64, readZipText } from "./zip.js";

const TERMINAL = new Set(["Succeeded", "Failed", "Canceled", "SucceededPartial"]);
const defaultSleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class SfError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.name = "SfError";
    this.status = status;
  }
}

export const soqlString = (s) => String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");

export function soapEnvelope(sessionId, body) {
  return (
    '<?xml version="1.0" encoding="utf-8"?>' +
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"' +
    ` xmlns:met="${METADATA_NS}">` +
    `<soapenv:Header><met:SessionHeader><met:sessionId>${escapeXml(sessionId)}</met:sessionId>` +
    "</met:SessionHeader></soapenv:Header>" +
    `<soapenv:Body>${body}</soapenv:Body></soapenv:Envelope>`
  );
}

// Human-readable message from a SOAP fault body, or null if it is not one.
export function faultString(payload) {
  let root;
  try { root = parseXml(String(payload || "")); } catch { return null; }
  const node = find(root, "faultstring");
  if (!node || !node.text.trim()) return null;
  const message = node.text.trim();
  return message.includes("INVALID_SESSION_ID")
    ? `${message}\nThe session id is expired or belongs to a different org.`
    : message;
}

const isInvalidSession = (status, text) =>
  (status === 401 || status === 500 || status === 403) && /INVALID_SESSION_ID/.test(text || "");

export function manifestXml(types, apiVersion) {
  const blocks = Object.entries(types)
    .map(([type, members]) =>
      "<met:types>" + members.map((m) => `<met:members>${escapeXml(m)}</met:members>`).join("") +
      `<met:name>${escapeXml(type)}</met:name></met:types>`)
    .join("");
  return `<met:unpackaged>${blocks}<met:version>${apiVersion}</met:version></met:unpackaged>`;
}

// One connection to one org. `getSession` returns { apiHost, sid } and is
// called again (once per request) when the org says INVALID_SESSION_ID.
export class Org {
  constructor({ getSession, fetch: fetchImpl, apiVersion = API_VERSION, sleep = defaultSleep } = {}) {
    this.getSession = getSession;
    this.fetch = fetchImpl || ((...a) => globalThis.fetch(...a));
    this.apiVersion = apiVersion;
    this.sleep = sleep;
    this._session = null;
  }

  async session(refresh = false) {
    if (!this._session || refresh) this._session = await this.getSession();
    return this._session;
  }

  // fetch against the API host; retries once with a freshly read sid.
  async _send(makeRequest) {
    for (let attempt = 0; ; attempt++) {
      const s = await this.session(attempt > 0);
      const [path, init] = makeRequest(s);
      const resp = await this.fetch(`https://${s.apiHost}${path}`, { ...init, credentials: "omit", redirect: "manual" });
      const redirected = resp.type === "opaqueredirect" || (resp.status >= 300 && resp.status < 400);
      if (redirected) {
        throw new SfError(
          `${path} was redirected (${resp.status || "redirect"}). The org API only answers on the My Domain host ` +
          "(https://<domain>.my.salesforce.com); open a Lightning or Setup page of the org and try again.");
      }
      const text = await resp.text();
      if (attempt === 0 && isInvalidSession(resp.status, text)) continue;
      return { status: resp.status, ok: resp.status >= 200 && resp.status < 300, text };
    }
  }

  async soap(body) {
    const path = `/services/Soap/m/${this.apiVersion}`;
    const r = await this._send((s) => [path, {
      method: "POST",
      headers: { "Content-Type": "text/xml; charset=UTF-8", SOAPAction: '""' },
      body: soapEnvelope(s.sid, body),
    }]);
    if (!r.ok) {
      const fault = faultString(r.text);
      if (fault) throw new SfError(`Salesforce rejected the call: ${fault}`);
      throw new SfError(`Metadata API call failed (${r.status}): ${r.text.slice(0, 600)}`);
    }
    return parseXml(r.text);
  }

  // GET a REST/Tooling query; follows nextRecordsUrl. Returns the records.
  async query(soql, { tooling = false, what = "records" } = {}) {
    let path = `/services/data/v${this.apiVersion}/${tooling ? "tooling/" : ""}query?q=${encodeURIComponent(soql)}`;
    const records = [];
    while (path) {
      const p = path;
      const r = await this._send((s) => [p, { method: "GET", headers: { Authorization: `Bearer ${s.sid}`, Accept: "application/json" } }]);
      if (!r.ok) {
        throw new SfError(faultString(r.text) || `Could not list ${what} (${r.status}): ${r.text.slice(0, 300)}`);
      }
      const data = JSON.parse(r.text);
      records.push(...(data.records || []));
      path = data.done === false ? data.nextRecordsUrl : null;
    }
    return records;
  }

  // True unless the org says it is a sandbox or a Developer/scratch org.
  async isProduction() {
    const rows = await this.query("SELECT IsSandbox, OrganizationType FROM Organization", { what: "the organization" });
    if (!rows.length) return true;
    return !rows[0].IsSandbox && !String(rows[0].OrganizationType).includes("Developer");
  }

  // Anonymous Apex over the Apex SOAP API (it returns the debug log inline).
  async runAnonymous(body) {
    const path = `/services/Soap/s/${this.apiVersion}`;
    const r = await this._send((s) => [path, {
      method: "POST",
      headers: { "Content-Type": "text/xml; charset=UTF-8", SOAPAction: '""' },
      body: apexEnvelope(s.sid, wrapScript(body)),
    }]);
    if (!r.ok) {
      const fault = faultString(r.text);
      throw new SfError(fault ? `Salesforce rejected the call: ${fault}` : `executeAnonymous failed (${r.status}): ${r.text.slice(0, 600)}`);
    }
    return parseApexResponse(r.text);
  }

  // ---- Metadata SOAP ------------------------------------------------

  async listMetadata(typeName) {
    const root = await this.soap(
      `<met:listMetadata><met:queries><met:type>${escapeXml(typeName)}</met:type></met:queries>` +
      `<met:asOfVersion>${this.apiVersion}</met:asOfVersion></met:listMetadata>`);
    return findAll(find(root, "listMetadataResponse"), "result").map((r) => ({
      full_name: childText(r, "fullName"),
      namespace_prefix: childText(r, "namespacePrefix"),
      manageable_state: childText(r, "manageableState"),
    }));
  }

  async startDeploy(zipBytes, checkOnly = true) {
    const root = await this.soap(
      `<met:deploy><met:zipFile>${bytesToBase64(zipBytes)}</met:zipFile><met:DeployOptions>` +
      "<met:allowMissingFiles>false</met:allowMissingFiles><met:autoUpdatePackage>false</met:autoUpdatePackage>" +
      `<met:checkOnly>${checkOnly}</met:checkOnly><met:ignoreWarnings>false</met:ignoreWarnings>` +
      "<met:performRetrieve>false</met:performRetrieve><met:purgeOnDelete>false</met:purgeOnDelete>" +
      "<met:rollbackOnError>true</met:rollbackOnError><met:singlePackage>true</met:singlePackage>" +
      "<met:testLevel>NoTestRun</met:testLevel></met:DeployOptions></met:deploy>");
    const id = childText(find(find(root, "deployResponse"), "result"), "id");
    if (!id) throw new SfError("deploy() returned no job id");
    return id;
  }

  async checkDeployStatus(jobId) {
    const root = await this.soap(
      `<met:checkDeployStatus><met:asyncProcessId>${escapeXml(jobId)}</met:asyncProcessId>` +
      "<met:includeDetails>true</met:includeDetails></met:checkDeployStatus>");
    return parseDeployResult(root, jobId);
  }

  async deployAndWait(zipBytes, { checkOnly = true, pollMs = 2000, timeoutMs = 300000 } = {}) {
    const jobId = await this.startDeploy(zipBytes, checkOnly);
    for (let waited = 0; waited < timeoutMs; waited += pollMs) {
      const result = await this.checkDeployStatus(jobId);
      if (TERMINAL.has(result.status)) return result;
      await this.sleep(pollMs);
    }
    throw new SfError(`deploy ${jobId} still running after ${timeoutMs / 1000}s`, 504);
  }

  async startRetrieve(types) {
    const root = await this.soap(
      "<met:retrieve><met:retrieveRequest>" +
      `<met:apiVersion>${this.apiVersion}</met:apiVersion><met:singlePackage>true</met:singlePackage>` +
      `${manifestXml(types, this.apiVersion)}</met:retrieveRequest></met:retrieve>`);
    const id = childText(find(find(root, "retrieveResponse"), "result"), "id");
    if (!id) throw new SfError("retrieve() returned no job id");
    return id;
  }

  async waitForRetrieve(jobId, { pollMs = 1500, timeoutMs = 120000 } = {}) {
    const body =
      `<met:checkRetrieveStatus><met:asyncProcessId>${escapeXml(jobId)}</met:asyncProcessId>` +
      "<met:includeZip>true</met:includeZip></met:checkRetrieveStatus>";
    for (let waited = 0; waited < timeoutMs; waited += pollMs) {
      const result = find(find(await this.soap(body), "checkRetrieveStatusResponse"), "result");
      if (!result) throw new SfError("checkRetrieveStatus returned no result");
      if ((childText(result, "done") || "").toLowerCase() === "true") {
        const status = childText(result, "status");
        if (status !== "Succeeded" && status !== "SucceededPartial") {
          throw new SfError(`Retrieve ${status}: ${childText(result, "errorMessage") || "no detail"}`);
        }
        const encoded = childText(result, "zipFile");
        if (!encoded) throw new SfError("Retrieve returned no package");
        return base64ToBytes(encoded);
      }
      await this.sleep(pollMs);
    }
    throw new SfError(`retrieve ${jobId} still running after ${timeoutMs / 1000}s`, 504);
  }

  async retrieveFiles(types, opts) {
    return readZipText(await this.waitForRetrieve(await this.startRetrieve(types), opts));
  }

  // ---- Lists (shapes as server.py returned them) -------------------------

  async listFlows() {
    const rows = await this.query(
      "SELECT DeveloperName, MasterLabel, Description, ActiveVersionId, LastModifiedDate " +
      "FROM FlowDefinition ORDER BY LastModifiedDate DESC", { tooling: true, what: "flows" });
    return { flows: rows.map((r) => ({
      api_name: r.DeveloperName, label: r.MasterLabel || r.DeveloperName, active: Boolean(r.ActiveVersionId),
      description: r.Description ?? null, last_modified: r.LastModifiedDate ?? null })) };
  }
  async listApexClasses() {
    const rows = await this.query("SELECT Name, LastModifiedDate FROM ApexClass ORDER BY LastModifiedDate DESC", { what: "Apex classes" });
    return { classes: rows.map((r) => ({ api_name: r.Name, last_modified: r.LastModifiedDate ?? null })) };
  }
  async listApexTriggers() {
    const rows = await this.query("SELECT Name, LastModifiedDate FROM ApexTrigger ORDER BY LastModifiedDate DESC", { what: "Apex triggers" });
    return { triggers: rows.map((r) => ({ api_name: r.Name, last_modified: r.LastModifiedDate ?? null })) };
  }
  async listLwc() {
    const rows = await this.query(
      "SELECT DeveloperName, LastModifiedDate FROM LightningComponentBundle ORDER BY LastModifiedDate DESC",
      { tooling: true, what: "Lightning Web Components" });
    return { components: rows.map((r) => ({ api_name: r.DeveloperName, last_modified: r.LastModifiedDate ?? null })) };
  }
  async listPermissionSets() {
    const rows = await this.query(
      "SELECT Name, Label, LastModifiedDate FROM PermissionSet WHERE IsOwnedByProfile = false ORDER BY LastModifiedDate DESC",
      { tooling: true, what: "Permission Sets" });
    return { permission_sets: rows.map((r) => ({ api_name: r.Name, label: r.Label ?? null, last_modified: r.LastModifiedDate ?? null })) };
  }
  async _listOwn(type, keep, what) {
    let found;
    try { found = await this.listMetadata(type); } catch (e) { throw new SfError(`Could not list ${what}: ${e.message}`); }
    return found.filter((f) => f.full_name && !f.namespace_prefix && keep(f.full_name))
      .map((f) => ({ api_name: f.full_name }))
      .sort((a, b) => a.api_name.toLowerCase().localeCompare(b.api_name.toLowerCase()));
  }
  async listPromptTemplates() { return { templates: await this._listOwn("GenAiPromptTemplate", () => true, "prompt templates") }; }
  async listCustomObjects() { return { objects: await this._listOwn("CustomObject", (n) => n.endsWith("__c"), "custom objects") }; }
  async listCustomFields() {
    return { fields: await this._listOwn("CustomField", (n) => n.includes(".") && n.endsWith("__c"), "custom fields") };
  }

  // ---- Retrieve (raw material; the backend turns it into IR) ---------------

  async retrieveSource(kind, apiName) {
    if (kind === "apex" || kind === "trigger") {
      const type = kind === "apex" ? "ApexClass" : "ApexTrigger";
      const rows = await this.query(`SELECT Body FROM ${type} WHERE Name = '${soqlString(apiName)}'`, { what: apiName });
      if (!rows.length) throw new SfError(`${apiName} was not found in the org.`);
      return { body: rows[0].Body };
    }
    const spec = {
      flow: ["Flow", "flows/", "Check the API name, or that the flow has a saved version."],
      lwc: ["LightningComponentBundle", `lwc/${apiName}/`, "Check the API name, or that the component has a saved version."],
      permission_set: ["PermissionSet", "permissionsets/", "Check the API name."],
      prompt_template: ["GenAiPromptTemplate", "genAiPromptTemplates/", ""],
      custom_object: ["CustomObject", "objects/", ""],
      custom_field: ["CustomField", "objects/", ""],
    }[kind];
    if (!spec) throw new SfError(`Unknown kind: ${kind}`);
    const files = await this.retrieveFiles({ [spec[0]]: [apiName] });
    const mine = Object.fromEntries(Object.entries(files).filter(([p]) => p.startsWith(spec[1])));
    if (!Object.keys(mine).length) {
      throw new SfError(`${apiName} was not in the retrieved package. ${spec[2]}`.trim());
    }
    return mine;
  }

  async retrieveAllFlows() {
    const files = await this.retrieveFiles({ Flow: ["*"] }, { timeoutMs: 600000 });
    const flows = {};
    for (const [p, text] of Object.entries(files)) {
      if (p.startsWith("flows/") && p.endsWith(".flow")) flows[p.slice(6, -5)] = text;
    }
    return flows;
  }

  async retrieveOrgSummaryZip(groups) {
    const chosen = groups ?? DEFAULT_ORG_SUMMARY_GROUPS;
    const unknown = chosen.filter((g) => !ORG_SUMMARY_GROUP_TYPES[g]);
    if (unknown.length) throw new SfError(`Unknown metadata group(s): ${JSON.stringify(unknown)}`);
    const types = chosen.flatMap((g) => ORG_SUMMARY_GROUP_TYPES[g]);
    const lists = await Promise.all(types.map((t) => this.listMetadata(t)));
    const manifest = {};
    types.forEach((t, i) => {
      const names = lists[i].filter(isUnmanaged).map((e) => e.full_name).filter(Boolean);
      if (names.length) manifest[t] = names;
    });
    if (!Object.keys(manifest).length) {
      throw new SfError("No unmanaged metadata of the selected groups was found in this org - only components from installed packages, or nothing was selected.");
    }
    return this.waitForRetrieve(await this.startRetrieve(manifest), { timeoutMs: 600000 });
  }

  // ---- Validate / deploy ---------------------------------------------------

  async validate(bundle, { checkOnly = true } = {}) {
    const zip = buildDeployZip(bundle.files, bundle.types, bundle.api_version || this.apiVersion);
    return this.deployAndWait(zip, { checkOnly });
  }

  // Second deploy that makes each just-deployed Prompt Template runnable
  // (flowtool/prompt_activation.py).
  async activateTemplates(names, apiVersion) {
    if (!names.length) return [];
    const files = await this.retrieveFiles({ GenAiPromptTemplate: names });
    const out = {};
    for (const n of names) {
      const xml = files[`genAiPromptTemplates/${n}.genAiPromptTemplate`];
      if (xml === undefined) throw new SfError(`could not read back ${n} to activate it`);
      const updated = withActiveVersion(xml);
      if (updated !== null) out[`genAiPromptTemplates/${n}.genAiPromptTemplate`] = updated;
    }
    const members = Object.keys(out).map((p) => p.split("/")[1].replace(/\.genAiPromptTemplate$/, ""));
    if (!members.length) return [];
    const result = await this.deployAndWait(
      buildDeployZip(out, { GenAiPromptTemplate: members }, apiVersion), { checkOnly: false });
    if (!result.success) {
      throw new SfError(result.failures.map(problemToString).join("; ") || result.error_message || result.status);
    }
    return members;
  }

  // ---- Setup links (sfdc.py component_setup_url) --------------------------

  async setupUrl(type, apiName, objectApiName) {
    const s = await this.session();
    const base = `https://${s.apiHost}`;
    if (type === "object") return `${base}/lightning/setup/ObjectManager/${apiName}/Details/view`;
    if (type === "field") return `${base}/lightning/setup/ObjectManager/${objectApiName}/FieldsAndRelationships/${apiName}/view`;
    if (type === "flow") {
      const id = await this._firstId(`SELECT Id FROM Flow WHERE Definition.DeveloperName = '${soqlString(apiName)}' ORDER BY VersionNumber DESC LIMIT 1`);
      return id ? `${base}/builder_platform_interaction/flowBuilder.app?flowId=${id}` : `${base}/lightning/setup/Flows/home`;
    }
    const t = {
      apex: ["ApexClasses", "ApexClass", "Name"], trigger: ["ApexTriggers", "ApexTrigger", "Name"],
      permission_set: ["PermSets", "PermissionSet", "Name"],
      lwc: ["LightningComponentBundles", "LightningComponentBundle", "DeveloperName"],
    }[type];
    if (!t) return `${base}/lightning/setup/SetupOneHome/home`;
    const id = await this._firstId(`SELECT Id FROM ${t[1]} WHERE ${t[2]} = '${soqlString(apiName)}'`);
    return id ? `${base}/lightning/setup/${t[0]}/page?address=%2F${id}` : `${base}/lightning/setup/${t[0]}/home`;
  }

  // A missing link must never turn a successful deploy into an error.
  async _firstId(soql) {
    try {
      const rows = await this.query(soql, { tooling: true });
      return rows.length ? rows[0].Id : null;
    } catch { return null; }
  }
}

export const problemToString = (p) =>
  `[${p.problem_type}] ${p.full_name}${p.line ? ` (line ${p.line}, col ${p.column})` : ""}: ${p.problem}`;

// -> { id, status, success, failures: [ComponentProblem], error_message }
export function parseDeployResult(root, jobId) {
  const result = find(find(root, "checkDeployStatusResponse"), "result");
  if (!result) throw new SfError("checkDeployStatus returned no result");
  const failures = findAll(result, "componentFailures").map((f) => ({
    full_name: childText(f, "fullName") || "?",
    problem: childText(f, "problem") || "?",
    problem_type: childText(f, "problemType") || "Error",
    line: childText(f, "lineNumber"),
    column: childText(f, "columnNumber"),
  }));
  return {
    id: childText(result, "id") || jobId,
    status: childText(result, "status") || "Unknown",
    success: (childText(result, "success") || "false").toLowerCase() === "true",
    failures,
    error_message: childText(result, "errorMessage"),
  };
}

// server.py _failures(): the strings the UI shows and the repair loop reads.
export function failureStrings(result) {
  const out = result.failures.map(problemToString);
  return !out.length && result.error_message ? [result.error_message] : out;
}

// ---- Prompt template activation (prompt_activation.py) ------------------

export function withActiveVersion(xml) {
  const versions = [...xml.matchAll(/<versionIdentifier>([^<]+)<\/versionIdentifier>/g)].map((m) => m[1]);
  if (!versions.length || /<activeVersionIdentifier>[^<]+<\/activeVersionIdentifier>/.test(xml)) return null;
  return xml.replace(/<developerName>/,
    () => `<activeVersionIdentifier>${versions[versions.length - 1]}</activeVersionIdentifier>\n    <developerName>`);
}

// ---- Org summary (sfdc.py ORG_SUMMARY_TYPE_GROUPS) ----------------------

const ORG_SUMMARY_TYPE_GROUPS = [
  { group: "objects", types: ["CustomObject"], default: true },
  { group: "apex", types: ["ApexClass", "ApexTrigger"], default: true },
  { group: "flows", types: ["Flow"], default: true },
  { group: "components", types: ["LightningComponentBundle", "AuraDefinitionBundle"], default: true },
  { group: "profiles", types: ["Profile"], default: false },
  { group: "custom_metadata", types: ["CustomMetadata"], default: true },
];
const ORG_SUMMARY_GROUP_TYPES = Object.fromEntries(ORG_SUMMARY_TYPE_GROUPS.map((g) => [g.group, g.types]));
const DEFAULT_ORG_SUMMARY_GROUPS = ORG_SUMMARY_TYPE_GROUPS.filter((g) => g.default).map((g) => g.group);

export const isUnmanaged = (e) => !e.namespace_prefix && (e.manageable_state || "unmanaged") === "unmanaged";
