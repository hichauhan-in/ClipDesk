// Choose a format and quality before saving. The same panel serves Outputs and
// the editor's media list, so "save it smaller" means the same thing everywhere.

import { api } from "../api.js";
import { h, openDialog, toast } from "../dom.js";

let cachedOptions = null;

async function loadOptions() {
  if (!cachedOptions) cachedOptions = await api.exportOptions();
  return cachedOptions;
}

/**
 * @param {object} params
 * @param {string} params.filename  What is being saved.
 * @param {"output"|"media"|"source"} params.sourceKind Where it lives.
 * @param {string} params.directUrl  Download link for the untouched original.
 */
export async function openExportDialog({
  project,
  filename,
  sourceKind = "output",
  directUrl = "",
  jobPanel,
  ctx,
}) {
  let options;
  try {
    options = await loadOptions();
  } catch (error) {
    toast(error.message, "err");
    return;
  }

  const isAudioSource = /\.(mp3|wav|m4a|aac|flac|ogg)$/i.test(filename);
  const formatSelect = h(
    "select",
    options.formats
      .filter((format) => !isAudioSource || format.audio_only)
      .map((format) => h("option", { value: format.id }, format.label))
  );
  const qualitySelect = h(
    "select",
    options.qualities.map((quality) =>
      h(
        "option",
        { value: quality.id, selected: quality.id === "balanced" },
        `${quality.label} — ${quality.description}`
      )
    )
  );
  const nameInput = h("input", { type: "text", placeholder: "Chosen automatically" });
  const qualityRow = h(
    "label.field",
    h("span", "Quality"),
    qualitySelect
  );

  function syncQualityVisibility() {
    const chosen = options.formats.find((format) => format.id === formatSelect.value);
    // Audio containers carry their own fixed bitrate, so a video quality is moot.
    qualityRow.style.display = chosen?.audio_only ? "none" : "";
  }
  formatSelect.onchange = syncQualityVisibility;
  syncQualityVisibility();

  const dialog = openDialog({
    title: `Save ${filename}`,
    hint: "Re-encodes a new file into Outputs. The original is left untouched.",
    body: h(
      "div.stack",
      h("label.field", h("span", "Format"), formatSelect),
      qualityRow,
      h("label.field", h("span", "Save as (optional)"), nameInput)
    ),
    actions: [
      directUrl
        ? h(
            "a.btn",
            { href: directUrl, download: filename, onclick: () => dialog.close() },
            "Download original"
          )
        : null,
      h(
        "button.btn.btn-primary",
        {
          onclick: () => {
            dialog.close();
            jobPanel.run(
              api.exportOutput(project.id, {
                source: filename,
                source_kind: sourceKind,
                format: formatSelect.value,
                quality: qualitySelect.value,
                output_name: nameInput.value.trim(),
              }),
              {
                title: `Exporting ${filename}`,
                onDone: (result) => {
                  toast(`${result.file} ready — see Outputs.`, "ok");
                  ctx.refresh();
                },
              }
            );
          },
        },
        "Export"
      ),
    ].filter(Boolean),
  });
}
