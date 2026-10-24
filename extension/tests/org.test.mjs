// node --test extension/tests/   (no dependencies)
import test from "node:test";
import assert from "node:assert/strict";
import { Org, SfError, faultString, soapEnvelope, failureStrings, withActiveVersion } from "../panel/org/sfdc.js";
import { parseXml, find } from "../panel/org/xml.js";
import { buildDeployZip, deployPackageXml, readZipText, bytesToBase64, base64ToBytes } from "../panel/org/zip.js";
import { createRouter, componentsFromTypes } from "../panel/org-client.js";

const soap = (inner) =>
  `<?xml version="1.0"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="http://soap.sforce.com/2006/04/metadata"><soapenv:Body>${inner}</soapenv:Body></soapenv:Envelope>`;
const fault = (msg) => soap(`<soapenv:Fault><faultcode>sf:INVALID_SESSION_ID</faultcode><faultstring>${msg}</faultstring></soapenv:Fault>`);
const deployStatus = (status, success, extra = "") =>
  soap(`<checkDeployStatusResponse><result><id>0Af1</id><status>${status}</status><success>${success}</success>${extra}</result></checkDeployStatusResponse>`);

// Scripted fetch: the handler returns a body string or {status, body}.
function fakeFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const r = await handler(url, init, calls.length);
    const { status = 200, body } = typeof r === "string" ? { body: r } : r;
    return { status, type: "basic", text: async () => body };
  };
  fn.calls = calls;
  return fn;
}
const session = { apiHost: "acme.my.salesforce.com", sid: "00Dxx!SID" };
const mkOrg = (f, getSession = async () => session) => new Org({ getSession, fetch: f, sleep: async () => {} });

test("envelope carries the session id, escaped, in the SOAP header", () => {
  const x = soapEnvelope("a<b&c", "<met:listMetadata/>");
  assert.match(x, /<met:sessionId>a&lt;b&amp;c<\/met:sessionId>/);
  assert.ok(find(parseXml(x), "listMetadata"));
});

test("faultString extracts message and explains INVALID_SESSION_ID", () => {
  assert.equal(faultString(fault("boom")), "boom");
  assert.match(faultString(fault("INVALID_SESSION_ID: bad")), /expired or belongs to a different org/);
  assert.equal(faultString("not xml at all"), null);
});

test("SOAP fault on HTTP 500 becomes SfError with the faultstring", async () => {
  const org = mkOrg(fakeFetch(() => ({ status: 500, body: fault("Cannot do that") })));
  await assert.rejects(org.listMetadata("Flow"), (e) => e instanceof SfError && /Salesforce rejected the call: Cannot do that/.test(e.message));
});

test("sid is re-read once on INVALID_SESSION_ID, then the call succeeds", async () => {
  let reads = 0;
  const f = fakeFetch((_u, init) => init.body.includes("SID2")
    ? soap("<listMetadataResponse><result><fullName>A</fullName></result></listMetadataResponse>")
    : { status: 500, body: fault("INVALID_SESSION_ID: Invalid Session ID") });
  const org = mkOrg(f, async () => ({ apiHost: session.apiHost, sid: ++reads === 1 ? "SID1" : "SID2" }));
  const out = await org.listMetadata("ApexClass");
  assert.equal(out[0].full_name, "A");
  assert.equal(reads, 2);
  assert.equal(f.calls.length, 2);
});

test("INVALID_SESSION_ID twice surfaces the error (no loop)", async () => {
  const f = fakeFetch(() => ({ status: 500, body: fault("INVALID_SESSION_ID") }));
  await assert.rejects(mkOrg(f).listMetadata("Flow"), /expired or belongs/);
  assert.equal(f.calls.length, 2);
});

test("requests omit cookies, use manual redirects, and a redirect is reported", async () => {
  const f = fakeFetch(() => ({ status: 302, body: "" }));
  await assert.rejects(mkOrg(f).listMetadata("Flow"), /redirected/);
  assert.equal(f.calls[0].init.credentials, "omit");
  assert.equal(f.calls[0].init.redirect, "manual");
});

