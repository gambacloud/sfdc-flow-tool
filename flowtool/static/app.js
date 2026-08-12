"use strict";

const $ = (id) => document.getElementById(id);

// sessionStorage, not localStorage: this is about surviving a reload of this
// tab (the OAuth redirect, a plain F5), not resurrecting a session days
// later in a fresh tab against a server that has very likely since restarted
// and forgotten it.
const SESSION_STORAGE_KEY = "flowtool.sessionId";

const state = {
  sessionId: null,
  version: 0,
  approved: false,
  validatedVersion: null, // the version that passed checkOnly
  status: "Draft",
  artifacts: {},
  tab: "diagram",
  org: null, // { accessToken, instanceUrl } once logged into Salesforce directly
  usage: null, // cumulative token usage for this session, refreshed on every model call
  errors: [], // every error message shown to the user this session, newest first
};

let mermaid = null;

// Mermaid comes from a CDN. If it is unavailable - offline, blocked, whatever -
// the diagram falls back to its source, which is still readable.
async function loadMermaid() {
  try {
    const mod = await import(
      "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
    );
    mermaid = mod.default;
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    mermaid.initialize({
      startOnLoad: false,
      theme: dark ? "dark" : "default",
      securityLevel: "strict",
    });
  } catch {
    mermaid = null;
  }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = { detail: text };
  }
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// A validate/deploy/import can run past Heroku's 30s request limit, so the
// server hands back a job immediately and this polls for the result instead
// of one request sitting open the whole time.
async function poll(path, params) {
  const query = new URLSearchParams(params).toString();
  for (;;) {
    const data = await api(`${path}?${query}`);
    if (data.done) return data;
    await sleep(1500);
  }
}

function busy(button, on, label) {
  button.disabled = on;
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = label || "Working...";
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
}

