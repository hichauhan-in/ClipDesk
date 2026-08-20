// Reusable post-analysis recipes compiled by the server into existing actions.

import { api } from "../api.js";
import { confirmAction, h, mount, toast } from "../dom.js";

const STEP_LABEL = {
  notes: "Notes",
  cleanup: "Clean cut",
  clip: "Clip",
  highlight: "Highlight",
  prompt: "Prompt edit",
  intro: "Intro",
  outro: "Outro",
  assemble: "Assemble",
};

const ENRICHMENT = [
  "Transcript only",
  "Light context",
  "Explained",
  "Detailed",
  "Deep dive",
  "Expert",
  "Reference",
];

const TRANSITIONS = ["cut", "fade", "dissolve", "wipe-left", "slide-left"];

function newStep(type) {
  if (type === "notes") {
    return { type, enrichment: 0, include_mermaid: true, include_timestamps: true };
  }
  if (type === "cleanup") {
    return {
      type,
      remove_silence: true,
      remove_filler: true,
      remove_off_topic: true,
      remove_admin: true,
      remove_qa: false,
      remove_intro: false,
      remove_outro: false,
      output_name: "flow-cleaned.mp4",
    };
  }
  if (type === "clip") {
    return {
      type,
      input_from: "source",
      start: 0,
      end: 60,
      title: "Flow clip",
      reframe: false,
      output_name: "flow-clip.mp4",
    };
  }
  if (type === "highlight") {
    return {
      type,
      mode: "best",
      query: "",
      target_seconds: 90,
      reframe: true,
      output_name: "flow-highlight.mp4",
    };
  }
  if (type === "prompt") {
    return {
      type,
      input_from: "source",
      prompt: "",
      output_name: "flow-edited.mp4",
    };
  }
  if (type === "intro" || type === "outro") {
    return {
      type,
      source: "generate",
      input_from: "flow-cleaned.mp4",
      local_path: "",
      style_id: type === "intro" ? "prestige" : "minimal-frame",
      duration_seconds: type === "intro" ? 12 : 8,
      shot_count: 5,
      title: type === "outro" ? "Thank you for watching" : "",
      subtitle: "",
      audio_id: "elevate",
      include_final_message: false,
      final_message: "See you next time",
      output_name: `flow-${type}.mp4`,
    };
  }
  return {
    type: "assemble",
    input_from: "flow-cleaned.mp4",
    intro_transition: "fade",
    outro_transition: "fade",
    output_name: "flow-final.mp4",
  };
}

function newFlow() {
  return {
    id: `flow-${Date.now().toString(36)}`,
    name: "New publishing flow",
    description: "",
    steps: [
      newStep("cleanup"),
      newStep("intro"),
      newStep("outro"),
      newStep("assemble"),
    ],
  };
}

const clone = (value) => JSON.parse(JSON.stringify(value));