test("listMetadata parses namespace and manageable state", async () => {
  const f = fakeFetch(() => soap(
    "<listMetadataResponse><result><fullName>X__c</fullName><manageableState>unmanaged</manageableState></result>" +
    "<result><fullName>ns__Y__c</fullName><namespacePrefix>ns</namespacePrefix><manageableState>installed</manageableState></result></listMetadataResponse>"));
  const rows = await mkOrg(f).listMetadata("CustomObject");
  assert.deepEqual(rows.map((r) => [r.full_name, r.namespace_prefix, r.manageable_state]),
    [["X__c", null, "unmanaged"], ["ns__Y__c", "ns", "installed"]]);
});

test("REST list uses Bearer, maps UI shape, follows nextRecordsUrl", async () => {
  const f = fakeFetch((url) => url.includes("page2")
    ? JSON.stringify({ done: true, records: [{ Name: "B", LastModifiedDate: "t2" }] })
    : JSON.stringify({ done: false, nextRecordsUrl: "/services/data/v64.0/query/page2", records: [{ Name: "A", LastModifiedDate: "t1" }] }));
  const out = await mkOrg(f).listApexClasses();
  assert.deepEqual(out, { classes: [{ api_name: "A", last_modified: "t1" }, { api_name: "B", last_modified: "t2" }] });
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer " + session.sid);
  assert.match(f.calls[0].url, /^https:\/\/acme\.my\.salesforce\.com\/services\/data\/v64\.0\/query\?q=/);
});

test("listFlows uses tooling and maps FlowDefinition rows", async () => {
  const f = fakeFetch(() => JSON.stringify({ done: true, records: [{ DeveloperName: "F1", MasterLabel: "Flow 1", ActiveVersionId: "x", Description: null, LastModifiedDate: "t" }] }));
  const out = await mkOrg(f).listFlows();
  assert.match(f.calls[0].url, /\/tooling\/query/);
  assert.deepEqual(out.flows[0], { api_name: "F1", label: "Flow 1", active: true, description: null, last_modified: "t" });
});

test("custom object list drops managed and standard objects", async () => {
  const f = fakeFetch(() => soap(
    "<listMetadataResponse><result><fullName>Zed__c</fullName></result><result><fullName>Account</fullName></result>" +
    "<result><fullName>ns__M__c</fullName><namespacePrefix>ns</namespacePrefix></result><result><fullName>alpha__c</fullName></result></listMetadataResponse>"));
  assert.deepEqual((await mkOrg(f).listCustomObjects()).objects, [{ api_name: "alpha__c" }, { api_name: "Zed__c" }]);
});

test("deploy status: failures carry line/column and render like the Python ComponentProblem", async () => {
  const f = fakeFetch(() => deployStatus("Failed", "false",
    "<details><componentFailures><fullName>Acct</fullName><problem>bad &amp; wrong</problem><problemType>Error</problemType><lineNumber>3</lineNumber><columnNumber>9</columnNumber></componentFailures></details>"));
  const r = await mkOrg(f).checkDeployStatus("0Af1");
  assert.equal(r.success, false);
  assert.deepEqual(failureStrings(r), ["[Error] Acct (line 3, col 9): bad & wrong"]);
});

test("deploy status falls back to errorMessage when there are no component failures", () => {
  assert.deepEqual(failureStrings({ failures: [], error_message: "nope" }), ["nope"]);
});

test("deployAndWait polls until a terminal status", async () => {
  const statuses = ["Pending", "InProgress", "Succeeded"];
  const f = fakeFetch((_u, init) => init.body.includes("<met:deploy>")
    ? soap("<deployResponse><result><id>0Af1</id></result></deployResponse>")
    : deployStatus(statuses.shift(), "true"));
  const r = await mkOrg(f).deployAndWait(new Uint8Array([1]), { checkOnly: true });
  assert.equal(r.status, "Succeeded");
  assert.match(f.calls[0].init.body, /<met:checkOnly>true<\/met:checkOnly>/);
  assert.equal(f.calls.length, 4);
});