// A button with a dropdown panel next to it: click to toggle, click outside
// or Escape to close, and opening one closes any other still-open dropdown -
// shared by Logs and Manual entry so neither has to duplicate this.
function wireDropdown(btn, panel) {
  const close = () => {
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  };
  btn.onclick = (event) => {
    event.stopPropagation();
    const opening = panel.hidden;
    document.querySelectorAll(".dropdown-panel").forEach((other) => {
      if (other !== panel) other.hidden = true;
    });
    panel.hidden = !opening;
    btn.setAttribute("aria-expanded", String(opening));
  };
  document.addEventListener("click", (event) => {
    if (!panel.hidden && !panel.contains(event.target) && event.target !== btn) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
}

function showError(where, message) {
  const existing = where.querySelector(".error");
  if (existing) existing.remove();
  if (!message) return;
  const node = document.createElement("div");
  node.className = "error";
  node.textContent = message;
  where.appendChild(node);
}

// Every error the user sees also lands in the Logs panel, so a failure that
// happened three actions ago is still findable instead of gone the moment the
// next click clears it from view.
function logError(label, message) {
  if (!message) return;
  state.errors.unshift({ time: new Date(), label, message });
  if (state.errors.length > 50) state.errors.length = 50;
  renderLogs();
}

// Shared by the inline usage line under the flow header and the Logs panel,
// so the two never disagree about what a session has cost so far.
function usageText(usage) {
  if (!usage || !usage.calls) return "";
  const bits = [
    `${usage.calls} model call${usage.calls === 1 ? "" : "s"}`,
    `${usage.input_tokens.toLocaleString()} in`,
    `${usage.output_tokens.toLocaleString()} out`,
  ];
  // Cached input bills at roughly a tenth, so it is worth showing apart.
  if (usage.cached_input_tokens) {
    bits.push(`${usage.cached_input_tokens.toLocaleString()} cached`);
  }
  if (usage.thinking_tokens) {
    bits.push(`${usage.thinking_tokens.toLocaleString()} thinking`);
  }
  return bits.join("  ·  ");
}

function renderLogs() {
  $("logsUsage").textContent = usageText(state.usage) || "No model calls yet.";

  const errors = state.errors;
  $("logsErrorCount").textContent = errors.length ? `(${errors.length})` : "";
  const box = $("logsErrors");
  box.innerHTML = "";
  if (!errors.length) {
    box.textContent = "No errors yet.";
  } else {
    errors.forEach((entry) => {
      const node = document.createElement("div");
      node.className = "logs-entry";
      const time = document.createElement("div");
      time.className = "t";
      time.textContent = `${entry.time.toLocaleTimeString()} · ${entry.label}`;
      const message = document.createElement("div");
      message.textContent = entry.message;
      node.append(time, message);
      box.appendChild(node);
    });
  }

  const count = $("logCount");
  count.hidden = !errors.length;
  count.textContent = String(errors.length);
}

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

async function renderDiagram(source) {
  const host = $("diagram");
  host.innerHTML = "";
  resetDiagramView();
  if (!mermaid) {
    const pre = document.createElement("pre");
    pre.textContent = source;
    host.appendChild(pre);
    return;
  }
  try {
    const { svg } = await mermaid.render("g" + Date.now(), source);
    host.innerHTML = svg;
    wireDiagramSync();
  } catch (err) {
    const pre = document.createElement("pre");
    pre.textContent = source + "\n\n// could not render: " + err.message;
    host.appendChild(pre);
  }
}

// Pan and zoom the diagram itself rather than scrolling its container - a
// flow with twenty elements is wider than any viewport, and native scroll
// has no way to zoom out and get oriented first.
const diagramView = { scale: 1, x: 0, y: 0 };
const DIAGRAM_ZOOM_MIN = 0.3;
const DIAGRAM_ZOOM_MAX = 4;

function applyDiagramView() {
  $("diagram").style.transform =
    `translate(${diagramView.x}px, ${diagramView.y}px) scale(${diagramView.scale})`;
}

function resetDiagramView() {
  diagramView.scale = 1;
  diagramView.x = 0;
  diagramView.y = 0;
  applyDiagramView();
}

// Zooms around a fixed point (mx, my, in the wrap's own coordinates) rather
// than the canvas origin, so the thing under the cursor is what stays put -
// zooming toward the corner instead of toward whatever you are looking at is
// the usual complaint about naive scroll-to-zoom.
function zoomDiagramAt(mx, my, factor) {
  const newScale = Math.min(
    DIAGRAM_ZOOM_MAX,
    Math.max(DIAGRAM_ZOOM_MIN, diagramView.scale * factor)
  );
  const px = (mx - diagramView.x) / diagramView.scale;
  const py = (my - diagramView.y) / diagramView.scale;
  diagramView.x = mx - px * newScale;
  diagramView.y = my - py * newScale;
  diagramView.scale = newScale;
  applyDiagramView();
}

// Wired once at boot - the wrap element itself never gets replaced, only
// #diagram's contents on each new render.
function wireDiagramPanZoom() {
  const wrap = $("diagram").closest(".diagram-wrap");
  let dragging = false;
  let moved = false;
  let lastX = 0;
  let lastY = 0;

  wrap.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = wrap.getBoundingClientRect();
      const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
      zoomDiagramAt(event.clientX - rect.left, event.clientY - rect.top, factor);
    },
    { passive: false }
  );

  wrap.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    moved = false;
    lastX = event.clientX;
    lastY = event.clientY;
    wrap.classList.add("dragging");
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const dx = event.clientX - lastX;
    const dy = event.clientY - lastY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
    diagramView.x += dx;
    diagramView.y += dy;
    lastX = event.clientX;
    lastY = event.clientY;
    applyDiagramView();
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    wrap.classList.remove("dragging");
  });

  // A single, permanent listener rather than a one-shot added per drag: a
  // one-shot armed after a drag that ends somewhere with no click target at
  // all (e.g. released past the window edge) would stay armed and swallow
  // the next unrelated click - the Reset view button, say. Checking and
  // clearing the flag right here means it only ever catches the click that
  // is the direct continuation of that same drag.
  wrap.addEventListener(
    "click",
    (event) => {
      if (moved) {
        moved = false;
        event.stopPropagation();
      }
    },
    { capture: true }
  );

  $("diagramResetBtn").onclick = resetDiagramView;
}

