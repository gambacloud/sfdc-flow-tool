// Zip helpers over the vendored fflate (lib/vendor/fflate.js, MIT).

import { zipSync, unzipSync, strToU8, strFromU8 } from "../../lib/vendor/fflate.js";
import { escapeXml } from "./xml.js";

export const METADATA_NS = "http://soap.sforce.com/2006/04/metadata";

// package.xml for a deploy zip (flowtool/sfdc.py _deploy_package_xml).
export function deployPackageXml(types, apiVersion) {
  const blocks = Object.entries(types)
    .map(([type, members]) =>
      "    <types>\n" + members.map((m) => `        <members>${escapeXml(m)}</members>\n`).join("") +
      `        <name>${escapeXml(type)}</name>\n    </types>\n`)
    .join("");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<Package xmlns="${METADATA_NS}">\n${blocks}` +
    `    <version>${apiVersion}</version>\n</Package>\n`;
}

// bundle = { files: {path: text}, types: {Type: [members]}, api_version }
export function buildDeployZip(files, types, apiVersion) {
  const entries = { "package.xml": strToU8(deployPackageXml(types, apiVersion)) };
  for (const [path, content] of Object.entries(files)) entries[path] = strToU8(content);
  return zipSync(entries);
}

export function bytesToBase64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
export function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// { path: text } for every file in a zip (directories skipped).
export function readZipText(bytes) {
  const out = {};
  for (const [path, data] of Object.entries(unzipSync(bytes))) {
    if (!path.endsWith("/")) out[path] = strFromU8(data);
  }
  return out;
}
