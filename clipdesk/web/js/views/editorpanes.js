// Editor panes: clipping (assemble + trim) and the prompt director.
//
// The director plans first and runs second. A prompt is a guess about intent, so
// the plan is always shown before anything is rendered, and every intent is
// carried out by the same typed endpoint its own panel uses.

import { api } from "../api.js";
import { h, mount, numberField, toast } from "../dom.js";
import { timecode } from "../format.js";
import { runOrQueue, withQueued } from "../queue.js";

const TRANSITIONS = [
  ["cut", "Cut"],
  ["fade", "Fade"],
  ["dissolve", "Dissolve"],
  ["wipe-left", "Wipe left"],
  ["slide-left", "Slide left"],
];

/** Accepts 90, 1:30 or 00:01:30 and returns seconds, or null when blank. */
export function parseTimecode(value) {
  const clean = String(value ?? "").trim();
  if (!clean) return null;
  if (!/^\d{1,2}(:\d{1,2}){0,2}(\.\d+)?$/.test(clean)) return NaN;
  return clean.split(":").reduce((total, part) => total * 60 + Number(part), 0);
}

export function clippingPane({ project, state, videoOutputs, jobPanel, ctx, queue }) {
  const selection = {
    header: "",
    footer: "",
    body: "",
    introTransition: "fade",
    outroTransition: "fade",
  };

  const transitionSelect = (key) =>
    h(
      "select",
      { onchange: (event) => (selection[key] = event.target.value) },
      TRANSITIONS.map(([value, label]) =>
        h("option", { value, selected: value === selection[key] }, label)
      )
    );

  const mediaOptions = (onchange) =>
    h(
      "select",
      { onchange },
      h("option", { value: "" }, "None"),
      state.assets.map((asset) => h("option", { value: asset.name }, asset.name))
    );

  const headerSelect = mediaOptions((event) => (selection.header = event.target.value));
  const footerSelect = mediaOptions((event) => (selection.footer = event.target.value));
  const bodySelect = h("select", {
    onchange: (event) => (selection.body = event.target.value),
  });

  // Files the queue has not written yet still belong in this list: choosing one
  // is what makes this step run after the step that writes it.
  function drawBodyOptions() {
    const existing = videoOutputs.map((artifact) => [
      artifact.filename,
      `${artifact.label} (${artifact.filename})`,
    ]);
    const options = queue ? withQueued(existing, queue) : existing;
    mount(
      bodySelect,
      h("option", { value: "" }, `Original recording — ${project.source_filename}`),
      options.map(([value, label]) =>
        h("option", { value, selected: value === selection.body }, label)
      )
    );
  }
  drawBodyOptions();
  queue?.subscribe(drawBodyOptions);

  const trimStart = h("input", { type: "text", placeholder: "0:00", inputmode: "numeric" });
  const trimEnd = h("input", { type: "text", placeholder: "end", inputmode: "numeric" });
  const outputName = h("input", { type: "text", value: "final.mp4" });

  return h(
    "div.stack.editor-tool-pane",
    h(
      "p.muted.small",
      "Top and tail the video, trim its ends, or both. Every part is normalized onto " +
        "one canvas and audio layout before joining."
    ),
    h("label.field", h("span", "Main video"), bodySelect),
    h(
      "fieldset.pane-group",
      h("legend", "Trim"),
      h(
        "div.field-grid",
        h("label.field", h("span", "Start at"), trimStart),
        h("label.field", h("span", "End at"), trimEnd)
      ),
      h("div.faint.small", "Leave blank to keep the whole video. Use m:ss or h:mm:ss.")
    ),
    h(
      "fieldset.pane-group",
      h("legend", "Attach"),
      h(
        "div.field-grid",
        h("label.field", h("span", "Intro"), headerSelect),
        h("label.field", h("span", "Intro transition"), transitionSelect("introTransition")),
        h("label.field", h("span", "Outro"), footerSelect),
        h("label.field", h("span", "Outro transition"), transitionSelect("outroTransition"))
      ),
      state.assets.length
        ? null
        : h("div.faint.small", "Import media above to attach an intro or outro.")
    ),
    h("label.field", h("span", "Save as"), outputName),
    runOrQueue({
      label: "Assemble and render",
      onRun: () => {
        const body = assembleBody();
        if (!body) return;
        jobPanel.run(api.bookend(project.id, body), {
          title: "Assembling",
          onDone: () => {
            toast("Final cut ready — see Outputs.", "ok");
            ctx.refresh();
          },
        });
      },
      onQueue: () => {
        const body = assembleBody();
        if (!body) return Promise.resolve();
        return queue.add((payload) => api.bookend(project.id, payload), body);
      },
    })
  );

  /** The assemble request, or null when the form does not describe an edit. */
  function assembleBody() {
    const start = parseTimecode(trimStart.value);
    const end = parseTimecode(trimEnd.value);
    if (Number.isNaN(start) || Number.isNaN(end)) {
      toast("Use m:ss or h:mm:ss for the trim points.", "err");
      return null;
    }
    if (start !== null && end !== null && end <= start) {
      toast("The trim must end after it starts.", "err");
      return null;
    }
    if (!selection.header && !selection.footer && start === null && end === null) {
      toast("Choose an intro, an outro, or a trim range.", "err");
      return null;
    }
    return {
      body_filename: selection.body,
      header_asset: selection.header || null,
      footer_asset: selection.footer || null,
      intro_transition: selection.introTransition,
      outro_transition: selection.outroTransition,
      trim_start: start ?? 0,
      trim_end: end,
      output_name: outputName.value || "final.mp4",
    };
  }
}

