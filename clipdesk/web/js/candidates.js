// A reusable "here are your options — pick the ones you want" list.
//
// Used by both the clip and highlight flows. Rendering is expensive and hard to
// undo, so nothing is encoded until the user has seen what is on offer and
// chosen. Each row carries a title and a two-line summary so that choice can be
// made without scrubbing through the video.

import { h, mount } from "./dom.js";
import { duration, timecode } from "./format.js";

// Picking is real work: a dozen decisions and a filename. Losing it because the
// user looked at the transcript means doing all of it again, so it is kept for
// the session and dropped once the choice has actually been acted on.
const PICK_PREFIX = "clipdesk:pick:";

function readPick(key) {
  if (!key) return null;
  try {
    return JSON.parse(sessionStorage.getItem(PICK_PREFIX + key) || "null");
  } catch {
    return null;
  }
}

function writePick(key, value) {
  if (!key) return;
  try {
    sessionStorage.setItem(PICK_PREFIX + key, JSON.stringify(value));
  } catch {
    /* Private mode or a full store: the picker still works, it just forgets. */
  }
}

function forgetPick(key) {
  if (!key) return;
  try {
    sessionStorage.removeItem(PICK_PREFIX + key);
  } catch {
    /* nothing to do */
  }
}

/**
 * @param {object} options
 * @param {Array} options.candidates
 * @param {string} options.actionLabel   text for the render button
 * @param {boolean} options.allowCombine offer "join into one video"
 * @param {boolean} options.combineDefault
 * @param {string} options.combineLabel
 * @param {string} options.outputNameDefault
 * @param {(selection: Array, opts: {combine: boolean, outputName: string}) => void} options.onRender
 * @param {(candidate: object) => void} [options.onPreview]
 * @param {string} [options.stateKey] identifies this set of options, so the
 *   choices made against it survive leaving the tab
 */
export function candidatePicker({
  candidates,
  actionLabel = "Render selected",
  allowCombine = false,
  combineDefault = false,
  combineLabel = "Join into a single video with transitions",
  outputNameDefault = "clip.mp4",
  stateKey = "",
  onRender,
  onPreview,
}) {
  const saved = readPick(stateKey);
  const restored = saved && saved.total === candidates.length ? saved : null;
  const selected = new Set(
    restored ? restored.selected.filter((index) => index < candidates.length) : candidates.map((_, index) => index)
  );
  let combine = restored ? restored.combine : combineDefault;
  const outputName = h("input", {
    type: "text",
    value: restored ? restored.outputName : outputNameDefault,
    placeholder: "clip.mp4",
    oninput: () => remember(),
  });

  function remember() {
    writePick(stateKey, {
      total: candidates.length,
      selected: [...selected],
      combine,
      outputName: outputName.value,
    });
  }

  const summaryLine = h("div.muted.small");
  const combineRow = h("div", { style: { display: allowCombine ? "" : "none" } });
  const renderButton = h(
    "button.btn.btn-primary",
    { onclick: () => submit() },
    actionLabel
  );

  function refresh() {
    const chosen = [...selected];
    const total = chosen.reduce(
      (sum, index) => sum + (candidates[index].end - candidates[index].start),
      0
    );
    summaryLine.textContent = chosen.length
      ? `${chosen.length} of ${candidates.length} selected · ${duration(total)} total`
      : "Nothing selected";
    renderButton.disabled = chosen.length === 0;
    combineRow.style.display = allowCombine && chosen.length > 1 ? "" : "none";
  }

  function submit() {
    const chosen = [...selected]
      .sort((a, b) => a - b)
      .map((index) => candidates[index]);
    if (chosen.length) {
      forgetPick(stateKey);
      onRender(chosen, {
        combine: allowCombine && combine && chosen.length > 1,
        outputName: outputName.value.trim(),
      });
    }
  }

  const rows = candidates.map((candidate, index) => {
    const picked = selected.has(index);
    const checkbox = h("input", {
      type: "checkbox",
      checked: picked,
      onchange: (event) => {
        if (event.target.checked) selected.add(index);
        else selected.delete(index);
        row.classList.toggle("picked", event.target.checked);
        remember();
        refresh();
      },
    });

    const row = h(
      `div.pick${picked ? ".picked" : ""}`,
      h("label.pick-check", checkbox),
      h(
        "div.pick-body",
        h(
          "div.row",
          h("strong", candidate.title || `Clip ${index + 1}`),
          h(
            "span.tag",
            `${timecode(candidate.start)} – ${timecode(candidate.end)}`
          ),
          h("span.tag", duration(candidate.end - candidate.start)),
          candidate.score !== undefined
            ? h("span.faint.small.mono", candidate.score.toFixed(2))
            : null
        ),
        candidate.summary ? h("div.muted.small", candidate.summary) : null,
        candidate.reason ? h("div.faint.small", candidate.reason) : null
      ),
      onPreview
        ? h(
            "button.btn.btn-sm.btn-ghost",
            { onclick: () => onPreview(candidate), title: "Jump to this point" },
            "Preview"
          )
        : null
    );
    return row;
  });

  mount(
    combineRow,
    h(
      "label.check",
      h("input", {
        type: "checkbox",
        checked: combine,
        onchange: (event) => {
          combine = event.target.checked;
          remember();
        },
      }),
      combineLabel
    )
  );

  const container = h(
    "div.stack",
    h(
      "div.row-between",
      summaryLine,
      h(
        "div.row",
        h(
          "button.btn.btn-sm.btn-ghost",
          {
            onclick: () => {
              candidates.forEach((_, index) => selected.add(index));
              container.querySelectorAll(".pick input").forEach((box) => (box.checked = true));
              container.querySelectorAll(".pick").forEach((el) => el.classList.add("picked"));
              remember();
              refresh();
            },
          },
          "Select all"
        ),
        h(
          "button.btn.btn-sm.btn-ghost",
          {
            onclick: () => {
              selected.clear();
              container.querySelectorAll(".pick input").forEach((box) => (box.checked = false));
              container.querySelectorAll(".pick").forEach((el) => el.classList.remove("picked"));
              remember();
              refresh();
            },
          },
          "Clear"
        )
      )
    ),
    h("div.stack", rows),
    combineRow,
    h(
      "label.field",
      h("span", "Save as"),
      outputName,
      h(
        "span.faint.small",
        "When exporting separate clips, ClipDesk adds -01, -02 and so on."
      )
    ),
    h("div.row", renderButton)
  );

  refresh();
  return container;
}
