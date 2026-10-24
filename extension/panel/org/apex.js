// Port of flowtool/anon_apex.py: a test scenario as Anonymous Apex, wrapped in
// a savepoint that is always rolled back, with governor limits read from the
// debug log's CUMULATIVE_LIMIT_USAGE block. Pure functions; the network half
// is Org.runAnonymous in sfdc.js.

import { escapeXml, parseXml, find } from "./xml.js";

export const APEX_NS = "http://soap.sforce.com/2006/08/apex";
const WARN_AT = 0.7;
const CRITICAL_AT = 0.9;

const ADVICE = {
  "Number of SOQL queries": "A query per record will break at scale - query once, outside the loop.",
  "Number of DML statements": "DML per record will break at scale - collect records and write them once.",
  "Number of DML rows": "Close to the DML row cap for one transaction.",
  "Number of query rows": "Close to the query row cap - tighten the WHERE clause or add a LIMIT.",
  "Maximum CPU time": "CPU heavy - look for nested loops or work done per record that could be done once.",
  "Maximum heap size": "Large in-memory collections - process in smaller batches.",
  "Number of callouts": "Close to the callout cap for one transaction.",
};

// Lines wrapScript puts above the body, so a compile error's line can be mapped back.
const WRAP_LINES = 2;

export function wrapScript(body) {
  return (
    "Savepoint forgeSp = Database.setSavepoint();\n" +
    "try {\n" + body + "\n} finally {\n    Database.rollback(forgeSp);\n}\n"
  );
}

function level(used, allowed) {
  const ratio = allowed ? used / allowed : 0;
  return ratio >= CRITICAL_AT ? "critical" : ratio >= WARN_AT ? "warn" : "ok";
}

// First (default namespace) CUMULATIVE_LIMIT_USAGE block of a debug log.
export function parseLimits(log) {
  const out = [];
  let inside = false;
  for (const line of String(log || "").split(/\r?\n/)) {
    if (line.includes("|CUMULATIVE_LIMIT_USAGE_END")) break;
    if (line.includes("|LIMIT_USAGE_FOR_NS|")) {
      if (inside && out.length) break;
      inside = true;
      continue;
    }
    if (!inside) continue;
    const m = /^\s*(.+?):\s*(\d+)\s+out of\s+(\d+)/.exec(line);
    if (m && Number(m[3]) > 0) {
      const used = Number(m[2]);
      const allowed = Number(m[3]);
      out.push({
        name: m[1], used, allowed,
        percent: Math.round((used / allowed) * 1000) / 10, level: level(used, allowed),
      });
    }
  }
  return out;
}

export function warningsFor(limits) {
  return limits.filter((l) => l.level !== "ok").map((l) => {
    const tip = ADVICE[l.name] || "";
    return `${l.name}: ${l.used} of ${l.allowed} (${Math.round((l.used / l.allowed) * 100)}%). ${tip}`.trim();
  });
}

// Same shape as ScenarioRun.as_dict() in anon_apex.py.
export function parseResponse(payload) {
  const root = parseXml(payload);
  const text = (name) => { const n = find(root, name); return n ? n.text : null; };
  const log = text("debugLog") || "";
  const compiled = (text("compiled") || "").toLowerCase() === "true";
  const success = (text("success") || "").toLowerCase() === "true";
  let problem = null;
  if (!compiled) {
    const line = text("line");
    problem = (text("compileProblem") || "Did not compile") +
      (/^\d+$/.test(line || "") ? ` (line ${Math.max(Number(line) - WRAP_LINES, 1)})` : "");
  } else if (!success) {
    problem = text("exceptionMessage") || "The script threw an exception";
  }
  const limits = parseLimits(log);
  return { success, compiled, problem, limits, warnings: warningsFor(limits), log_excerpt: log.slice(-4000) };
}

export function envelope(sessionId, script) {
  const cat = (c, l) => `<apex:categories><apex:category>${c}</apex:category><apex:level>${l}</apex:level></apex:categories>`;
  return (
    '<?xml version="1.0" encoding="utf-8"?>' +
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"' +
    ` xmlns:apex="${APEX_NS}"><soapenv:Header><apex:DebuggingHeader>` +
    cat("Apex_code", "DEBUG") + cat("Apex_profiling", "FINE") + cat("Workflow", "INFO") +
    "</apex:DebuggingHeader>" +
    `<apex:SessionHeader><apex:sessionId>${escapeXml(sessionId)}</apex:sessionId></apex:SessionHeader>` +
    "</soapenv:Header><soapenv:Body>" +
    `<apex:executeAnonymous><apex:String>${escapeXml(script)}</apex:String></apex:executeAnonymous>` +
    "</soapenv:Body></soapenv:Envelope>"
  );
}
