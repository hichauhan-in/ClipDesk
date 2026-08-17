// The notifications bell and its panel, mounted once in the sidebar footer.

import { h, mount } from "./dom.js";
import {
  clearAll,
  dismiss,
  markAllRead,
  subscribeNotifications,
} from "./notifications.js";

const KIND_CLASS = {
  error: "note-error",
  warn: "note-warn",
  ok: "note-ok",
  info: "note-info",
};

function whenText(at) {
  const seconds = Math.round((Date.now() - at) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

export function createNotificationCentre(onFollow) {
  const badge = h("span.note-count");
  const button = h(
    "button.note-bell",
    { type: "button", "aria-expanded": "false", title: "Messages" },
    bellIcon(),
    h("span", "Messages"),
    badge
  );

  const list = h("div.note-list");
  const clearButton = h("button.btn.btn-sm", { onclick: () => clearAll() }, "Clear all");
  const panel = h(
    "div.note-panel",
    { hidden: true },
    h(
      "div.row-between",
      h("strong.small", "Messages"),
      h("div.row", h("button.btn.btn-sm", { onclick: () => markAllRead() }, "Mark read"), clearButton)
    ),
    list
  );

  const wrapper = h("div.note-wrap", panel, button);
  let open = false;

  function toggle(next = !open) {
    open = next;
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (open) {
      const anchor = button.getBoundingClientRect();
      panel.style.left = `${Math.round(anchor.left)}px`;
      panel.style.bottom = `${Math.round(window.innerHeight - anchor.top + 8)}px`;
      markAllRead();
    }
  }

  button.addEventListener("click", () => toggle());
  document.addEventListener("click", (event) => {
    if (open && !wrapper.contains(event.target)) toggle(false);
  });

  subscribeNotifications((items) => {
    const unread = items.filter((item) => !item.read).length;
    badge.textContent = unread ? String(unread) : "";
    badge.hidden = unread === 0;
    button.classList.toggle("has-unread", unread > 0);
    clearButton.disabled = items.length === 0;

    if (!items.length) {
      mount(
        list,
        h("div.faint.small", { style: { padding: "12px 0" } }, "No messages.")
      );
      return;
    }
    mount(
      list,
      items.map((item) => row(item, onFollow, () => toggle(false)))
    );
  });

  return wrapper;
}

function row(item, onFollow, close) {
  const detail = item.detail
    ? h("div.note-detail.small", item.detail)
    : null;
  return h(
    "div.note-row",
    { class: KIND_CLASS[item.kind] || "note-info" },
    h(
      "div.row-between",
      h("strong.small", item.title),
      h("span.faint.small", whenText(item.at))
    ),
    detail,
    h(
      "div.row",
      { style: { marginTop: "6px" } },
      item.count > 1 ? h("span.tag", `${item.count}x`) : null,
      item.href
        ? h(
            "button.btn.btn-sm",
            {
              onclick: () => {
                close();
                onFollow(item.href);
              },
            },
            "Go there"
          )
        : null,
      h("button.btn.btn-sm", { onclick: () => dismiss(item.id) }, "Dismiss")
    )
  );
}

function bellIcon() {
  return h("svg", {
    viewBox: "0 0 16 16",
    width: "13",
    height: "13",
    "aria-hidden": "true",
    html:
      '<path fill="currentColor" d="M8 1.5a.75.75 0 0 1 .75.75v.42a4.25 4.25 0 0 1 3.5 4.18v2.06l.9 1.8A.75.75 0 0 1 12.48 12H9.9a2 2 0 0 1-3.8 0H3.52a.75.75 0 0 1-.67-1.09l.9-1.8V6.85a4.25 4.25 0 0 1 3.5-4.18v-.42A.75.75 0 0 1 8 1.5Zm0 2.6a2.75 2.75 0 0 0-2.75 2.75v2.24a.75.75 0 0 1-.08.33l-.4.83h6.46l-.4-.83a.75.75 0 0 1-.08-.33V6.85A2.75 2.75 0 0 0 8 4.1Z"/>',
  });
}