test("retrieve returns the zip and rejects failed retrieves", async () => {
  const zip = buildDeployZip({ "flows/A.flow": "<Flow/>" }, { Flow: ["A"] }, "64.0");
  const mk = (status, zipB64 = "") => fakeFetch((_u, init) => init.body.includes("<met:retrieve>")
    ? soap("<retrieveResponse><result><id>09S1</id></result></retrieveResponse>")
    : soap(`<checkRetrieveStatusResponse><result><done>true</done><status>${status}</status><errorMessage>why</errorMessage><zipFile>${zipB64}</zipFile></result></checkRetrieveStatusResponse>`));
  const files = await mkOrg(mk("Succeeded", bytesToBase64(zip))).retrieveFiles({ Flow: ["A"] });
  assert.equal(files["flows/A.flow"], "<Flow/>");
  await assert.rejects(mkOrg(mk("Failed")).retrieveFiles({ Flow: ["A"] }), /Retrieve Failed: why/);
});

test("zip building: package.xml lists types, members land at their paths, round-trips", () => {
  const types = { Flow: ["F1"], CustomField: ["Obj__c.Fld__c"] };
  const px = deployPackageXml(types, "64.0");
  assert.match(px, /<members>Obj__c\.Fld__c<\/members>\s*<name>CustomField<\/name>/);
  assert.match(px, /<version>64\.0<\/version>/);
  const files = readZipText(buildDeployZip({ "flows/F1.flow": "<Flow>é</Flow>" }, types, "64.0"));
  assert.deepEqual(Object.keys(files).sort(), ["flows/F1.flow", "package.xml"]);
  assert.equal(files["flows/F1.flow"], "<Flow>é</Flow>");
  assert.deepEqual([...base64ToBytes(bytesToBase64(new Uint8Array([0, 255, 7])))], [0, 255, 7]);
});

test("prompt template activation edit", () => {
  const xml = "<G><versionIdentifier>v1</versionIdentifier><versionIdentifier>v2</versionIdentifier><developerName>T</developerName></G>";
  assert.match(withActiveVersion(xml), /<activeVersionIdentifier>v2<\/activeVersionIdentifier>\s*<developerName>/);
  assert.equal(withActiveVersion("<G><developerName>T</developerName></G>"), null);
  assert.equal(withActiveVersion(xml.replace("<developerName>", "<activeVersionIdentifier>v1</activeVersionIdentifier><developerName>")), null);
});

// ---- router ---------------------------------------------------------------

function routerWith({ orgHandler, backend }) {
  const org = mkOrg(fakeFetch(orgHandler));
  const sent = [];
  let n = 0;
  const router = createRouter({
    newOrg: async () => { await org.session(); return org; },
    backend: async (path, body) => { sent.push([path, body]); return backend(path, body); },
    newId: () => `job${++n}`,
  });
  return { ...router, sent, org };
}
const until = async (fn) => {
  for (let i = 0; i < 100; i++) {
    const r = await fn();
    if (r.done) return r;
    await new Promise((r2) => setTimeout(r2, 1));
  }
  throw new Error("never finished");
};
const bundle = { version: 3, files: { "flows/F.flow": "<Flow/>" }, types: { Flow: ["F"] }, api_version: "64.0" };
const deployOrg = (final = "Succeeded") => (url, init) => {
  if (url.includes("/tooling/query")) return JSON.stringify({ done: true, records: [{ Id: "301X" }] });
  return init.body.includes("<met:deploy>")
    ? soap("<deployResponse><result><id>0Af1</id></result></deployResponse>")
    : deployStatus(final, final === "Succeeded");
};

test("router: org paths are local, everything else is null (backend)", () => {
  const { orgMethodFor } = routerWith({ orgHandler: () => "", backend: () => ({}) });
  for (const p of ["api/flows", "/api/apex-classes", "api/validate/start", "api/validate/status?session_id=1", "api/plan/deploy/start", "api/import/start", "api/org-summary/status?job_id=1", "api/survey/start"]) {
    assert.equal(typeof orgMethodFor(p), "function", p);
  }
  for (const p of ["api/design/start", "api/session/abc", "api/plan/start", "api/config", "api/activity?after=0", "api/repair/start", "api/plan/approve"]) {
    assert.equal(orgMethodFor(p), null, p);
  }
});