export function createFlowsPane({ project, analysis, jobPanel, ctx }) {
  const root = h("div.stack.flows-page", h("div.loading", "Loading Flows…"));
  let flows = [];
  let current = null;
  let installedStyles = [];
  let audioItems = [];
  let expandedIndex = 0;

  const input = (value, change, props = {}) =>
    h("input", {
      type: props.type || "text",
      value: value ?? "",
      ...props,
      oninput: (event) => change(event.target.value),
    });

  const select = (value, options, change) =>
    h(
      "select",
      { onchange: (event) => change(event.target.value) },
      options.map(([id, label]) => h("option", { value: id, selected: id === value }, label))
    );

  const field = (label, control, className = "") =>
    h("label.field", { class: className }, h("span", label), control);

  const check = (label, value, change) => {
    const box = h("input", {
      type: "checkbox",
      checked: value,
      onchange: (event) => change(event.target.checked),
    });
    return h("label.check", box, label);
  };

  function sourceOptions(index) {
    const options = [["source", `Original recording — ${project.source_filename}`]];
    for (const previous of current.steps.slice(0, index)) {
      if (!["cleanup", "clip", "highlight", "prompt", "intro", "outro", "assemble"].includes(previous.type)) continue;
      if ((previous.type === "intro" || previous.type === "outro") && previous.source === "local") continue;
      if (!previous.output_name) continue;
      options.push([
        previous.output_name,
        `${previous.output_name} — ${STEP_LABEL[previous.type]}`,
      ]);
    }
    return options;
  }

  function sourceField(step, index) {
    const options = sourceOptions(index);
    if (!options.some(([value]) => value === step.input_from)) step.input_from = options.at(-1)[0];
    return field(
      "Source video",
      select(step.input_from, options, (value) => (step.input_from = value))
    );
  }

  function outputField(step, fallback, index) {
    if (!step.output_name) step.output_name = fallback;
    return field(
      "Save as",
      input(step.output_name, (value) => {
        const previous = step.output_name;
        step.output_name = value;
        for (const later of current.steps.slice(index + 1)) {
          if (later.input_from === previous) later.input_from = value;
        }
      })
    );
  }

  function stepEditor(step, index) {
    if (step.type === "notes") {
      return h(
        "div.flow-fields",
        field(
          "Verbosity",
          select(
            String(step.enrichment),
            ENRICHMENT.map((label, index) => [String(index), `${index} — ${label}`]),
            (value) => (step.enrichment = Number(value))
          )
        ),
        h(
          "div.flow-checks",
          check("Include diagrams", step.include_mermaid, (value) => (step.include_mermaid = value)),
          check("Include timestamps", step.include_timestamps, (value) => (step.include_timestamps = value))
        )
      );
    }

    if (step.type === "cleanup") {
      const toggles = [
        ["remove_silence", "Long silences"],
        ["remove_filler", "Filler"],
        ["remove_off_topic", "Off-topic sections"],
        ["remove_admin", "Logistics"],
        ["remove_qa", "Q&A"],
        ["remove_intro", "Detected intro"],
        ["remove_outro", "Detected outro"],
      ];
      return h(
        "div.flow-fields",
        h(
          "div.flow-checks",
          toggles.map(([key, label]) =>
            check(label, step[key], (value) => (step[key] = value))
          )
        ),
        outputField(step, "flow-cleaned.mp4", index)
      );
    }

    if (step.type === "clip") {
      return h(
        "div.flow-fields",
        sourceField(step, index),
        h(
          "div.field-grid",
          field("Start (seconds)", input(step.start, (value) => (step.start = Number(value)), { type: "number", min: 0, step: 1 })),
          field("End (seconds)", input(step.end, (value) => (step.end = Number(value)), { type: "number", min: 1, step: 1 }))
        ),
        field("Clip title", input(step.title, (value) => (step.title = value))),
        h("div.flow-checks", check("Fit to 16:9 canvas", step.reframe, (value) => (step.reframe = value))),
        outputField(step, "flow-clip.mp4", index)
      );
    }

    if (step.type === "highlight") {
      return h(
        "div.flow-fields",
        field(
          "Selection",
          select(
            step.mode,
            [["best", "Best standalone highlight"], ["topic", "Best highlight about a topic"]],
            (value) => {
              step.mode = value;
              drawEditor();
            }
          )
        ),
        step.mode === "topic"
          ? field(
              "Topic",
              input(step.query, (value) => (step.query = value), {
                placeholder: "e.g. deployment lessons or customer impact",
              })
            )
          : null,
        field(
          "Target length (seconds)",
          input(step.target_seconds, (value) => (step.target_seconds = Number(value)), {
            type: "number", min: 10, max: 7200, step: 5,
          })
        ),
        h("div.flow-checks", check("Fit to 16:9 canvas", step.reframe, (value) => (step.reframe = value))),
        outputField(step, "flow-highlight.mp4", index),
        h("div.faint.small", "Uses analysis to choose one self-contained moment automatically.")
      );
    }

    if (step.type === "prompt") {
      return h(
        "div.flow-fields",
        sourceField(step, index),
        field(
          "Edit instruction",
          h("textarea", {
            rows: 3,
            value: step.prompt,
            placeholder: "e.g. trim the first 10 seconds and add text \"Confidential\" bottom right",
            oninput: (event) => (step.prompt = event.target.value),
          })
        ),
        outputField(step, "flow-edited.mp4", index),
        h("div.faint.small", "The same constrained prompt-edit parser validates this before rendering.")
      );
    }

    if (step.type === "intro" || step.type === "outro") {
      const generated = step.source === "generate";
      return h(
        "div.flow-fields",
        field(
          "Source",
          select(step.source, [["generate", "Generate for each video"], ["local", "Fixed local video file"]], (value) => {
            step.source = value;
            drawEditor();
          })
        ),
        generated
          ? [
              sourceField(step, index),
              h(
                "div.field-grid",
                field(
                  "Template",
                  select(
                    step.style_id,
                    installedStyles.map((style) => [style.id, style.name]),
                    (value) => (step.style_id = value)
                  )
                ),
                field(
                  "Length (seconds)",
                  input(step.duration_seconds, (value) => (step.duration_seconds = Number(value)), {
                    type: "number",
                    min: 5,
                    max: 60,
                    step: 1,
                  })
                )
              ),
              field(
                step.type === "outro" ? "Thank-you line" : "Title override",
                input(step.title, (value) => (step.title = value), {
                  placeholder: step.type === "intro" ? "Blank uses the analysed title" : "",
                })
              ),
              field(
                "Supporting line",
                input(step.subtitle, (value) => (step.subtitle = value), {
                  placeholder: "Blank uses analysis where appropriate",
                })
              ),
              field(
                "Soundtrack",
                select(step.audio_id, [["none", "None"], ...audioItems], (value) => (step.audio_id = value))
              ),
              step.type === "outro"
                ? h(
                    "div.flow-optional",
                    check("Add a final message", step.include_final_message, (value) => {
                      step.include_final_message = value;
                      drawEditor();
                    }),
                    field(
                      "Final message",
                      input(step.final_message, (value) => (step.final_message = value), {
                        disabled: !step.include_final_message,
                      }),
                      step.include_final_message ? "" : "disabled-field"
                    )
                  )
                : null,
              outputField(step, `flow-${step.type}.mp4`, index),
            ]
          : field(
              "Absolute file path",
              input(step.local_path, (value) => (step.local_path = value), {
                placeholder: "C:\\Brand\\intro.mp4",
              })
            ),
        h(
          "div.faint.small",
          generated
            ? "Templates must be installed first under Intro or Outro → Get more."
            : "The same file is copied into each project when this Flow runs."
        )
      );
    }

    return h(
      "div.flow-fields",
      sourceField(step, index),
      h(
        "div.field-grid",
        field(
          "Intro transition",
          select(step.intro_transition, TRANSITIONS.map((item) => [item, item.replace("-", " ")]), (value) => (step.intro_transition = value))
        ),
        field(
          "Outro transition",
          select(step.outro_transition, TRANSITIONS.map((item) => [item, item.replace("-", " ")]), (value) => (step.outro_transition = value))
        )
      ),
      outputField(step, "flow-final.mp4", index),
      h(
        "div.faint.small",
        "Uses the selected source video plus the latest Intro and Outro above it."
      )
    );
  }

  function drawEditor() {
    if (!current) return;
    const editor = root.querySelector(".flow-editor");
    if (!editor) return;

    const name = input(current.name, (value) => (current.name = value), { maxlength: 100 });
    const description = h("textarea", {
      rows: 2,
      maxlength: 300,
      value: current.description || "",
      oninput: (event) => (current.description = event.target.value),
    });
    const addType = h(
      "select",
      h("optgroup", { label: "Documents" }, h("option", { value: "notes" }, "Notes")),
      h(
        "optgroup",
        { label: "Cuts & highlights" },
        h("option", { value: "cleanup" }, "Clean cut"),
        h("option", { value: "highlight" }, "Best or topic highlight"),
        h("option", { value: "clip" }, "Fixed-time clip")
      ),
      h("optgroup", { label: "Editing" }, h("option", { value: "prompt" }, "Prompt edit")),
      h(
        "optgroup",
        { label: "Intro & outro" },
        h("option", { value: "intro" }, "Intro"),
        h("option", { value: "outro" }, "Outro")
      ),
      h("optgroup", { label: "Finish" }, h("option", { value: "assemble" }, "Assemble"))
    );

    const stepSummary = (step) => {
      const source = step.input_from
        ? (step.input_from === "source" ? "Original" : step.input_from)
        : "Analysis";
      const output = step.output_name || (step.type === "notes" ? "notes.md" : "Configured output");
      if (step.type === "intro" || step.type === "outro") {
        return step.source === "local" ? "Fixed local file" : `${step.style_id} → ${output}`;
      }
      return `${source} → ${output}`;
    };

    const stepRows = current.steps.map((step, index) => {
      const expanded = index === expandedIndex;
      return h(
        "section.flow-step",
        { class: expanded ? "expanded" : "collapsed" },
        h(
          "div.row-between.flow-step-head",
          h(
            "button.flow-step-toggle",
            {
              type: "button",
              "aria-expanded": String(expanded),
              onclick: () => {
                expandedIndex = index;
                drawEditor();
              },
            },
            h(
              "span.row.flow-step-title",
              h("span.flow-index", String(index + 1)),
              h(
                "span.flow-step-copy",
                h("strong", STEP_LABEL[step.type] || step.type),
                h("span.faint.small.flow-step-summary", stepSummary(step))
              )
            )
          ),
          h(
            "div.row.flow-step-actions",
            h("span.faint.flow-chevron", expanded ? "▾" : "▸"),
            h("button.icon-btn", {
              type: "button",
              title: "Move step up",
              disabled: index === 0,
              onclick: (event) => { event.stopPropagation(); moveStep(index, -1); },
            }, "▲"),
            h("button.icon-btn", {
              type: "button",
              title: "Move step down",
              disabled: index === current.steps.length - 1,
              onclick: (event) => { event.stopPropagation(); moveStep(index, 1); },
            }, "▼"),
            h("button.icon-btn", {
              type: "button",
              title: "Remove step",
              onclick: (event) => {
                event.stopPropagation();
                current.steps.splice(index, 1);
                expandedIndex = Math.min(index, current.steps.length - 1);
                drawEditor();
              },
            }, "✕")
          )
        ),
        expanded ? h("div.flow-step-body", stepEditor(step, index)) : null
      );
    });

    const save = h("button.btn", { onclick: () => saveCurrent() }, "Save Flow");
    const run = h(
      "button.btn.btn-primary",
      {
        disabled: !analysis,
        title: analysis ? "" : "Analyse this recording before running a Flow.",
        onclick: () => runCurrent(),
      },
      "Save and run"
    );

    mount(
      editor,
      h(
        "div.row-between.flow-editor-head",
        h("div", h("h2", current.name || "Untitled Flow"), h("div.faint.small", `${current.steps.length} step(s)`)),
        h("div.row", save, run)
      ),
      h("div.flow-identity", field("Flow name", name), field("Description", description)),
      h("div.stack.flow-steps", stepRows),
      h(
        "div.row.flow-add",
        addType,
        h("button.btn.btn-sm", {
          onclick: () => {
            current.steps.push(newStep(addType.value));
            expandedIndex = current.steps.length - 1;
            drawEditor();
          },
        }, "Add step")
      ),
      analysis ? null : h("div.notice.warn", "This recording must finish analysis before the Flow can run.")
    );
  }

  function moveStep(index, offset) {
    const target = index + offset;
    if (target < 0 || target >= current.steps.length) return;
    [current.steps[index], current.steps[target]] = [current.steps[target], current.steps[index]];
    expandedIndex = target;
    drawEditor();
  }

  async function saveCurrent() {
    current.name = current.name.trim();
    if (current.name.length < 2) {
      toast("Give this Flow a name.", "err");
      return null;
    }
    if (!current.steps.length) {
      toast("Add at least one step.", "err");
      return null;
    }
    const saved = await api.saveFlow(current.id, current);
    const index = flows.findIndex((item) => item.id === saved.id);
    if (index >= 0) flows[index] = saved;
    else flows.push(saved);
    current = clone(saved);
    expandedIndex = Math.min(expandedIndex, current.steps.length - 1);
    drawShell();
    toast("Flow saved.", "ok");
    return saved;
  }

  async function runCurrent() {
    try {
      const saved = await saveCurrent();
      if (!saved) return;
      const result = await api.runFlow(project.id, saved.id);
      const last = result.job_ids.at(-1);
      toast(`Running ${result.job_ids.length} Flow step(s).`, "ok");
      if (last) {
        jobPanel.follow(last, {
          title: saved.name,
          onDone: () => {
            toast(`${saved.name} finished.`, "ok");
            ctx.refresh();
          },
          onError: () => ctx.refresh(),
        });
      }
    } catch (error) {
      toast(error.message, "err");
    }
  }

  async function removeCurrent() {
    if (!current) return;
    const ok = await confirmAction({
      title: `Delete ${current.name}?`,
      message: "The Flow is removed, but outputs it already created are kept.",
      confirmLabel: "Delete Flow",
    });
    if (!ok) return;
    await api.deleteFlow(current.id);
    flows = flows.filter((item) => item.id !== current.id);
    current = flows[0] ? clone(flows[0]) : newFlow();
    expandedIndex = 0;
    drawShell();
  }

  function drawShell() {
    const list = h("div.flow-list");
    mount(
      list,
      h(
        "div.row-between.flow-list-head",
        h("strong", "Saved Flows"),
        h("button.btn.btn-sm", { onclick: () => { current = newFlow(); expandedIndex = 0; drawShell(); } }, "New")
      ),
      flows.length
        ? flows.map((flow) =>
            h(
              "button.flow-list-item",
              {
                type: "button",
                "aria-current": String(flow.id === current?.id),
                onclick: () => {
                  current = clone(flow);
                  expandedIndex = 0;
                  drawShell();
                },
              },
              h("strong.small", flow.name),
              h("span.faint.small", `${flow.steps.length} step(s)`)
            )
          )
        : h("div.empty.empty-sm", "No saved Flows yet."),
      flows.some((item) => item.id === current?.id)
        ? h("button.btn.btn-ghost.btn-sm.btn-danger", { onclick: removeCurrent }, "Delete selected")
        : null
    );

    mount(
      root,
      h(
        "div.flow-intro",
        h("h2", "Flows"),
        h(
          "p.muted.small",
          "Turn a repeatable publishing routine into one run. Steps use the same Notes, Clean cut, Intro, Outro, Clip, and Assemble tools already available in ClipDesk."
        )
      ),
      h("div.flows-shell", list, h("div.flow-editor"))
    );
    drawEditor();
  }

  (async () => {
    try {
      const [saved, styles, audio] = await Promise.all([
        api.listFlows(),
        api.introStyles(),
        api.introAudio(),
      ]);
      flows = saved.flows || [];
      installedStyles = styles.installed || [];
      audioItems = [
        ...(audio.imported || []).map((item) => [item.id, `${item.name} — saved`]),
        ...(audio.built_in || []).map((item) => [item.id, item.name]),
      ];
      current = flows[0] ? clone(flows[0]) : newFlow();
      drawShell();
    } catch (error) {
      mount(root, h("div.empty", error.message));
    }
  })();

  return root;
}