/**
 * Examples drawn from this recording, so the first thing a user sees is an
 * instruction that would actually work here rather than a generic placeholder.
 */
function buildExamples({ project, analysis, state }) {
  const title = (analysis?.title || project.title || "").trim();
  const chapters = analysis?.chapters || [];
  const keywords = analysis?.keywords || [];
  const outputs = (project.artifacts || []).filter((artifact) =>
    /\.(mp4|mov|mkv|webm)$/i.test(artifact.filename)
  );
  const media = state.assets || [];
  const short = (value, limit = 46) =>
    value.length > limit ? `${value.slice(0, limit - 1).trimEnd()}…` : value;
  // An overlay example becomes real text on the frame, so trim to whole words.
  const words = (value, count) => value.split(/\s+/).slice(0, count).join(" ");

  const examples = [];
  examples.push(
    title
      ? `Create a cinematic intro titled "${short(title)}"`
      : "Create a cinematic intro that runs 12 seconds"
  );
  examples.push("Clean up the recording and remove the pauses");

  const topic = keywords[0] || chapters[0]?.title;
  if (topic) examples.push(`Find the parts about "${short(String(topic), 40)}"`);

  const chapter = chapters.find((item) => item.end > item.start);
  if (chapter) {
    examples.push(`Clip from ${timecode(chapter.start)} to ${timecode(chapter.end)}`);
  }
  examples.push(`Find ${Math.min(5, Math.max(3, chapters.length || 3))} highlights`);
  examples.push(
    title ? `Make an outro saying "Thanks for watching"` : "Make an outro with an end card"
  );
  if (media.length) {
    examples.push(`Attach intro ${media[0].name}`);
  }
  examples.push("Trim the first 30 seconds");
  examples.push("Make a 30 second edit from the middle");
  examples.push("Make it vertical for reels and add a fade out");
  examples.push("Speed it up 1.5x and sharpen");
  if (chapter) {
    examples.push(
      `Keep ${timecode(chapter.start)} to ${timecode(chapter.end)}, make it square with a cinematic look`
    );
  }
  if (outputs.length) {
    examples.push(`Export ${outputs[0].filename} as a small mp4`);
  }
  examples.push("Just the audio as mp3");
  if (title) {
    examples.push(`Add text "${words(title, 3)}" bottom right from 00:10 to 00:25`);
  }
  examples.push("Mute audio and make it black and white");
  return examples;
}