test("router: validate fetches the bundle from the backend, runs checkOnly locally, reports back", async () => {
  const r = routerWith({ orgHandler: deployOrg(), backend: (path) => (path.endsWith("/bundle") ? bundle : {}) });
  assert.deepEqual(await r.orgMethodFor("api/validate/start")({ session_id: "S 1" }), { started: true });
  const res = await until(() => r.orgMethodFor("api/validate/status?session_id=S%201")());
  assert.deepEqual(res, { done: true, success: true, status: "Succeeded", failures: [], checked_version: 3 });
  assert.equal(r.sent[0][0], "api/session/S%201/bundle");
  assert.equal(r.sent[1][0], "api/session/S%201/deploy-result");
  assert.equal(r.sent[1][1].check_only, true);
  assert.ok(!JSON.stringify(r.sent).includes(session.sid), "session id must not reach the backend");
});

test("router: deploy needs confirm; flow deploy returns instance_url and flow_url", async () => {
  const r = routerWith({ orgHandler: deployOrg(), backend: (p) => (p.endsWith("/bundle") ? bundle : {}) });
  await assert.rejects(r.orgMethodFor("api/deploy/start")({ session_id: "1" }), /explicit confirmation/);
  await r.orgMethodFor("api/deploy/start")({ session_id: "1", confirm: true });
  const res = await until(() => r.orgMethodFor("api/deploy/status?session_id=1")());
  assert.equal(res.success, true);
  assert.equal(res.instance_url, "https://acme.my.salesforce.com");
  assert.equal(res.flow_url, "https://acme.my.salesforce.com/builder_platform_interaction/flowBuilder.app?flowId=301X");
  assert.equal(r.sent[1][1].check_only, false);
});

test("router: failed plan validate returns failure strings; plan deploy returns setup_urls and passes plan through", async () => {
  const bad = routerWith({
    orgHandler: (_u, i) => (i.body.includes("<met:deploy>")
      ? soap("<deployResponse><result><id>1</id></result></deployResponse>")
      : deployStatus("Failed", "false", "<componentFailures><fullName>F</fullName><problem>p</problem><problemType>Error</problemType></componentFailures>")),
    backend: (p) => (p.endsWith("/bundle") ? bundle : {}),
  });
  await bad.orgMethodFor("api/plan/validate/start")({ session_id: "1" });
  const v = await until(() => bad.orgMethodFor("api/plan/validate/status?session_id=1")());
  assert.deepEqual([v.success, v.failures], [false, ["[Error] F: p"]]);
  assert.equal(bad.sent[0][0], "api/plan/session/1/bundle");

  const planBundle = { ...bundle, types: { CustomObject: ["O__c"], CustomField: ["O__c.F__c"] }, files: { "objects/O__c.object": "<x/>" } };
  const ok = routerWith({ orgHandler: deployOrg(), backend: (p) => (p.endsWith("/bundle") ? planBundle : { plan: { steps: [] } }) });
  await ok.orgMethodFor("api/plan/deploy/start")({ session_id: "1", confirm: true });
  const d = await until(() => ok.orgMethodFor("api/plan/deploy/status?session_id=1")());
  assert.deepEqual(d.setup_urls, {
    "O__c": "https://acme.my.salesforce.com/lightning/setup/ObjectManager/O__c/Details/view",
    "O__c.F__c": "https://acme.my.salesforce.com/lightning/setup/ObjectManager/O__c/FieldsAndRelationships/F__c/view",
  });
  assert.deepEqual(d.plan, { steps: [] });
});

test("router: import retrieves locally then posts files (not credentials) to import/parse", async () => {
  const zip = buildDeployZip({ "flows/A.flow": "<Flow/>" }, { Flow: ["A"] }, "64.0");
  const r = routerWith({
    orgHandler: (_u, init) => (init.body.includes("<met:retrieve>")
      ? soap("<retrieveResponse><result><id>09S</id></result></retrieveResponse>")
      : soap(`<checkRetrieveStatusResponse><result><done>true</done><status>Succeeded</status><zipFile>${bytesToBase64(zip)}</zipFile></result></checkRetrieveStatusResponse>`)),
    backend: () => ({ done: true, session_id: "new" }),
  });
  const { job_id } = await r.orgMethodFor("api/import/start")({ api_name: "A", kind: "flow", effort: "medium", api_key: "k" });
  const out = await until(() => r.orgMethodFor(`api/import/status?job_id=${job_id}`)());
  assert.equal(out.session_id, "new");
  assert.equal(r.sent[0][0], "api/import/parse");
  assert.deepEqual(r.sent[0][1].files, { "flows/A.flow": "<Flow/>" });
  assert.ok(!JSON.stringify(r.sent).includes(session.sid));
});

