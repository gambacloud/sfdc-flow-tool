"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  version: 0,
  approved: false,
  validatedVersion: null, // the version that passed checkOnly
  status: "Draft",
  artifacts: {},
  tab: "diagram",
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

function busy(button, on, label) {
  button.disabled = on;
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = label || "Working...";
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
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

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

async function renderDiagram(source) {
  const host = $("diagram");
  host.innerHTML = "";
  if (!mermaid) {
    const pre = document.createElement("pre");
    pre.textContent = source;
    host.appendChild(pre);
    return;
  }
  try {
    const { svg } = await mermaid.render("g" + Date.now(), source);
    host.innerHTML = svg;
  } catch (err) {
    const pre = document.createElement("pre");
    pre.textContent = source + "\n\n// could not render: " + err.message;
    host.appendChild(pre);
  }
}

function renderTab() {
  const isDiagram = state.tab === "diagram";
  $("diagram").parentElement.hidden = !isDiagram;
  $("code").hidden = isDiagram;
  if (!isDiagram) $("code").textContent = state.artifacts[state.tab] ?? "";
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

  $("result").hidden = true;
  renderDiagram(data.mermaid);
  renderTab();
  renderGate();

  // The XML is generated server-side; fetch it lazily for the tab.
  fetch(`/api/session/${data.session_id}/xml`)
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
    const data = await api("/api/design", {
      request: $("request").value,
      provider: $("provider").value || null,
      effort: $("effort").value,
      activate: $("activate").checked,
      api_version: $("apiVersion").value.trim() || "62.0",
    });
    state.validatedVersion = null;
    renderFlow(data);
  } catch (err) {
    showError(button.parentElement, err.message);
  } finally {
    busy(button, false);
  }
}

async function refine() {
  const button = $("refineBtn");
  showError(button.parentElement, "");
  busy(button, true, "Revising...");
  try {
    const data = await api("/api/refine", {
      session_id: state.sessionId,
      instruction: $("instruction").value,
    });
    $("instruction").value = "";
    renderFlow(data);
  } catch (err) {
    showError(button.parentElement, err.message);
  } finally {
    busy(button, false);
  }
}

async function approve() {
  const button = $("approveBtn");
  try {
    // Sending the version we rendered means a flow that changed underneath us
    // is rejected rather than silently approved.
    const data = await api("/api/approve", {
      session_id: state.sessionId,
      version: state.version,
    });
    renderFlow(data);
  } catch (err) {
    showError($("gate").parentElement, err.message);
  }
}

async function validate() {
  const button = $("validateBtn");
  showError($("gate").parentElement, "");
  busy(button, true, "Validating...");
  try {
    const result = await api("/api/validate", {
      session_id: state.sessionId,
      org: $("org").value || null,
    });
    if (result.success) state.validatedVersion = result.checked_version;
    renderResult(result);
    renderGate();
  } catch (err) {
    showError($("gate").parentElement, err.message);
  } finally {
    busy(button, false);
  }
}

async function repair(button) {
  busy(button, true, "Repairing...");
  try {
    const data = await api("/api/repair", { session_id: state.sessionId });
    state.validatedVersion = null;
    renderFlow(data);
  } catch (err) {
    showError($("gate").parentElement, err.message);
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
    const result = await api("/api/deploy", {
      session_id: state.sessionId,
      org: $("org").value || null,
      confirm: true,
    });
    renderResult(result);
  } catch (err) {
    showError($("gate").parentElement, err.message);
  } finally {
    busy(button, false);
  }
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

async function boot() {
  loadMermaid();

  try {
    const config = await api("/api/config");

    const provider = $("provider");
    if (config.providers.length) {
      config.providers.forEach((name) => provider.add(new Option(name, name)));
    } else {
      provider.add(new Option("no key found", ""));
      $("designBtn").disabled = true;
    }

    const org = $("org");
    if (config.orgs.length) {
      org.add(new Option("(default org)", ""));
      config.orgs.forEach((name) => org.add(new Option(name, name)));
    } else {
      org.add(new Option(config.sf_cli ? "no orgs" : "sf CLI not installed", ""));
    }

    const bits = [];
    bits.push(
      config.providers.length
        ? `LLM: ${config.providers.join(", ")}`
        : `no LLM key - add one to ${config.env_file}`
    );
    if (!config.sf_cli) bits.push("sf CLI not found - validation unavailable");
    $("env").textContent = bits.join("  ·  ");
  } catch (err) {
    $("env").textContent = "Could not reach the server: " + err.message;
  }

  $("designBtn").onclick = design;
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

  $("request").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) design();
  });
  $("instruction").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) refine();
  });
}

boot();
