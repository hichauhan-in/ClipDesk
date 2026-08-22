// A buffered soundtrack picker with gapless preview switching.

import { h, mount } from "../dom.js";

const CROSSFADE_SECONDS = 0.3;
const PREWARM_CONCURRENCY = 2;

export function createSoundtrackPicker({ onChange } = {}) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const encoded = new Map();
  const decoded = new Map();
  const prewarmQueue = [];
  let prewarming = 0;
  let context = null;
  let active = null;
  let value = "";
  let playingId = "";
  let requestToken = 0;
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

  function ensureContext() {
    if (!context && AudioContextClass) context = new AudioContextClass();
    return context;
  }

  function previewUrl(id) {
    return `/api/intro/audio/preview?audio_id=${encodeURIComponent(id)}`;
  }

  function fetchPreview(id) {
    if (!encoded.has(id)) {
      encoded.set(
        id,
        fetch(previewUrl(id), { cache: "force-cache" })
          .then((response) => {
            if (!response.ok) throw new Error(`Preview failed (${response.status})`);
            return response.arrayBuffer();
          })
          .catch((error) => {
            encoded.delete(id);
            throw error;
          })
      );
    }
    return encoded.get(id);
  }

  async function decodePreview(id) {
    if (!decoded.has(id)) {
      const audioContext = ensureContext();
      if (!audioContext) throw new Error("Audio previews are not supported by this browser.");
      decoded.set(
        id,
        fetchPreview(id)
          .then((bytes) => audioContext.decodeAudioData(bytes.slice(0)))
          .catch((error) => {
            decoded.delete(id);
            throw error;
          })
      );
    }
    return decoded.get(id);
  }

  function queuePrewarm(id) {
    if (encoded.has(id) || prewarmQueue.includes(id)) return;
    prewarmQueue.push(id);
    pumpPrewarm();
  }

  function pumpPrewarm() {
    while (prewarming < PREWARM_CONCURRENCY && prewarmQueue.length) {
      const id = prewarmQueue.shift();
      prewarming += 1;
      fetchPreview(id)
        .catch(() => {})
        .finally(() => {
          prewarming -= 1;
          pumpPrewarm();
        });
    }
  }

  function setOpen(open) {
    const wasOpen = !list.hidden;
    list.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (wasOpen && !open) stop();
  }

  function resetIcons() {
    for (const row of rows) {
      if (row.button instanceof HTMLButtonElement) {
        row.button.textContent = row.id === playingId ? "■" : "▶";
        row.button.removeAttribute("aria-busy");
      }
    }
  }

  function retire(channel, delayMs = 0) {
    if (!channel) return;
    window.setTimeout(() => {
      try {
        channel.source.stop();
      } catch {
        // It may already have ended naturally.
      }
      channel.source.disconnect();
      channel.gain.disconnect();
    }, delayMs);
  }

  function stop({ fade = true } = {}) {
    requestToken += 1;
    playingId = "";
    resetIcons();
    if (!active || !context) return;
    const previous = active;
    active = null;
    const now = context.currentTime;
    previous.gain.gain.cancelScheduledValues(now);
    previous.gain.gain.setValueAtTime(previous.gain.gain.value, now);
    previous.gain.gain.linearRampToValueAtTime(0, now + (fade ? 0.12 : 0.01));
    retire(previous, fade ? 150 : 20);
  }

  async function play(item, button) {
    if (playingId === item.id) {
      stop();
      return;
    }

    const audioContext = ensureContext();
    if (!audioContext) return;
    const token = ++requestToken;
    playingId = item.id;
    resetIcons();
    button.textContent = "…";
    button.setAttribute("aria-busy", "true");

    // Resume inside the click turn so autoplay policy sees user intent.
    const resume = audioContext.state === "suspended" ? audioContext.resume() : Promise.resolve();
    try {
      const [, buffer] = await Promise.all([resume, decodePreview(item.id)]);
      if (token !== requestToken || playingId !== item.id) return;

      const source = audioContext.createBufferSource();
      const gain = audioContext.createGain();
      source.buffer = buffer;
      source.connect(gain);
      gain.connect(audioContext.destination);

      const previous = active;
      const now = audioContext.currentTime;
      gain.gain.setValueAtTime(0, now);
      source.start(now);
      gain.gain.linearRampToValueAtTime(1, now + (previous ? CROSSFADE_SECONDS : 0.08));
      active = { id: item.id, source, gain };

      if (previous) {
        previous.gain.gain.cancelScheduledValues(now);
        previous.gain.gain.setValueAtTime(previous.gain.gain.value, now);
        previous.gain.gain.linearRampToValueAtTime(0, now + CROSSFADE_SECONDS);
        retire(previous, (CROSSFADE_SECONDS + 0.05) * 1000);
      }

      source.onended = () => {
        if (active?.source !== source) return;
        active = null;
        playingId = "";
        resetIcons();
      };
      resetIcons();
    } catch {
      if (token === requestToken) stop({ fade: false });
    }
  }

  function draw(groups) {
    rows = [];
    const playable = groups.flatMap((group) => group.items).filter((item) => item.previewable !== false);
    mount(
      list,
      groups.map((group) =>
        h(
          "div.soundtrack-group",
          h("div.soundtrack-group-head", group.label),
          group.items.map((item) => {
            const button = item.previewable === false
              ? h("span.soundtrack-play", { "aria-hidden": "true" }, "")
              : h(
                  "button.soundtrack-play",
                  {
                    type: "button",
                    title: `Hear ${item.name}`,
                    "aria-label": `Hear ${item.name}`,
                    onpointerenter: () => queuePrewarm(item.id),
                    onfocus: () => queuePrewarm(item.id),
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

    // Prepare cold server/browser caches quietly. Two-at-a-time avoids saturating
    // disk or network while making later clicks immediate.
    const schedule = window.requestIdleCallback || ((callback) => window.setTimeout(callback, 250));
    schedule(() => playable.forEach((item) => queuePrewarm(item.id)));
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
    stop();
    value = next;
    markSelection();
    setOpen(false);
    onChange?.(value);
  }

  document.addEventListener("click", (event) => {
    if (!root.contains(event.target) && !list.hidden) setOpen(false);
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