const INTENT_LABEL = {
  intro: "Intro",
  outro: "Outro",
  clip: "Clip",
  clean: "Clean cut",
  assemble: "Assemble",
  export: "Export",
  effects: "Effects",
};

const CLEANUP_TOGGLES = [
  ["remove_silence", "Long silences"],
  ["remove_filler", "Filler and false starts"],
  ["remove_admin", "Logistics"],
  ["remove_off_topic", "Off-topic chatter"],
  ["remove_qa", "Q&A"],
  ["remove_intro", "Intro"],
  ["remove_outro", "Outro and sign-off"],
];

/**
 * What the plan got from your wording, as controls rather than prose. Every
 * value the director guessed is a starting point, so each one is editable
 * before anything renders.
 */
function planFields(plan, { styles, formats, qualities, media, outputs }) {
  const params = plan.params || {};
  const styleOptions = styles.length
    ? styles.map((style) => [style.id, style.name])
    : [[params.style_id, params.style_id]];
  const names = media.map((item) => item.name);
  // Whatever the director picked has to stay selectable even if the media list
  // has not caught up with it.
  for (const picked of [params.header_asset, params.footer_asset]) {
    if (picked && !names.includes(picked)) names.push(picked);
  }
  const mediaOptions = [["", "None"], ...names.map((name) => [name, name])];

  if (plan.intent === "intro" || plan.intent === "outro") {
    return [
      { key: "style_id", label: "Style", type: "select", options: styleOptions },
      { key: "duration_seconds", label: "Length (seconds)", type: "number", min: 5, max: 60 },
      { key: "shot_count", label: "Moments to feature", type: "number", min: 2, max: 12 },
      { key: "title", label: "Title", type: "text", wide: true },
      ...(plan.intent === "outro"
        ? [{ key: "end_card_text", label: "End card", type: "text", wide: true }]
        : []),
    ];
  }
  if (plan.intent === "clean") {
    return CLEANUP_TOGGLES.map(([key, label]) => ({ key, label, type: "checkbox" }));
  }
  if (plan.intent === "export") {
    const names = outputs.map((item) => item.filename);
    // The director may have picked the source video, which is not an output.
    if (params.source && !names.includes(params.source)) names.unshift(params.source);
    return [
      { key: "source", label: "File", type: "select", options: names.map((n) => [n, n]) },
      { key: "format", label: "Format", type: "select", options: formats },
      { key: "quality", label: "Quality", type: "select", options: qualities },
    ];
  }
  if (plan.intent === "assemble") {
    return [
      { key: "header_asset", label: "Intro", type: "select", options: mediaOptions },
      {
        key: "intro_transition",
        label: "Intro transition",
        type: "select",
        options: TRANSITIONS,
      },
      { key: "footer_asset", label: "Outro", type: "select", options: mediaOptions },
      {
        key: "outro_transition",
        label: "Outro transition",
        type: "select",
        options: TRANSITIONS,
      },
      { key: "trim_start", label: "Start at (seconds)", type: "number", min: 0 },
    ];
  }
  if (plan.intent === "clip") {
    if (params.mode === "span") {
      return [
        { key: "start", label: "From (seconds)", type: "number", min: 0 },
        { key: "end", label: "To (seconds)", type: "number", min: 0 },
        { key: "title", label: "Title", type: "text", wide: true },
      ];
    }
    return [
      {
        key: "mode",
        label: "Look for",
        type: "select",
        options: [
          ["duration", "A clip of a given length"],
          ["topic", "Everywhere a topic comes up"],
          ["highlight", "Standalone highlights"],
        ],
      },
      { key: "count", label: "How many", type: "number", min: 1, max: 20 },
      { key: "query", label: "About", type: "text", wide: true },
    ];
  }
  return [];
}

