// A reusable "here are your options — pick the ones you want" list.
//
// Used by both the clip and highlight flows. Rendering is expensive and hard to
// undo, so nothing is encoded until the user has seen what is on offer and
// chosen. Each row carries a title and a two-line summary so that choice can be
// made without scrubbing through the video.

import { h, mount } from "./dom.js";
import { duration, timecode } from "./format.js";

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
 */
export function candidatePicker({
  candidates,
  actionLabel = "Render selected",
  allowCombine = false,
  combineDefault = false,
  combineLabel = "Join into a single video with transitions",
  outputNameDefault = "clip.mp4",
  onRender,
  onPreview,
}) {
  const selected = new Set(candidates.map((_, index) => index));
  let combine = combineDefault;
  const outputName = h("input", {
    type: "text",
    value: outputNameDefault,
    placeholder: "clip.mp4",
  });

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
      onRender(chosen, {
        combine: allowCombine && combine && chosen.length > 1,
        outputName: outputName.value.trim(),
      });
    }
  }

  const rows = candidates.map((candidate, index) => {
    const checkbox = h("input", {
      type: "checkbox",
      checked: true,
      onchange: (event) => {
        if (event.target.checked) selected.add(index);
        else selected.delete(index);
        row.classList.toggle("picked", event.target.checked);
        refresh();
      },
    });

    const row = h(
      "div.pick.picked",
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
        checked: combineDefault,
        onchange: (event) => {
          combine = event.target.checked;
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