function renderElementIndex(rows) {
  const host = $("elementIndex");
  host.innerHTML = "";
  (rows || []).forEach((row) => {
    const node = document.createElement("div");
    node.className = "row";
    node.dataset.id = row.name;
    const head = document.createElement("div");
    head.className = "row-head";
    const name = document.createElement("span");
    name.className = "row-name";
    name.textContent = row.label;
    const type = document.createElement("span");
    type.className = "row-type";
    type.textContent = row.type;
    head.append(name, type);
    const detail = document.createElement("div");
    detail.className = "row-detail";
    detail.textContent = row.detail;
    node.append(head, detail);
    host.appendChild(node);
  });
}

// Mermaid renders each node as <g id="<renderId>-flowchart-<nodeId>-<n>">, and
// to_mermaid() uses the element's own name as nodeId - so the two sides share
// an id with no lookup table needed. Names never contain a hyphen (Salesforce
// API names are letters, numbers and underscores only), so the pattern is
// unambiguous even though nodeId itself is unbounded text.
function wireDiagramSync() {
  const svg = $("diagram").querySelector("svg");
  const rows = $("elementIndex").querySelectorAll(".row");
  if (!svg || !rows.length) return;

  const rowById = new Map([...rows].map((row) => [row.dataset.id, row]));
  const set = (id, on) => {
    rowById.get(id)?.classList.toggle("linked", on);
    svg.querySelector(`[data-index-id="${CSS.escape(id)}"]`)?.classList.toggle("linked", on);
  };

  svg.querySelectorAll(".node").forEach((g) => {
    const match = g.id.match(/-flowchart-(.+)-\d+$/);
    const id = match && match[1];
    if (!id || !rowById.has(id)) return;
    g.dataset.indexId = id;
    g.addEventListener("mouseenter", () => set(id, true));
    g.addEventListener("mouseleave", () => set(id, false));
    g.addEventListener("click", () =>
      rowById.get(id)?.scrollIntoView({ block: "nearest", behavior: "smooth" })
    );
  });
  rows.forEach((row) => {
    const id = row.dataset.id;
    row.addEventListener("mouseenter", () => set(id, true));
    row.addEventListener("mouseleave", () => set(id, false));
  });
}

function renderTab() {
  const isDiagram = state.tab === "diagram";
  const isExplain = state.tab === "explain";
  $("diagramTab").hidden = !isDiagram;
  $("explainPane").hidden = !isExplain;
  $("code").hidden = isDiagram || isExplain;
  if (!isDiagram && !isExplain) $("code").textContent = state.artifacts[state.tab] ?? "";
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === state.tab);
  });
}

function renderGate() {
  const approve = $("approveBtn");
  const validate = $("validateBtn");
  const deploy = $("deployBtn");
  const gate = $("gate");

  approve.disabled = state.approved;
  approve.textContent = state.approved ? "Approved" : "Approve";
  validate.disabled = !state.approved;

  const validatedNow = state.validatedVersion === state.version;
  deploy.disabled = !state.approved || !validatedNow;

  if (!state.approved) {
    gate.textContent =
      "This flow has not been approved. Nothing is sent to Salesforce until you approve it.";
  } else if (!validatedNow) {
    gate.textContent = "Approved. Validate it against the org before deploying.";
  } else if (state.status === "Active") {
    gate.textContent =
      "Validated. Deploying will make this flow ACTIVE - it starts running on live records immediately.";
  } else {
    gate.textContent = "Validated. It will deploy as a Draft and will not run.";
  }
}