/** Builds the controls and hands back a reader for their current values. */
function fieldControls(fields, params, onedit) {
  const readers = [];
  const nodes = fields.map((field) => {
    const value = params[field.key];
    if (field.type === "checkbox") {
      const input = h("input", { type: "checkbox", checked: Boolean(value), onchange: onedit });
      readers.push([field.key, () => input.checked]);
      return h("label.check.plan-field-check", input, field.label);
    }
    if (field.type === "select") {
      const select = h(
        "select",
        { onchange: onedit },
        field.options.map(([optionValue, label]) =>
          h(
            "option",
            { value: optionValue, selected: String(optionValue) === String(value ?? "") },
            label
          )
        )
      );
      readers.push([field.key, () => select.value]);
      return h("label.field", h("span", field.label), select);
    }
    if (field.type === "number") {
      const input = numberField({
        value: Number(value ?? 0),
        min: field.min ?? null,
        max: field.max ?? null,
        onchange: onedit,
      });
      readers.push([field.key, () => Number(input.value)]);
      return h("label.field", h("span", field.label), input);
    }
    const input = h("input", {
      type: "text",
      value: value == null ? "" : String(value),
      onchange: onedit,
    });
    readers.push([field.key, () => input.value.trim()]);
    return h(
      "label.field",
      { class: field.wide ? "plan-field-wide" : null },
      h("span", field.label),
      input
    );
  });
  return {
    nodes,
    read: () => Object.fromEntries(readers.map(([key, get]) => [key, get()])),
  };
}