test("router: org errors reject with a message; unknown job errors", async () => {
  const r = routerWith({ orgHandler: () => ({ status: 400, body: JSON.stringify([{ message: "nope" }]) }), backend: () => ({}) });
  await assert.rejects(r.orgMethodFor("api/flows")({}), /Could not list flows \(400\)/);
  await assert.rejects(r.orgMethodFor("api/import/status?job_id=zzz")(), /Unknown import job/);
});

test("router: agent publish and portal answer 'not available in the extension yet'", async () => {
  const { orgMethodFor } = routerWith({ orgHandler: () => "", backend: () => ({}) });
  for (const p of ["api/plan/agent/publish/start", "api/portal/apply", "api/portal/status?job_id=1"]) {
    await assert.rejects(orgMethodFor(p)({}), (e) => e.status === 501 && /not available in the extension yet/.test(e.message));
  }
});

test("componentsFromTypes maps package types to setup kinds", () => {
  assert.deepEqual(componentsFromTypes({ ApexClass: ["A"], CustomField: ["O__c.F__c"], Other: ["x"] }),
    [{ type: "apex", name: "A", step: "A" }, { type: "field", name: "F__c", object: "O__c", step: "O__c.F__c" }]);
});

test("router: choose_fields without a pick_id is re-posted to import/parse with the chosen fields", async () => {
  const zip = buildDeployZip({ "objects/O__c.object": "<x/>" }, { CustomObject: ["O__c"] }, "64.0");
  const r = routerWith({
    orgHandler: (_u, init) => (init.body.includes("<met:retrieve>")
      ? soap("<retrieveResponse><result><id>09S</id></result></retrieveResponse>")
      : soap(`<checkRetrieveStatusResponse><result><done>true</done><status>Succeeded</status><zipFile>${bytesToBase64(zip)}</zipFile></result></checkRetrieveStatusResponse>`)),
    backend: (_p, b) => (b.fields ? { done: true, opened_as: "plan" } : { done: true, opened_as: "choose_fields", pick_id: null, fields: [] }),
  });
  const { job_id } = await r.orgMethodFor("api/import/start")({ api_name: "O__c", kind: "custom_object" });
  const picked = await until(() => r.orgMethodFor(`api/import/status?job_id=${job_id}`)());
  assert.ok(picked.pick_id);
  const plan = await r.orgMethodFor("api/import/object-fields")({ pick_id: picked.pick_id, fields: ["A__c"] });
  assert.equal(plan.opened_as, "plan");
  assert.deepEqual(r.sent[1][1].fields, ["A__c"]);
  assert.equal(r.sent[1][0], "api/import/parse");
});

test("router: needs_existing_permission_set fetches the set locally and re-requests the merged bundle", async () => {
  const zip = buildDeployZip({ "permissionsets/PS.permissionset": "<PermissionSet>old</PermissionSet>" }, { PermissionSet: ["PS"] }, "64.0");
  const merged = { ...bundle, types: { PermissionSet: ["PS"] } };
  const r = routerWith({
    orgHandler: (u, init) => {
      if (init.body && init.body.includes("<met:retrieve>")) return soap("<retrieveResponse><result><id>09S</id></result></retrieveResponse>");
      if (init.body && init.body.includes("<met:checkRetrieveStatus>")) return soap(`<checkRetrieveStatusResponse><result><done>true</done><status>Succeeded</status><zipFile>${bytesToBase64(zip)}</zipFile></result></checkRetrieveStatusResponse>`);
      return deployOrg()(u, init);
    },
    backend: (p, b) => (p.endsWith("/bundle") ? (b ? merged : { ...bundle, needs_existing_permission_set: "PS" }) : {}),
  });
  await r.orgMethodFor("api/plan/validate/start")({ session_id: "1" });
  const v = await until(() => r.orgMethodFor("api/plan/validate/status?session_id=1")());
  assert.equal(v.success, true);
  assert.deepEqual(r.sent[1], ["api/plan/session/1/bundle", { existing_permission_set_xml: "<PermissionSet>old</PermissionSet>" }]);
  assert.ok(Array.isArray(r.sent[2][1].component_failures));
});