function renderFlow(data) {
  state.sessionId = data.session_id;
  sessionStorage.setItem(SESSION_STORAGE_KEY, data.session_id);
  state.version = data.version;
  state.approved = data.approved;
  state.status = data.status;
  state.artifacts.markdown = data.markdown;
  state.artifacts.ir = JSON.stringify(data.ir, null, 2);

  $("empty").hidden = true;
  $("flowView").hidden = false;
  $("refineBox").hidden = false;

  $("flowLabel").textContent = data.label;
  $("flowMeta").textContent =
    `${data.api_name}  ·  ${data.trigger}  ·  ${data.element_count} element` +
    (data.element_count === 1 ? "" : "s") +
    `  ·  API ${data.api_version}`;
  $("flowDesc").textContent = data.description || "";

  const badge = $("statusBadge");
  badge.textContent = data.status === "Active" ? "ACTIVE" : "Draft";
  badge.className = "badge " + (data.status === "Active" ? "active" : "draft");
  $("versionBadge").textContent = "v" + data.version;

  if (data.usage) state.usage = data.usage;
  const text = usageText(state.usage);
  if (text) $("usage").textContent = text;
  renderLogs();

  $("log").innerHTML = "";
  data.history.forEach((entry) => {
    const node = document.createElement("div");
    node.className = "entry";
    node.innerHTML =
      `<div class="v">v${entry.version}</div>` +
      `<div></div>`;
    node.lastElementChild.textContent = entry.note;
    $("log").appendChild(node);
  });

  // A new version means the old explanation describes something else.
  $("explanation").textContent =
    "Ask the model what this flow does, or leave the box empty for a walkthrough.";
  $("explanation").className = "explanation dim";

  $("result").hidden = true;
  renderElementIndex(data.element_index);
  renderDiagram(data.mermaid);
  renderTab();
  renderGate();

  // The XML is generated server-side; fetch it lazily for the tab.
  fetch(`api/session/${data.session_id}/xml`)
    .then((r) => r.text())
    .then((text) => {
      state.artifacts.xml = text;
      if (state.tab === "xml") renderTab();
    })
    .catch(() => {});
}

function renderResult(result) {
  const box = $("result");
  box.hidden = false;
  box.className = "result " + (result.success ? "ok" : "bad");
  box.innerHTML = "";

  const title = document.createElement("h3");
  title.textContent = result.success
    ? `${result.status} - this flow will deploy cleanly`
    : `${result.status} - Salesforce rejected it`;
  box.appendChild(title);

  if (result.flow_url) {
    const link = document.createElement("a");
    link.href = result.flow_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "flow-link";
    link.textContent = "Open it in Flow Builder";
    box.appendChild(link);
  }

  if (result.failures.length) {
    const list = document.createElement("ul");
    result.failures.forEach((failure) => {
      const item = document.createElement("li");
      item.textContent = failure;
      list.appendChild(item);
    });
    box.appendChild(list);

    const fix = document.createElement("button");
    fix.textContent = "Ask the model to fix these";
    fix.onclick = () => repair(fix);
    box.appendChild(fix);
  }
}

// --------------------------------------------------------------------------
// Actions
// --------------------------------------------------------------------------

async function design() {
  const button = $("designBtn");
  showError(button.parentElement, "");
  busy(button, true, "Designing...");
  try {
    const { job_id } = await api("api/design/start", {
      request: $("request").value,
      provider: $("provider").value || null,
      effort: $("effort").value,
      activate: $("activate").checked,
      api_version: $("apiVersion").value.trim() || "62.0",
      api_key: $("apiKey").value.trim() || null,
      model: $("model").value || null,
    });
    const data = await poll("api/design/status", { job_id });
    state.validatedVersion = null;
    renderFlow(data);
  } catch (err) {
    showError(button.parentElement, err.message);
    logError("Design", err.message);
  } finally {
    busy(button, false);
  }
}

async function loadFlows() {
  const picker = $("flowPicker");
  picker.innerHTML = "";
  // Neither an OAuth/manual session nor the sf CLI is available yet - hitting
  // /api/flows anyway would surface the CLI's "not on PATH" message, which is
  // meaningless to someone who was never going to use the CLI in the first
  // place (every Heroku deployment). Wait for a real credential instead.
  if (!state.org && !state.sfCli) {
    picker.add(new Option("connect to an org first", ""));
    return;
  }
  picker.add(new Option("Loading...", ""));
  try {
    const query = new URLSearchParams(orgCredentials()).toString();
    const data = await api(`api/flows${query ? "?" + query : ""}`);
    picker.innerHTML = "";
    if (!data.flows.length) {
      picker.add(new Option("no flows in this org", ""));
      return;
    }
    data.flows.forEach((flow) => {
      const mark = flow.active ? "" : "  (inactive)";
      picker.add(new Option(`${flow.label}${mark}`, flow.api_name));
    });
  } catch (err) {
    picker.innerHTML = "";
    picker.add(new Option("could not list flows", ""));
    showError($("openPane"), err.message);
    logError("Load flows", err.message);
  }
}

