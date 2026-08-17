// Minimal DOM helpers. Enough to build the whole UI without a framework.

import { notify } from "./notifications.js";

const SVG_NS = "http://www.w3.org/2000/svg";
// SVG lives in its own namespace. Built with createElement it becomes an unknown
// HTML element: it lays out as zero by zero and paints nothing, which is a
// silent failure rather than an error.
const SVG_TAGS = new Set(["svg", "path", "circle", "rect", "line", "polyline", "polygon", "g"]);

/**
 * h('div.card', { onclick }, child, child) → HTMLElement
 * The tag accepts CSS-ish shorthand: 'button.btn.btn-primary'.
 */
export function h(spec, props, ...children) {
  const [tag, ...classes] = String(spec).split(".");
  const name = tag || "div";
  const isSvg = SVG_TAGS.has(name);
  const el = isSvg ? document.createElementNS(SVG_NS, name) : document.createElement(name);
  if (classes.length) el.setAttribute("class", classes.join(" "));

  if (props && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) {
    children.unshift(props);
    props = null;
  }

  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") {
      el.setAttribute("class", `${el.getAttribute("class") || ""} ${value}`.trim());
    } else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "html") el.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (!isSvg && key in el && typeof value !== "object" && !key.startsWith("aria")) {
      // SVG properties are read-only mirrors of their attributes, so they always
      // go through setAttribute.
      el[key] = value;
    } else {
      el.setAttribute(key, value === true ? "" : value);
    }
  }

  append(el, children);
  return el;
}

function append(parent, children) {
  for (const child of children.flat(6)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(target, ...children) {
  target.replaceChildren();
  append(target, children);
  return target;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

/** Debounce, for inputs that trigger a network call. */
export function debounce(fn, ms = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/**
 * A number input with a themed stepper.
 *
 * The native spin buttons cannot be restyled — only hidden — and they render as
 * a bright block on hover that clashes with the dark theme. Returns the wrapper
 * with `.input` pointing at the field itself.
 */
export function numberField({ value = 0, min = null, max = null, step = 1, onchange } = {}) {
  const input = h("input", {
    type: "number",
    value: String(value),
    step: String(step),
    ...(min !== null ? { min: String(min) } : {}),
    ...(max !== null ? { max: String(max) } : {}),
  });

  const clamp = (raw) => {
    let next = Number(raw);
    if (!Number.isFinite(next)) next = Number(min ?? 0);
    if (min !== null) next = Math.max(min, next);
    if (max !== null) next = Math.min(max, next);
    // Keep the precision of the step rather than accumulating float noise.
    const decimals = (String(step).split(".")[1] || "").length;
    return Number(next.toFixed(decimals));
  };

  const nudge = (direction) => {
    input.value = String(clamp(Number(input.value) + direction * Number(step)));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    onchange?.(Number(input.value));
  };

  input.addEventListener("change", () => {
    input.value = String(clamp(input.value));
    onchange?.(Number(input.value));
  });

  const wrapper = h(
    "div.stepper",
    input,
    h(
      "div.stepper-buttons",
      h("button", { type: "button", tabindex: -1, onclick: () => nudge(1), "aria-label": "Increase" }, "▲"),
      h("button", { type: "button", tabindex: -1, onclick: () => nudge(-1), "aria-label": "Decrease" }, "▼")
    )
  );
  wrapper.input = input;
  Object.defineProperty(wrapper, "value", {
    get: () => Number(input.value),
    set: (next) => {
      input.value = String(clamp(next));
    },
  });
  return wrapper;
}

/**
 * A transient message. Errors are also recorded in the message centre, because
 * the one worth reading is usually the one that vanished while the user was
 * looking elsewhere. Pass ``record: false`` when the caller records it itself
 * with more context.
 */
export function toast(message, kind = "", { record = true } = {}) {
  if (kind === "err" && record) {
    notify({ kind: "error", title: String(message).split("\n")[0].slice(0, 90), detail: message });
  }
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = h(`div.toast${kind ? "." + kind : ""}`, message);
  host.append(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 320);
  }, kind === "err" ? 9000 : 4500);
}

/**
 * A modal panel. Returns a handle so callers can close it themselves; Escape,
 * the close button and a backdrop click all dismiss it.
 */
export function openDialog({ title, hint = "", body, actions = [], onClose } = {}) {
  const previouslyFocused = document.activeElement;
  const panel = h(
    "div.dialog",
    { role: "dialog", "aria-modal": "true", "aria-label": title || "Dialog" },
    h(
      "div.dialog-head",
      h("h2", title || ""),
      h("button.icon-btn", { "aria-label": "Close", onclick: () => close() }, "✕")
    ),
    hint ? h("p.muted.small", hint) : null,
    h("div.dialog-body", body),
    actions.length ? h("div.dialog-actions", actions) : null
  );
  const backdrop = h("div.dialog-backdrop", {
    onclick: (event) => {
      if (event.target === backdrop) close();
    },
  });
  backdrop.append(panel);

  function onKey(event) {
    if (event.key === "Escape") close();
  }

  function close() {
    document.removeEventListener("keydown", onKey);
    backdrop.remove();
    previouslyFocused?.focus?.();
    onClose?.();
  }

  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  panel.querySelector("button, input, select, textarea, a")?.focus?.();
  return { close, panel };
}

/**
 * A placeholder for a page that is still loading. Shaped like the page it
 * replaces so the layout does not jump, and animated so a slow route reads as
 * working rather than frozen.
 */
export function loadingView(message = "Loading…", { cards = 3, slowAfterMs = 4000 } = {}) {
  const label = h("span.small.muted", message);
  const view = h(
    "div.loading-view",
    h("div.loading-head", h("span.spinner", { "aria-hidden": "true" }), label),
    h(
      "div.stack",
      Array.from({ length: cards }, () =>
        h(
          "div.skeleton-card",
          h("span.skeleton.skeleton-title"),
          h("span.skeleton.skeleton-line"),
          h("span.skeleton.skeleton-line.short")
        )
      )
    )
  );
  view.setAttribute("role", "status");
  view.setAttribute("aria-live", "polite");

  // Say something rather than nothing when a check runs long.
  const timer = setTimeout(() => {
    if (view.isConnected) label.textContent = `${message} still working…`;
  }, slowAfterMs);
  new MutationObserver((_records, observer) => {
    if (!view.isConnected) {
      clearTimeout(timer);
      observer.disconnect();
    }
  }).observe(document.body, { childList: true, subtree: true });

  return view;
}

/** A confirm dialog that does not block the event loop like window.confirm. */
export function confirmAction(message, { confirmLabel = "Continue", danger = true } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    const dialog = openDialog({
      title: String(message).split("\n")[0].slice(0, 90),
      body: h(
        "div.stack",
        String(message)
          .split("\n")
          .slice(1)
          .filter((line) => line.trim())
          .map((line) => h("p.muted.small", line))
      ),
      actions: [
        h("button.btn", { onclick: () => finish(false) }, "Cancel"),
        h(
          `button.btn.btn-primary${danger ? ".btn-danger" : ""}`,
          { onclick: () => finish(true) },
          confirmLabel
        ),
      ],
      onClose: () => finish(false),
    });
  });
}