// ---- Anonymous Apex scenarios ---------------------------------------------
import { parseLimits, parseResponse as parseApex, wrapScript, envelope as apexEnvelope } from "../panel/org/apex.js";

const APEX_LOG = [
  "12:00:00.9 (9)|CUMULATIVE_LIMIT_USAGE",
  "12:00:00.9 (9)|LIMIT_USAGE_FOR_NS|(default)|",
  "  Number of SOQL queries: 100 out of 100 ******* CLOSE TO LIMIT",
  "  Number of DML statements: 140 out of 150",
  "  Maximum CPU time: 8000 out of 10000",
  "12:00:00.9 (9)|LIMIT_USAGE_FOR_NS|Other|",
  "  Number of SOQL queries: 50 out of 100",
  "12:00:00.9 (9)|CUMULATIVE_LIMIT_USAGE_END",
].join("\n");
const apexResponse = ({ compiled = "true", success = "true", problem = "", exception = "", log = "" } = {}) =>
  `<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="http://soap.sforce.com/2006/08/apex"><soapenv:Header><DebuggingInfo><debugLog>${log}</debugLog></DebuggingInfo></soapenv:Header><soapenv:Body><executeAnonymousResponse><result><compileProblem>${problem}</compileProblem><compiled>${compiled}</compiled><exceptionMessage>${exception}</exceptionMessage><line>4</line><success>${success}</success></result></executeAnonymousResponse></soapenv:Body></soapenv:Envelope>`;

test("limits come from the default namespace and flag hot ones", () => {
  const limits = parseLimits(APEX_LOG);
  assert.equal(limits.filter((l) => l.name === "Number of SOQL queries").length, 1);
  const run = parseApex(apexResponse({ log: APEX_LOG }));
  assert.equal(run.success, true);
  assert.equal(run.limits.find((l) => l.name === "Number of DML statements").level, "critical");
  assert.equal(run.limits.find((l) => l.name === "Maximum CPU time").level, "warn");
  assert.equal(run.limits.find((l) => l.name === "Number of SOQL queries").level, "critical");
  assert.equal(run.warnings.length, 3);
  assert.ok(run.warnings.some((w) => /140 of 150/.test(w)));
});

test("compile problems and exceptions are reported", () => {
  const bad = parseApex(apexResponse({ compiled: "false", success: "false", problem: "Unexpected token" }));
  assert.equal(bad.compiled, false);
  assert.match(bad.problem, /Unexpected token \(line 2\)/);
  const boom = parseApex(apexResponse({ success: "false", exception: "System.AssertException: nope" }));
  assert.match(boom.problem, /AssertException/);
});

test("script is rolled back and escaped in the request", () => {
  const w = wrapScript("insert a;");
  assert.ok(w.indexOf("setSavepoint") < w.indexOf("insert a;") && w.indexOf("insert a;") < w.indexOf("rollback"));
  assert.match(apexEnvelope("tok", "a < b && c"), /a &lt; b &amp;&amp; c/);
});

test("runAnonymous posts to the Apex SOAP endpoint and parses the log", async () => {
  const f = fakeFetch(() => apexResponse({ log: APEX_LOG }));
  const run = await mkOrg(f).runAnonymous("insert a;");
  assert.match(f.calls[0].url, /\/services\/Soap\/s\//);
  assert.match(f.calls[0].init.body, /Database\.rollback/);
  assert.equal(run.success, true);
});

test("scenarios/run refuses production and an undeployed plan, and never runs there", async () => {
  const sent = [];
  const org = (production) => ({
    isProduction: async () => production,
    runAnonymous: async () => { sent.push("ran"); return { success: true }; },
  });
  const router = (deployed, production) => createRouter({
    newOrg: async () => org(production),
    backend: async () => ({ apex: "x", deployed }),
  });
  await assert.rejects(router(false, false).orgMethodFor("api/plan/scenarios/run")({ session_id: "1", index: 0 }), /Deploy this plan/);
  await assert.rejects(router(true, true).orgMethodFor("api/plan/scenarios/run")({ session_id: "1", index: 0 }), /never production/);
  assert.deepEqual(sent, []);
  const ok = await router(true, false).orgMethodFor("api/plan/scenarios/run")({ session_id: "1", index: 0 });
  assert.equal(ok.success, true);
});