async function importFlow() {
  const button = $("importBtn");
  showError($("openPane"), "");
  const apiName = $("flowPicker").value;
  if (!apiName) return;
  busy(button, true, "Opening...");
  try {
    const { job_id } = await api("api/import/start", {
      api_name: apiName,
      ...orgCredentials(),
      effort: $("effort").value,
      provider: $("provider").value || null,
      api_key: $("apiKey").value.trim() || null,
      model: $("model").value || null,
    });
    const data = await poll("api/import/status", { job_id });
    state.validatedVersion = null;
    state.explanation = null;
    renderFlow(data);
  } catch (err) {
    showError($("openPane"), err.message);
    logError("Import", err.message);
  } finally {
    busy(button, false);
  }
}

async function runSurvey() {
  showError($("openPane"), "");
  if (!state.org && !state.sfCli) {
    showError($("openPane"), "Connect to an org first.");
    return;
  }

  const dialog = $("surveyDialog");
  const body = $("surveyBody");
  const text = $("surveyText");
  const copyBtn = $("surveyCopyBtn");
  body.hidden = false;
  body.className = "dim";
  body.textContent = "Scanning every flow in the org - this can take a moment...";
  text.hidden = true;
  copyBtn.hidden = true;
  dialog.showModal();

  try {
    const { job_id } = await api("api/survey/start", orgCredentials());
    const data = await poll("api/survey/status", { job_id });
    body.hidden = true;
    text.hidden = false;
    text.value = data.report;
    copyBtn.hidden = false;
  } catch (err) {
    body.className = "error";
    body.textContent = err.message;
    logError("Support report", err.message);
  }
}

async function explainFlow() {
  const button = $("explainBtn");
  const target = $("explanation");
  busy(button, true, "Reading...");
  target.textContent = "Reading the flow...";
  target.className = "explanation dim";
  try {
    await api("api/explain/start", {
      session_id: state.sessionId,
      question: $("question").value.trim() || null,
    });
    const data = await poll("api/explain/status", { session_id: state.sessionId });
    target.textContent = data.explanation;
    target.className = "explanation filled";
    if (data.usage) {
      state.usage = data.usage;
      const text = usageText(state.usage);
      if (text) $("usage").textContent = text;
      renderLogs();
    }
  } catch (err) {
    target.textContent = err.message;
    target.className = "explanation dim";
    logError("Explain", err.message);
  } finally {
    busy(button, false);
  }
}

async function refine() {
  const button = $("refineBtn");
  showError(button.parentElement, "");
  busy(button, true, "Revising...");
  try {
    await api("api/refine/start", {
      session_id: state.sessionId,
      instruction: $("instruction").value,
    });
    const data = await poll("api/refine/status", { session_id: state.sessionId });
    $("instruction").value = "";
    renderFlow(data);
  } catch (err) {
    showError(button.parentElement, err.message);
    logError("Refine", err.message);
  } finally {
    busy(button, false);
  }
}

async function approve() {
  const button = $("approveBtn");
  try {
    // Sending the version we rendered means a flow that changed underneath us
    // is rejected rather than silently approved.
    const data = await api("api/approve", {
      session_id: state.sessionId,
      version: state.version,
    });
    renderFlow(data);
  } catch (err) {
    showError($("gate").parentElement, err.message);
    logError("Approve", err.message);
  }
}

