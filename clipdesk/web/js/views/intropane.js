// The Intro studio: pick a look, set the words, hear the bed, build the opener.
//
// The preview is CSS rather than a rendered sample. Encoding a real preview for
// every click would cost seconds each time; a looping mock of the same motion
// language answers "what will this feel like" instantly.

import { api, uploadIntroAudio } from "../api.js";
import { h, mount, numberField, toast } from "../dom.js";
import { runOrQueue } from "../queue.js";
import { createSoundtrackPicker } from "./soundtrackpicker.js";

const STYLE_JSON_EXAMPLE = `{
  "id": "team-open",
  "name": "Team open",
  "description": "A warm, measured internal opener.",
  "accent": "#f0a13c",
  "backdrop": "source-blur",
  "title_animation": "band-reveal",
  "shot_motions": ["drift", "punch-in"],
  "transition": "dissolve",
  "grade": "warm",
  "letterbox": true,
  "default_shots": 5,
  "default_duration_seconds": 14
}`;

function stylePreview(style) {
  if (!style) return h("div.intro-preview");
  const node = h("div.intro-preview", {
    "data-animation": style.title_animation,
    "data-backdrop": style.backdrop,
    style: { "--accent": style.accent },
  });
  mount(
    node,
    h("span.intro-preview-bg"),
    h("span.intro-preview-sweep"),
    h("span.intro-preview-panel"),
    h("span.intro-preview-rule"),
    h(
      "span.intro-preview-band",
      h("span.intro-preview-title", "TITLE"),
    ),
    h("span.intro-preview-underline"),
    h("span.intro-preview-sub", "subtitle line"),
    h(
      "span.intro-preview-shots",
      h("i"),
      h("i"),
      h("i"),
    ),
  );
  return node;
}

