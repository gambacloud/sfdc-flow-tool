/*
 * A rough, non-executing visual approximation of an LWC's HTML template -
 * NOT a real render. Maps a handful of common lightning-* tags to plain HTML
 * styled with the SLDS-inspired classes in vendor/slds-subset.css; every
 * other tag either passes through (a small safe-HTML allowlist) or renders
 * as a neutral labeled placeholder box. No JS from the component ever runs,
 * no data is bound - {expr} bindings are shown literally as text.
 *
 * Security note: the source html is untrusted (LLM-generated or retrieved
 * from an org) and DOMParser is used only to *read* its structure. The
 * walker below rebuilds a brand new DOM tree via createElement/setAttribute/
 * textContent - it never assigns parsed content through innerHTML, and it
 * never copies an `on*` attribute or an unrecognized/executable tag through
 * verbatim. That rebuild is what makes this safe to call on arbitrary text.
 *
 * Exposes one global: window.renderLwcPreview(container, htmlSource).
 */
(function () {
  "use strict";

  const SAFE_PASSTHROUGH_TAGS = new Set([
    "div", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "a", "img", "br", "strong", "em", "b", "i", "label",
  ]);
  const SAFE_PASSTHROUGH_ATTRS = new Set(["class", "title", "alt"]);
  const BINDING_RE = /\{[^{}]+\}/g;

  function attr(el, name) {
    return el.hasAttribute(name) ? el.getAttribute(name) : null;
  }

  // Splits text into plain text nodes plus <span class="lwc-preview-binding">
  // chips for any {expr} found - binding expressions are shown verbatim as
  // their source text, never evaluated (there is no data to evaluate against).
  function renderBindingText(text) {
    const frag = document.createDocumentFragment();
    let last = 0;
    let match;
    BINDING_RE.lastIndex = 0;
    while ((match = BINDING_RE.exec(text)) !== null) {
      if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
      const chip = document.createElement("span");
      chip.className = "lwc-preview-binding";
      chip.textContent = match[0];
      frag.appendChild(chip);
      last = match.index + match[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }

  function iconInitials(name) {
    const part = name && name.includes(":") ? name.split(":")[1] : name;
    return (part || "?").slice(0, 2).toUpperCase();
  }

  function isTemplateEl(node) {
    return node.nodeType === Node.ELEMENT_NODE && node.tagName.toLowerCase() === "template";
  }

  // Walks every child of `sourceParent` into freshly-built safe DOM under
  // `targetParent`. Handles the if:true/lwc:if -> lwc:elseif/lwc:else chain
  // convention at this sibling level (not inside walkNode) since deciding
  // "skip the next N siblings" needs to see the whole run at once.
  function walkChildren(sourceParent, targetParent) {
    const nodes = Array.from(sourceParent.childNodes);
    let i = 0;
    while (i < nodes.length) {
      const node = nodes[i];
      if (isTemplateEl(node) && (node.hasAttribute("if:true") || node.hasAttribute("if:false") || node.hasAttribute("lwc:if"))) {
        walkNode(node, targetParent);
        // Legacy if:true/if:false are each independently evaluated by the
        // real framework - they're only an if/else *pair* when they share
        // the same bound expression, so that's compared by raw attribute
        // text below rather than assumed from position alone. lwc:if's own
        // elseif/else, by contrast, are unconditionally part of the one
        // chain per spec.
        const startCond = node.getAttribute("if:true") || node.getAttribute("if:false");
        const startIsLegacyIf = node.hasAttribute("if:true") || node.hasAttribute("if:false");
        i++;
        while (i < nodes.length) {
          const next = nodes[i];
          if (next.nodeType === Node.TEXT_NODE && !next.textContent.trim()) { i++; continue; }
          if (!isTemplateEl(next)) break;
          if (next.hasAttribute("lwc:elseif") || next.hasAttribute("lwc:else")) { i++; continue; }
          if (startIsLegacyIf && next.hasAttribute("if:false") && next.getAttribute("if:false") === startCond) { i++; continue; }
          break;
        }
        continue;
      }
      walkNode(node, targetParent);
      i++;
    }
  }

  function walkNode(node, targetParent) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.textContent.trim()) targetParent.appendChild(renderBindingText(node.textContent));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    const tag = node.tagName.toLowerCase();

    if (tag === "template") {
      // A root passthrough, an if:true/if:false/lwc:if branch (chain
      // continuations were already swallowed by walkChildren above), or a
      // for:each/iterator:* loop template - all render their static
      // children exactly once. A loop has no real data to know how many
      // items to show, so one rendered pass stands in as a sample row.
      walkChildren(node.content || node, targetParent);
      return;
    }

    if (tag.startsWith("lightning-")) {
      renderLightningTag(node, tag, targetParent);
      return;
    }

    if (SAFE_PASSTHROUGH_TAGS.has(tag)) {
      renderPassthroughTag(node, tag, targetParent);
      return;
    }

    renderUnknownTag(node, tag, targetParent);
  }

  function renderLightningTag(node, tag, targetParent) {
    switch (tag) {
      case "lightning-card": return renderCard(node, targetParent);
      case "lightning-button": return renderButton(node, targetParent);
      case "lightning-input": return renderInput(node, targetParent);
      case "lightning-input-field":
      case "lightning-output-field": return renderField(node, targetParent);
      case "lightning-record-view-form":
      case "lightning-record-edit-form": return renderRecordForm(node, tag, targetParent);
      case "lightning-layout": return renderLayout(node, targetParent);
      case "lightning-layout-item": return renderLayoutItem(node, targetParent);
      case "lightning-icon": return renderIcon(node, targetParent);
      default: return renderUnknownTag(node, tag, targetParent);
    }
  }

  function renderCard(node, targetParent) {
    const article = document.createElement("article");
    article.className = "slds-card";
    const title = attr(node, "title");
    if (title) {
      const header = document.createElement("div");
      header.className = "slds-card__header";
      const h2 = document.createElement("h2");
      h2.className = "slds-card__header-title";
      h2.appendChild(renderBindingText(title));
      header.appendChild(h2);
      article.appendChild(header);
    }
    const body = document.createElement("div");
    body.className = "slds-card__body";
    walkChildren(node, body);
    article.appendChild(body);
    targetParent.appendChild(article);
  }

  const BUTTON_VARIANT_CLASS = {
    brand: "slds-button_brand",
    destructive: "slds-button_destructive",
    "outline-brand": "slds-button_outline-brand",
  };

  function renderButton(node, targetParent) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.disabled = true;
    const variant = (attr(node, "variant") || "").toLowerCase();
    btn.className = "slds-button " + (BUTTON_VARIANT_CLASS[variant] || "slds-button_neutral");
    const label = attr(node, "label") || node.textContent.trim() || "Button";
    btn.appendChild(renderBindingText(label));
    targetParent.appendChild(btn);
  }

  const INPUT_TYPE_PASSTHROUGH = new Set(["text", "email", "tel", "number", "date", "password", "search", "url"]);

  function renderInput(node, targetParent) {
    const type = (attr(node, "type") || "text").toLowerCase();
    const label = attr(node, "label") || "";
    const wrap = document.createElement("div");
    wrap.className = "slds-form-element";

    if (type === "checkbox") {
      const control = document.createElement("div");
      control.className = "slds-form-element__control";
      const lbl = document.createElement("label");
      lbl.className = "slds-checkbox";
      const faux = document.createElement("span");
      faux.className = "slds-checkbox_faux";
      const text = document.createElement("span");
      text.className = "slds-form-element__label";
      text.appendChild(renderBindingText(label));
      lbl.append(faux, text);
      control.appendChild(lbl);
      wrap.appendChild(control);
    } else {
      if (label) {
        const lbl = document.createElement("label");
        lbl.className = "slds-form-element__label";
        lbl.appendChild(renderBindingText(label));
        wrap.appendChild(lbl);
      }
      const control = document.createElement("div");
      control.className = "slds-form-element__control";
      const input = document.createElement("input");
      input.className = "slds-input";
      input.type = INPUT_TYPE_PASSTHROUGH.has(type) ? type : "text";
      input.disabled = true;
      const placeholder = attr(node, "placeholder");
      if (placeholder) input.setAttribute("placeholder", placeholder);
      control.appendChild(input);
      wrap.appendChild(control);
    }
    targetParent.appendChild(wrap);
  }

  function renderField(node, targetParent) {
    const fieldName = attr(node, "field-name") || "Field";
    const wrap = document.createElement("div");
    wrap.className = "slds-form-element";
    const lbl = document.createElement("label");
    lbl.className = "slds-form-element__label";
    lbl.textContent = fieldName;
    const control = document.createElement("div");
    control.className = "slds-form-element__control";
    const staticEl = document.createElement("div");
    staticEl.className = "slds-form-element__static";
    staticEl.textContent = "—";
    control.appendChild(staticEl);
    wrap.append(lbl, control);
    targetParent.appendChild(wrap);
  }

  function renderRecordForm(node, tag, targetParent) {
    const box = document.createElement("div");
    box.className = "lwc-record-form";
    const label = document.createElement("div");
    label.className = "lwc-record-form-label";
    label.textContent = tag === "lightning-record-edit-form" ? "Record Edit Form" : "Record Form";
    box.appendChild(label);
    walkChildren(node, box);
    targetParent.appendChild(box);
  }

  function renderLayout(node, targetParent) {
    const div = document.createElement("div");
    div.className = "slds-grid slds-wrap";
    walkChildren(node, div);
    targetParent.appendChild(div);
  }

  function renderLayoutItem(node, targetParent) {
    const div = document.createElement("div");
    div.className = "slds-col";
    const size = parseFloat(attr(node, "size"));
    if (!Number.isNaN(size) && size > 0) {
      const pct = Math.min(size / 12, 1) * 100 + "%";
      div.style.flex = "0 0 " + pct;
      div.style.maxWidth = pct;
    }
    walkChildren(node, div);
    targetParent.appendChild(div);
  }

  function renderIcon(node, targetParent) {
    const name = attr(node, "icon-name") || "";
    const span = document.createElement("span");
    span.className = "lwc-preview-icon";
    if (name) span.title = name;
    span.textContent = iconInitials(name);
    targetParent.appendChild(span);
  }

  function renderPassthroughTag(node, tag, targetParent) {
    const el = document.createElement(tag);
    Array.from(node.attributes).forEach((a) => {
      const name = a.name.toLowerCase();
      if (name.startsWith("on")) return; // never carry an event-handler attribute through
      if (tag === "a" && name === "href") {
        if (!/^\s*javascript:/i.test(a.value)) el.setAttribute("href", a.value);
        return;
      }
      if (tag === "img" && name === "src") {
        if (/^(https:|data:)/i.test(a.value.trim())) el.setAttribute("src", a.value);
        return;
      }
      if (SAFE_PASSTHROUGH_ATTRS.has(name)) el.setAttribute(name, a.value);
    });
    walkChildren(node, el);
    targetParent.appendChild(el);
  }

  function renderUnknownTag(node, tag, targetParent) {
    const box = document.createElement("div");
    box.className = "lwc-preview-unknown";
    box.textContent = "<" + tag + ">";
    targetParent.appendChild(box);
    walkChildren(node, box);
  }

  function renderLwcPreview(container, htmlSource) {
    container.innerHTML = "";
    const disclaimer = document.createElement("div");
    disclaimer.className = "lwc-preview-disclaimer";
    disclaimer.textContent = "Approximate preview — not a real render, no data, no interactivity.";
    container.appendChild(disclaimer);

    if (!htmlSource || !htmlSource.trim()) {
      const empty = document.createElement("div");
      empty.className = "lwc-preview-unknown";
      empty.textContent = "(no html to preview)";
      container.appendChild(empty);
      return;
    }

    let root = null;
    try {
      const doc = new DOMParser().parseFromString(htmlSource, "text/html");
      root = doc.querySelector("template");
    } catch (err) {
      root = null;
    }
    if (!root || !root.content) {
      const bad = document.createElement("div");
      bad.className = "lwc-preview-unknown";
      bad.textContent = "Could not parse this as an LWC template.";
      container.appendChild(bad);
      return;
    }
    walkChildren(root.content, container);
  }

  window.renderLwcPreview = renderLwcPreview;
})();