async function validate() {
  const button = $("validateBtn");
  showError($("gate").parentElement, "");
  busy(button, true, "Validating...");
  try {
    await api("api/validate/start", {
      session_id: state.sessionId,
      ...orgCredentials(),
    });
    const result = await poll("api/validate/status", { session_id: state.sessionId });
    if (result.success) state.validatedVersion = result.checked_version;
    renderResult(result);
    renderGate();
  } catch (err) {
    showError($("gate").parentElement, err.message);
    logError("Validate", err.message);
  } finally {
    busy(button, false);
  }
}

async function repair(button) {
  busy(button, true, "Repairing...");
  try {
    await api("api/repair/start", { session_id: state.sessionId });
    const data = await poll("api/repair/status", { session_id: state.sessionId });
    state.validatedVersion = null;
    renderFlow(data);
  } catch (err) {
    showError($("gate").parentElement, err.message);
    logError("Repair", err.message);
  } finally {
    busy(button, false);
  }
}

async function deploy() {
  const warning =
    state.status === "Active"
      ? "Deploy this flow as ACTIVE?\n\nIt will start running against live records immediately."
      : "Deploy this flow as a Draft?\n\nIt will be created in the org but will not run.";
  if (!confirm(warning)) return;

  const button = $("deployBtn");
  showError($("gate").parentElement, "");
  busy(button, true, "Deploying...");
  try {
    await api("api/deploy/start", {
      session_id: state.sessionId,
      ...orgCredentials(),
      confirm: true,
    });
    const result = await poll("api/deploy/status", { session_id: state.sessionId });
    renderResult(result);
  } catch (err) {
    showError($("gate").parentElement, err.message);
    logError("Deploy", err.message);
  } finally {
    busy(button, false);
  }
}

// --------------------------------------------------------------------------
// API key memory (browser-local only - never written to a file or sent
// anywhere but the request that needs it)
// --------------------------------------------------------------------------

function apiKeyStorageKey(providerName) {
  return `flowtool.apiKey.${providerName}`;
}

function loadStoredApiKey() {
  const name = $("provider").value;
  $("apiKey").value = (name && localStorage.getItem(apiKeyStorageKey(name))) || "";
}

// --------------------------------------------------------------------------
// Model picker
// --------------------------------------------------------------------------

// Asked of the provider rather than hard-coded, so a retired model stops being
// offered instead of failing several seconds into a design. Also the way out of
// a daily quota: switch model, keep working.
async function loadModels() {
  const select = $("model");
  const providerName = $("provider").value;
  const remembered =
    (providerName && localStorage.getItem(`flowtool.model.${providerName}`)) || "";

  select.innerHTML = "";
  select.add(new Option("provider default", ""));
  if (!providerName) return;

  select.disabled = true;
  showError($("options"), "");
  try {
    const data = await api("api/models", {
      provider: providerName,
      api_key: $("apiKey").value.trim() || null,
    });
    (data.models || []).forEach((name) => {
      const suffix = name === data.default ? " (default)" : "";
      select.add(new Option(name + suffix, name));
    });
    // A remembered choice the key can no longer use must not be sent silently.
    select.value = [...select.options].some((o) => o.value === remembered)
      ? remembered
      : "";
  } catch (err) {
    // Almost always a missing or bad key. Swallowing it meant the page looked
    // fine and only said so at design time, by which point the message names a
    // provider the user has since forgotten they were on.
    showError($("options"), `${providerName}: ${err.message}`);
    $("options").open = true; // an error inside a collapsed panel is no error
    logError("Models", err.message);
  } finally {
    select.disabled = false;
  }
}

function rememberModel() {
  const providerName = $("provider").value;
  if (!providerName) return;
  const value = $("model").value;
  if (value) localStorage.setItem(`flowtool.model.${providerName}`, value);
  else localStorage.removeItem(`flowtool.model.${providerName}`);
}

// --------------------------------------------------------------------------
// Salesforce login (OAuth implicit flow, same Connected App as the sibling
// salesforce-debugtool - see authorization.component.ts / app.component.ts
// there for the pattern this follows). No client secret, no server-side
// token exchange: Salesforce hands the token straight to this page.
// --------------------------------------------------------------------------

