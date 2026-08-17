// A soundtrack picker you can listen to before committing.
//
// A native <select> cannot hold a button, so this is a listbox that behaves like
// one: it reports a value, but every row also carries its own play control and
// only one track is ever audible.

import { h, mount } from "../dom.js";

export function createSoundtrackPicker({ onChange } = {}) {
  const audio = new Audio();
  let value = "";
  let playingId = "";
  let rows = [];

  const label = h("span.soundtrack-value", "Choose a soundtrack");
  const list = h("div.soundtrack-list", { role: "listbox", hidden: true });
  const toggle = h(
    "button.soundtrack-toggle",
    {
      type: "button",
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
      onclick: () => setOpen(list.hidden),
    },
    label,
    h("span.faint", "▾")
  );
  const root = h("div.soundtrack-picker", toggle, list);

  function setOpen(open) {
    list.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (!open) stop();
  }

  function stop() {
    audio.pause();
    audio.currentTime = 0;
    playingId = "";
    for (const row of rows) row.button.textContent = "▶";
  }

  function play(item, button) {
    if (playingId === item.id) {
      stop();
      return;
    }
    stop();
    playingId = item.id;
    button.textContent = "■";
    audio.src = `/api/intro/audio/preview?audio_id=${encodeURIComponent(item.id)}`;
    audio.play().catch(() => {
      button.textContent = "▶";
      playingId = "";
    });
  }

  audio.onended = () => stop();

  function draw(groups) {
    rows = [];
    mount(
      list,
      groups.map((group) =>
        h(
          "div.soundtrack-group",
          h("div.soundtrack-group-head", group.label),
          group.items.map((item) => {
            const button = h(
              "button.soundtrack-play",
              {
                type: "button",
                title: `Hear ${item.name}`,
                "aria-label": `Hear ${item.name}`,
                onclick: (event) => {
                  event.stopPropagation();
                  play(item, button);
                },
              },
              "▶"
            );
            const row = h(
              "div.soundtrack-row",
              {
                role: "option",
                tabindex: "0",
                "aria-selected": String(item.id === value),
                onclick: () => select(item.id),
                onkeydown: (event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    select(item.id);
                  }
                },
              },
              button,
              h(
                "div.soundtrack-text",
                h("strong.small", item.name),
                item.description ? h("div.faint.small", item.description) : null
              )
            );
            rows.push({ id: item.id, node: row, button });
            return row;
          })
        )
      )
    );
    markSelection();
  }

  function markSelection() {
    const chosen = rows.find((row) => row.id === value);
    label.textContent = chosen
      ? chosen.node.querySelector("strong").textContent
      : "Choose a soundtrack";
    for (const row of rows) {
      row.node.setAttribute("aria-selected", String(row.id === value));
      row.node.classList.toggle("selected", row.id === value);
    }
  }

  function select(next) {
    value = next;
    markSelection();
    setOpen(false);
    onChange?.(value);
  }

  // Clicking elsewhere closes the list, which also stops whatever is playing.
  document.addEventListener("click", (event) => {
    if (!root.contains(event.target)) setOpen(false);
  });

  return {
    el: root,
    draw,
    stop,
    get value() {
      return value;
    },
    set value(next) {
      value = next;
      markSelection();
    },
  };
}
