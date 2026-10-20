// Tiny XML reader for Salesforce SOAP responses (no DOMParser so it also runs
// under node for tests). Namespace prefixes are dropped; attributes are ignored.
// Only element/text structure is needed: { name, children, text }.

const ENTITIES = { "&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&apos;": "'" };
export const unescapeXml = (s) =>
  s.replace(/&(?:lt|gt|amp|quot|apos);|&#(\d+);|&#x([0-9a-fA-F]+);/g, (m, d, h) =>
    d ? String.fromCodePoint(+d) : h ? String.fromCodePoint(parseInt(h, 16)) : ENTITIES[m]);
export const escapeXml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function parseXml(xml) {
  const re = /<!\[CDATA\[([\s\S]*?)\]\]>|<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<(\/?)([\w.-]+:)?([\w.-]+)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>|([^<]+)/g;
  const root = { name: "#root", children: [], text: "" };
  const stack = [root];
  let m;
  while ((m = re.exec(xml))) {
    const top = stack[stack.length - 1];
    if (m[1] !== undefined) top.text += m[1];
    else if (m[7] !== undefined) top.text += unescapeXml(m[7]);
    else if (m[4]) {
      if (m[2]) {
        if (stack.length > 1) stack.pop();
      } else {
        const el = { name: m[4], children: [], text: "" };
        top.children.push(el);
        if (!m[6]) stack.push(el);
      }
    }
  }
  return root.children[0] || null;
}

// First descendant (depth-first) with this local name, or null.
export function find(node, name) {
  if (!node) return null;
  for (const c of node.children) {
    if (c.name === name) return c;
    const hit = find(c, name);
    if (hit) return hit;
  }
  return null;
}
export function findAll(node, name, out = []) {
  if (!node) return out;
  for (const c of node.children) {
    if (c.name === name) out.push(c);
    else findAll(c, name, out);
  }
  return out;
}
export const child = (node, name) => (node ? node.children.find((c) => c.name === name) || null : null);
export const childText = (node, name) => {
  const c = child(node, name);
  return c ? c.text : null;
};