function startOAuthLogin(clientId, host) {
  const redirectUri = window.location.origin + window.location.pathname;
  window.location.href =
    `${host}/services/oauth2/authorize?response_type=token` +
    `&client_id=${encodeURIComponent(clientId)}` +
    `&redirect_uri=${encodeURIComponent(redirectUri)}` +
    `&state=flowtool`;
}

// Salesforce returns the token in the URL fragment, not a query string or a
// redirect the server ever sees - read it here and scrub it from the address
// bar so it doesn't linger in history.
function restoreOAuthFromFragment() {
  if (!location.hash) return;
  const params = new URLSearchParams(location.hash.slice(1));
  const accessToken = params.get("access_token");
  const instanceUrl = params.get("instance_url");
  if (accessToken && instanceUrl) {
    state.org = { accessToken, instanceUrl };
    history.replaceState({}, document.title, location.pathname + location.search);
  }
}

function renderOAuthStatus() {
  $("orgConnected").textContent = state.org
    ? `Connected: ${new URL(state.org.instanceUrl).host}`
    : "Not connected to an org";
}

// The session itself lives server-side, independent of the browser - a
// reload only loses the browser's copy of it. Re-fetching here means the
// OAuth redirect (a full navigation, not an AJAX call) and a plain F5 both
// stop being a way to lose an in-progress design. A session the server no
// longer has (restarted since, or just old) fails quietly and clears the
// stale pointer rather than showing an error for something the user didn't
// just do.
async function restoreSession() {
  const sessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!sessionId) return;
  try {
    const data = await api(`api/session/${sessionId}`);
    state.validatedVersion = null;
    renderFlow(data);
  } catch {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
  }
}

// The implicit flow issues no refresh token, so an expired session just
// means logging in again - callers send whatever this returns and the
// sf-CLI-backed `org` alias only applies when it is empty.
function orgCredentials() {
  if (state.org) {
    return { instance_url: state.org.instanceUrl, access_token: state.org.accessToken };
  }
  const org = $("org")?.value;
  return org ? { org } : {};
}