export function createIntroPane({ project, analysis, jobPanel, ctx, queue }) {
  const pane = h("div.stack.editor-tool-pane");

  const analysedTitle = analysis?.title || project.title || project.source_filename || "Overview";
  const overview = analysis?.abstract || analysis?.summary || "";
  const firstSentence = (overview.split(/(?<=[.!?])\s+/)[0] || "").slice(0, 150);

  const duration = numberField({ value: 14, min: 5, max: 60, step: 1 });
  const shots = numberField({ value: 5, min: 2, max: 12 });
  const outputName = h("input", { type: "text", value: "intro.mp4" });

  const titleInput = h("input", { type: "text", maxlength: 120, value: analysedTitle });
  const subtitleInput = h("input", { type: "text", maxlength: 200, value: firstSentence });
  const endCardInput = h("input", {
    type: "text",
    maxlength: 120,
    placeholder: "Blank uses the title",
  });
  const shotTags = h("input", { type: "checkbox", checked: true });
  const repeatTitle = h("input", { type: "checkbox" });

  const voiceover = h("input", { type: "checkbox" });
  const voiceSelect = h(
    "select",
    { disabled: true },
    h("option", { value: "" }, "Default Windows voice")
  );
  const narration = h("textarea", {
    rows: 2,
    disabled: true,
    value: firstSentence || analysedTitle,
    placeholder: "Spoken over the intro",
  });
  voiceover.onchange = () => {
    narration.disabled = !voiceover.checked;
    voiceSelect.disabled = !voiceover.checked;
  };

  const audioPicker = createSoundtrackPicker();
  const audioStatus = h("span.faint.small");
  const audioInput = h("input", {
    type: "file",
    accept: ".mp3,.wav,.m4a,.aac,.flac,.ogg,audio/*",
    style: { display: "none" },
    onchange: async () => {
      const file = audioInput.files?.[0];
      if (!file) return;
      try {
        audioStatus.textContent = `Uploading ${file.name}…`;
        const saved = await uploadIntroAudio(file, (fraction) => {
          audioStatus.textContent = `Uploading ${Math.round(fraction * 100)}%`;
        });
        await loadAudio(saved.id);
        audioStatus.textContent = `${saved.name} added.`;
      } catch (error) {
        audioStatus.textContent = error.message;
      } finally {
        audioInput.value = "";
      }
    },
  });

  const styleSelect = h("select");
  const styleDetail = h("div.faint.small");
  const styleTraits = h("div.intro-traits");
  const previewHost = h("div");
  const catalogHost = h("div", { style: { display: "none" } });
  const styleStatus = h("div.faint.small");

  let payload = { installed: [], catalog: [], voices: [] };
  let selectedId = "prestige";

  const importInput = h("input", {
    type: "file",
    accept: "application/json,.json",
    style: { display: "none" },
    onchange: async () => {
      const file = importInput.files?.[0];
      if (!file) return;
      try {
        const imported = await api.importIntroStyle(JSON.parse(await file.text()));
        selectedId = imported.id;
        styleStatus.textContent = `${imported.name} imported.`;
        await loadStyles();
      } catch (error) {
        styleStatus.textContent = error.message || "That style file is not valid JSON.";
      } finally {
        importInput.value = "";
      }
    },
  });

  function selected() {
    return payload.installed.find((style) => style.id === selectedId) || null;
  }

  function drawStyle() {
    const style = selected();
    mount(
      styleSelect,
      payload.installed.map((item) =>
        h("option", { value: item.id, selected: item.id === selectedId }, item.name)
      )
    );
    styleDetail.textContent = style ? style.description : "Choose an intro style.";
    mount(
      styleTraits,
      style
        ? [
            h("span.tag", `${style.title_animation.replace("-", " ")} title`),
            h("span.tag", style.backdrop.replace("-", " ")),
            h("span.tag", `${style.transition} cuts`),
            h("span.tag", style.grade),
            style.letterbox ? h("span.tag", "letterboxed") : null,
            style.grain ? h("span.tag", "film grain") : null,
          ].filter(Boolean)
        : []
    );
    // Remounting restarts the CSS animation, so the motion plays on every change.
    mount(previewHost, stylePreview(style));
  }

  function drawCatalog() {
    mount(
      catalogHost,
      payload.catalog.length
        ? h(
            "div.intro-catalog",
            h(
              "div.row-between",
              h("strong.small", "More intro styles"),
              h(
                "button.btn.btn-sm.btn-ghost",
                { onclick: () => (catalogHost.style.display = "none") },
                "Close"
              )
            ),
            payload.catalog.map((style) =>
              h(
                "div.row-between",
                h(
                  "div",
                  h("div.small", style.name),
                  h("div.faint.small", style.description)
                ),
                h(
                  "button.btn.btn-sm",
                  {
                    onclick: async () => {
                      await api.installIntroStyle(style.id);
                      selectedId = style.id;
                      await loadStyles();
                    },
                  },
                  "Add"
                )
              )
            )
          )
        : h("div.faint.small", "Every bundled style is installed.")
    );
  }

  styleSelect.onchange = () => {
    selectedId = styleSelect.value;
    const style = selected();
    if (style) {
      duration.value = style.default_duration_seconds;
      shots.value = style.default_shots;
    }
    drawStyle();
  };

  async function loadStyles() {
    try {
      payload = await api.introStyles();
      voiceover.disabled = !payload.voiceover_available;
      if (!payload.voiceover_available) {
        voiceover.checked = false;
        narration.disabled = true;
        voiceover.title = "Local Windows speech is not available on this machine.";
      }
      mount(
        voiceSelect,
        h("option", { value: "" }, "Default Windows voice"),
        (payload.voices || []).map((voice) => h("option", { value: voice }, voice))
      );
      if (!payload.installed.some((item) => item.id === selectedId)) {
        selectedId = payload.installed[0]?.id || "";
      }
      const style = selected();
      if (style) {
        duration.value = style.default_duration_seconds;
        shots.value = style.default_shots;
      }
      drawStyle();
      drawCatalog();
    } catch (error) {
      styleDetail.textContent = error.message;
    }
  }

  async function loadAudio(selectedAudio = "") {
    try {
      const library = await api.introAudio();
      audioPicker.draw([
        { label: "Built-in soundtracks", items: library.built_in },
        ...(library.imported.length
          ? [{ label: "Imported audio", items: library.imported }]
          : []),
      ]);
      audioPicker.value = selectedAudio || audioPicker.value || "cinematic";
    } catch (error) {
      audioStatus.textContent = error.message;
    }
  }

  mount(
    pane,
    h(
      "p.muted.small",
      analysis
        ? "An intro is built as a sequence: a cold open, a title reveal, a rhythm of " +
          "your highest-value moments, then an end card. Source audio is never used."
        : "An intro is built as a sequence: a cold open, a title reveal, a rhythm of " +
          "moments sampled across the video, then an end card. No transcript required."
    ),
    h(
      "div.intro-style-row",
      h("label.field", h("span", "Intro style"), styleSelect),
      h(
        "div.row",
        h(
          "button.btn.btn-sm",
          {
            onclick: () => {
              catalogHost.style.display = "";
              drawCatalog();
            },
          },
          "Get more"
        ),
        h("button.btn.btn-sm.btn-ghost", { onclick: () => importInput.click() }, "Import JSON")
      )
    ),
    importInput,
    catalogHost,
    styleStatus,
    // The look sits beside its settings rather than above them, so the whole
    // pane fits without scrolling past a full-width preview.
    h(
      "div.intro-layout",
      h("aside.intro-aside", previewHost, styleDetail, styleTraits),
      h(
        "div.intro-main",
        h(
          "div.field-grid",
          h("label.field", h("span", "Length (seconds)"), duration),
          h("label.field", h("span", "Moments to feature"), shots)
        ),
        h(
          "div.intro-columns",
          h(
            "section.pane-group",
            h("legend", "Words"),
            h("label.field", h("span", "Title"), titleInput),
            h("label.field", h("span", "Subtitle"), subtitleInput),
            h("label.field", h("span", "End card"), endCardInput),
            h("label.check", shotTags, "Label moments with their chapter"),
            h("label.check", repeatTitle, "Repeat the title over the first moment")
          ),
          h(
            "section.pane-group",
            h("legend", "Sound"),
            // Not a <label>: a label forwards stray clicks to the control it
            // wraps, which would reopen the list every time a track is chosen.
            h("div.field", h("span", "Soundtrack"), audioPicker.el),
            h(
              "div.row",
              h("button.btn.btn-sm", { onclick: () => audioInput.click() }, "Import audio"),
              audioInput,
              audioStatus
            ),
            h("label.check", voiceover, "Narrate with a local Windows voice"),
            h("label.field", h("span", "Voice"), voiceSelect),
            h(
              "div.row",
              h(
                "button.btn.btn-sm.btn-ghost",
                {
                  onclick: async () => {
                    try {
                      const result = await api.refreshIntroVoices();
                      mount(
                        voiceSelect,
                        h("option", { value: "" }, "Default Windows voice"),
                        result.voices.map((voice) => h("option", { value: voice }, voice))
                      );
                      toast(`${result.voices.length} installed voice(s) found.`, "ok");
                    } catch (error) {
                      toast(error.message, "err");
                    }
                  },
                },
                "Refresh voices"
              )
            ),
            h("label.field", h("span", "Narration"), narration)
          )
        )
      )
    ),
    h(
      "details",
      h("summary.small", "Custom style JSON format"),
      h("pre.snippet", STYLE_JSON_EXAMPLE),
      h(
        "div.faint.small",
        "Backdrops: source-blur, gradient, dark-panel, duotone, stage, grid. " +
          "Title animations: band-reveal, stack-lines, center-pop, side-panel, " +
          "flash-cut, split-bars, lower-third. Motions: punch-in, pull-back, whip, " +
          "drift, hold, tilt, glide. Grades: clean, cinematic, vivid, mono, warm, " +
          "cool, soft, crisp, noir, teal-orange, bleach, faded, midnight, sunset. " +
          "Transitions are ffmpeg xfade names such as dissolve, wipeleft, slideup, " +
          "circleopen, radial, squeezeh, pixelize and smoothleft."
      )
    ),
    h(
      "div.intro-footer",
      h("label.field", h("span", "Save as"), outputName),
      runOrQueue({
        label: "Build intro",
        onRun: () =>
          jobPanel.run(api.intro(project.id, introBody()), {
            title: "Building the intro",
            onDone: () => {
              toast("Intro ready — see Outputs.", "ok");
              ctx.refresh();
            },
          }),
        onQueue: () => queue.add((body) => api.intro(project.id, body), introBody()),
      })
    )
  );

  function introBody() {
    return {
      duration_seconds: duration.value,
      shot_count: shots.value,
      style_id: selectedId,
      title: titleInput.value.trim(),
      subtitle: subtitleInput.value.trim(),
      end_card_text: endCardInput.value.trim(),
      show_shot_tags: shotTags.checked,
      repeat_title: repeatTitle.checked,
      voiceover: voiceover.checked,
      voiceover_text: narration.value.trim(),
      voice_id: voiceSelect.value,
      audio_id: audioPicker.value || "cinematic",
      output_name: outputName.value || "intro.mp4",
    };
  }

  loadStyles();
  loadAudio();
  return pane;
}