export function promptPane({ project, analysis, state, jobPanel, ctx, queue, onGoToTab }) {
  const examples = buildExamples({ project, analysis, state });
  const prompt = h("textarea", {
    rows: 3,
    placeholder: `Describe what you want. For example: ${examples[0]}`,
  });
  const outputName = h("input", {
    type: "text",
    value: "",
    placeholder: "Named automatically",
  });
  const planBox = h("div.plan-box");
  const continueButton = h("button.btn.btn-primary", "Continue");
  let current = null;
  //: Steps the user has kept. Only an effects plan has separable steps.
  let chosen = new Set();
  //: Live values from the plan's own controls, applied over what was parsed.
  let readFields = null;
  //: Option lists the plan editor needs, fetched once each.
  const lookups = { styles: null, formats: null, qualities: null };

  async function ensureLookups(intent) {
    if ((intent === "intro" || intent === "outro") && !lookups.styles) {
      lookups.styles = await api
        .introStyles()
        .then((payload) => payload.installed || [])
        .catch(() => []);
    }
    if (intent === "export" && !lookups.formats) {
      const options = await api.exportOptions().catch(() => ({ formats: [], qualities: [] }));
      lookups.formats = (options.formats || []).map((item) => [item.id, item.label]);
      lookups.qualities = (options.qualities || []).map((item) => [item.id, item.label]);
    }
  }

  function resetPlan(message) {
    current = null;
    chosen = new Set();
    readFields = null;
    mount(planBox, h("div.faint.small", message));
  }

  prompt.oninput = () => resetPlan("Describe what you want, then choose Continue.");

  function runPlan() {
    if (!current) return;
    execute(current, planSteps());
  }

  function queuePlan() {
    if (!current) return Promise.resolve();
    return enqueue(current, planSteps());
  }

  //: Only an effects plan has separable steps; everything else runs whole.
  function planSteps() {
    return current.intent === "effects" ? [...chosen].sort((a, b) => a - b) : null;
  }

  function showPlan(plan) {
    current = plan;
    const separable = plan.intent === "effects" && plan.steps.length > 1;
    chosen = new Set(plan.steps.map((_step, index) => index));

    const actions = runOrQueue({ label: "Create", onRun: runPlan, onQueue: queuePlan });
    const runButton = actions.run;
    //: Only the effects plan can run a subset, so only it needs a count.
    const stepCount = h("span.faint.small");
    function refreshRun() {
      runButton.disabled = separable && !chosen.size;
      actions.queue.disabled = runButton.disabled;
      stepCount.textContent = separable
        ? `${chosen.size} of ${plan.steps.length} steps`
        : "";
    }

    const steps = plan.steps.map((step, index) =>
      separable
        ? h(
            "label.check.plan-step",
            h("input", {
              type: "checkbox",
              checked: true,
              onchange: (event) => {
                if (event.target.checked) chosen.add(index);
                else chosen.delete(index);
                refreshRun();
              },
            }),
            step
          )
        : h("li", step)
    );

    refreshRun();

    const fields = planFields(plan, {
      styles: lookups.styles || [],
      formats: lookups.formats || [],
      qualities: lookups.qualities || [],
      media: state.assets || [],
      outputs: [
        ...(project.artifacts || []).filter((artifact) =>
          /\.(mp4|mov|mkv|webm)$/i.test(artifact.filename)
        ),
        // A file the queue will write is a legitimate thing to point at.
        ...(queue?.produces || []).map((filename) => ({ filename })),
      ],
    });
    // The director's sentence describes what it read from the wording. Once a
    // control is touched it no longer does, so say so rather than lie.
    const summary = h("strong.small", plan.summary);
    const markEdited = () => {
      summary.textContent = "Running with your changes below.";
      summary.classList.add("faint");
    };
    const controls = fields.length ? fieldControls(fields, plan.params || {}, markEdited) : null;
    readFields = controls ? controls.read : null;
    // Naming comes last, next to the button that acts on the name.
    outputName.placeholder = plan.params?.output_name || "Named automatically";

    mount(
      planBox,
      h("div.plan-head", h("span.pill.pill-accent", INTENT_LABEL[plan.intent] || plan.intent), summary),
      controls
        ? h(
            "div.plan-edit",
            h("div.faint.small", "Every value came from your wording. Change any of them."),
            h(plan.intent === "clean" ? "div.stack.plan-toggles" : "div.field-grid", controls.nodes)
          )
        : separable
          ? h(
              "div.plan-choices",
              h("div.faint.small", "Untick anything you do not want."),
              h("div.stack", steps)
            )
          : h("ul.plan-steps", steps),
      plan.notes?.length ? h("div.faint.small", plan.notes.join(" ")) : null,
      h("label.field.plan-name", h("span", "Save as"), outputName),
      h("div.row-between.plan-actions", stepCount, actions)
    );
  }

  continueButton.onclick = async () => {
    const text = prompt.value.trim();
    if (!text) {
      toast("Describe what you want first.", "err");
      return;
    }
    continueButton.disabled = true;
    try {
      const result = await api.planPrompt(project.id, text);
      await ensureLookups(result.plan.intent);
      showPlan(result.plan);
    } catch (error) {
      resetPlan(error.message);
    } finally {
      continueButton.disabled = false;
    }
  };

  /**
   * The plan as one API call. Returned rather than run, so the same description
   * serves both "run it now" and "put it in the queue".
   */
  function dispatch(plan, steps) {
    // Whatever the controls now say wins over what the wording implied.
    const params = { ...(plan.params || {}), ...(readFields ? readFields() : {}) };
    const named = outputName.value.trim();

    if (plan.intent === "intro" || plan.intent === "outro") {
      return {
        call: (body) => api.buildIntro(project.id, body),
        body: {
          style_id: params.style_id,
          duration_seconds: params.duration_seconds,
          shot_count: params.shot_count,
          title: params.title || "",
          end_card_text: params.end_card_text || "",
          output_name: named || params.output_name,
        },
        message: `${INTENT_LABEL[plan.intent]} ready — see Outputs.`,
      };
    }
    if (plan.intent === "clean") {
      return {
        call: (body) => api.cleanup(project.id, body),
        body: {
          remove_silence: params.remove_silence,
          remove_filler: params.remove_filler,
          remove_off_topic: params.remove_off_topic,
          remove_admin: params.remove_admin,
          remove_qa: params.remove_qa,
          remove_intro: params.remove_intro,
          remove_outro: params.remove_outro,
          output_name: named || params.output_name,
        },
        message: "Clean cut ready — see Outputs.",
      };
    }
    if (plan.intent === "assemble") {
      return {
        call: (body) => api.bookend(project.id, body),
        body: {
          body_filename: params.body_filename || "",
          header_asset: params.header_asset || null,
          footer_asset: params.footer_asset || null,
          intro_transition: params.intro_transition,
          outro_transition: params.outro_transition,
          trim_start: params.trim_start || 0,
          trim_end: params.trim_end,
          output_name: named || params.output_name,
        },
        message: "Final cut ready — see Outputs.",
      };
    }
    if (plan.intent === "export") {
      // The picker only offers finished outputs, so pointing it elsewhere
      // changes where the file is read from too.
      const kind = params.source === plan.params?.source ? params.source_kind : "output";
      return {
        call: (body) => api.exportOutput(project.id, body),
        body: {
          source: params.source,
          source_kind: kind,
          format: params.format,
          quality: params.quality,
          output_name: named,
        },
        message: "Export ready — see Outputs.",
      };
    }
    if (plan.intent === "clip") {
      if (params.mode !== "span") return { goToTab: ["clip", params] };
      return {
        call: (body) => api.renderClips(project.id, body),
        body: {
          selections: [
            { title: params.title, start: params.start, end: params.end, summary: "" },
          ],
          combine: false,
          reframe: false,
          prefix: "clip",
          title: params.title,
          output_name: named || params.output_name,
        },
        message: "Clip ready — see Outputs.",
      };
    }
    return {
      call: (body) => api.renderEdit(project.id, body),
      body: {
        prompt: prompt.value.trim(),
        output_name: named || "edited.mp4",
        preview_only: false,
        steps,
      },
      message: "Edited video ready — see Outputs.",
    };
  }

  function execute(plan, steps) {
    const action = dispatch(plan, steps);
    if (action.goToTab) {
      toast("Opening the clip finder with your search.", "ok");
      onGoToTab?.(...action.goToTab);
      return;
    }
    jobPanel.run(action.call(action.body), {
      title: plan.summary,
      onDone: () => {
        toast(action.message, "ok");
        ctx.refresh();
      },
    });
  }

  function enqueue(plan, steps) {
    const action = dispatch(plan, steps);
    if (action.goToTab) {
      toast("A clip search opens the finder, so it cannot be queued.", "err");
      return Promise.resolve();
    }
    return queue.add(action.call, action.body);
  }


  const chips = h(
    "div.example-chips",
    examples.map((example) =>
      h(
        "button.chip",
        {
          type: "button",
          onclick: () => {
            prompt.value = example;
            resetPlan("Plan it to see what will happen.");
            prompt.focus();
          },
        },
        example
      )
    )
  );

  resetPlan("Describe what you want, then choose Continue.");

  return h(
    "div.stack.editor-tool-pane",
    h(
      "p.muted.small",
      "One instruction can build an intro or outro, clean the recording, cut a clip, " +
        "assemble a final cut, export a format, or apply effects. Continue shows what " +
        "it would do; nothing is rendered until you confirm." +
        (state.assets.length
          ? ` Media you can name: ${state.assets.map((asset) => asset.name).join(", ")}.`
          : "")
    ),
    h("label.field", h("span", "What do you want to do?"), prompt),
    h(
      "details.example-details",
      h("summary", "Examples from this recording"),
      chips
    ),
    h("div.row", continueButton),
    planBox
  );
}