// The manual-entry alternative to OAuth: for a host with no Connected App
// configured, or anyone who would rather paste a token they already have
// than click through a login redirect. Feeds the same state.org the OAuth
// flow does, so orgCredentials() cannot tell the two apart.
function connectManually() {
  const instanceUrl = $("manualInstanceUrl").value.trim();
  const token = $("manualToken").value.trim();
  showError($("manualPanel"), "");
  if (!instanceUrl || !token) {
    showError($("manualPanel"), "Both fields are required.");
    return;
  }
  try {
    new URL(instanceUrl);
  } catch {
    showError($("manualPanel"), "Instance URL doesn't look like a valid URL.");
    return;
  }
  state.org = { accessToken: token, instanceUrl };
  renderOAuthStatus();
  // Cleared once it is in memory - the field held it only long enough to be
  // typed or pasted, same as the promise in the help text below it.
  $("manualToken").value = "";
  $("manualPanel").hidden = true;
  $("manualBtn").setAttribute("aria-expanded", "false");
  // The Open tab may already be showing "connect to an org first" from
  // before this credential existed - now that it does, retry.
  if ($("flowPicker").dataset.loaded) loadFlows();
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

async function boot() {
  loadMermaid();

  try {
    const config = await api("api/config");

    const provider = $("provider");
    const known = config.all_providers?.length ? config.all_providers : config.providers;
    if (known.length) {
      known.forEach((name) => {
        const hasServerKey = config.providers.includes(name);
        provider.add(new Option(hasServerKey ? `${name} (key set on server)` : name, name));
      });
      // Listing every provider means the first one is whichever the server
      // happens to define first, not one that has a key - so the page opened on
      // a provider with no credentials and said so only at design time. The
      // server already worked out which one is usable; use its answer.
      provider.value = config.default_provider || known[0];
    } else {
      provider.add(new Option("no provider available", ""));
      $("designBtn").disabled = true;
    }
    loadStoredApiKey();
    loadModels();
    provider.onchange = () => {
      loadStoredApiKey();
      loadModels();
    };
    $("model").onchange = rememberModel;
    $("apiKey").addEventListener("input", () => {
      const name = provider.value;
      if (!name) return;
      const value = $("apiKey").value.trim();
      if (value) localStorage.setItem(apiKeyStorageKey(name), value);
      else localStorage.removeItem(apiKeyStorageKey(name));
    });
    // Refresh once the key is finished, not on every keystroke.
    $("apiKey").addEventListener("change", loadModels);

    // With no sf CLI on this host, the picker can only ever offer "sf CLI not
    // installed" - not a choice, just noise next to the OAuth login buttons.
    state.sfCli = config.sf_cli;
    if (config.sf_cli) {
      const org = $("org");
      if (config.orgs.length) {
        org.add(new Option("(default org)", ""));
        config.orgs.forEach((name) => org.add(new Option(name, name)));
      } else {
        org.add(new Option("no orgs", ""));
      }
    } else {
      $("org").closest(".header-org").hidden = true;
    }

    if (config.clientId) {
      $("oauthBox").hidden = false;
      $("loginProdBtn").onclick = () =>
        startOAuthLogin(config.clientId, "https://login.salesforce.com");
      $("loginSandboxBtn").onclick = () =>
        startOAuthLogin(config.clientId, "https://test.salesforce.com");
    }
    restoreOAuthFromFragment();
    renderOAuthStatus();
    await restoreSession();

    const bits = [];
    if (config.providers.length) {
      bits.push(`LLM: ${config.providers.join(", ")}`);
    } else if (config.heroku) {
      bits.push("no LLM key - set ANTHROPIC_API_KEY or GEMINI_API_KEY as a config var");
    } else {
      bits.push(`no LLM key - add one to ${config.env_file}`);
    }
    // sf CLI is only the way in when there is no OAuth login configured either -
    // with a clientId set, the login buttons already cover validate/deploy.
    if (!config.sf_cli && !config.clientId) {
      bits.push("sf CLI not found - validation unavailable");
    }
    $("env").textContent = bits.join("  ·  ");
  } catch (err) {
    $("env").textContent = "Could not reach the server: " + err.message;
  }

  document.querySelectorAll(".mode").forEach((mode) => {
    mode.onclick = () => {
      const open = mode.dataset.mode === "open";
      document.querySelectorAll(".mode").forEach((other) =>
        other.classList.toggle("active", other === mode)
      );
      $("openPane").hidden = !open;
      $("newPane").hidden = open;
      if (open && !$("flowPicker").dataset.loaded) {
        $("flowPicker").dataset.loaded = "1";
        loadFlows();
      }
    };
  });

  wireDiagramPanZoom();

  $("designBtn").onclick = design;
  $("importBtn").onclick = importFlow;
  $("surveyLink").onclick = runSurvey;
  $("surveyCloseBtn").onclick = () => $("surveyDialog").close();
  $("surveyCopyBtn").onclick = async () => {
    const text = $("surveyText");
    try {
      await navigator.clipboard.writeText(text.value);
    } catch {
      // Clipboard API needs a secure context or permission that may not be
      // granted - selecting the text is a fallback anyone can copy manually.
      text.hidden = false;
      text.focus();
      text.select();
      return;
    }
    busy($("surveyCopyBtn"), true, "Copied");
    setTimeout(() => busy($("surveyCopyBtn"), false), 1200);
  };
  $("explainBtn").onclick = explainFlow;
  $("refineBtn").onclick = refine;
  $("approveBtn").onclick = approve;
  $("validateBtn").onclick = validate;
  $("deployBtn").onclick = deploy;

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      state.tab = tab.dataset.tab;
      renderTab();
    };
  });

  // Switching org invalidates the flow list that came from the previous one.
  $("org").onchange = () => {
    if ($("flowPicker").dataset.loaded) loadFlows();
  };

  $("request").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) design();
  });
  $("instruction").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) refine();
  });

  wireDropdown($("logsBtn"), $("logsPanel"));
  renderLogs();

  wireDropdown($("manualBtn"), $("manualPanel"));
  $("manualConnectBtn").onclick = connectManually;
}

boot();
