// One recording: what is in it, and everything you can make from it.

import { api } from "../api.js";
import { candidatePicker } from "../candidates.js";
import { confirmAction, debounce, h, mount, numberField, openDialog, toast } from "../dom.js";
import { bytes, duration, timecode } from "../format.js";
import { createJobPanel } from "../jobpanel.js";
import { createJobsChip } from "../jobschip.js";
import { latestForTab, refreshJobs } from "../jobs.js";
import { markdownToHtml } from "../markdown.js";
import { createQueue, queuePanel, runOrQueue, withQueued } from "../queue.js";
import { createAssetImporter } from "./assetimporter.js";
import { clippingPane as createClippingPane, promptPane as createPromptPane } from "./editorpanes.js";
import { openExportDialog } from "./exportdialog.js";
import { createIntroPane } from "./intropane.js";

const TABS = [
  ["overview", "Overview"],
  ["transcript", "Transcript & Notes"],
  ["clip", "Cuts and Highlights"],
  ["editor", "Editor"],
  ["outputs", "Outputs"],
];

//: Tabs that used to exist on their own and now live inside another one.
const TAB_ALIAS = {
  highlights: "clip",
  cleanup: "clip",
  notes: "transcript",
};

const resolveTab = (key) => TAB_ALIAS[key] || key;

const KIND_LABEL = {
  on_topic: "content",
  qa: "Q&A",
  off_topic: "off topic",
  admin: "logistics",
  filler: "filler",
  intro: "intro",
  outro: "outro",
  transition: "transition",
};

const ENRICHMENT = [
  ["Transcript only", "Nothing that was not said in the recording."],
  ["Clarify terms", "Expand acronyms and define jargon the speaker used but did not explain."],
  ["Add background", "Short background so a colleague new to the area can follow along."],
  ["Connect concepts", "Explain architecture, data flow, relationships and the main trade-offs."],
  [
    "Practical guide",
    "Add prerequisites, implementation steps, examples, verification and troubleshooting checks.",
  ],
  [
    "Technical deep dive",
    "Cover internals, alternatives, security, reliability, performance and failure modes.",
  ],
  [
    "Expert reference",
    "Build a comprehensive technical study guide for designing, implementing and operating the topic.",
  ],
];

export async function renderProject(root, ctx, projectId, params) {
  let project;
  try {
    project = await api.getProject(projectId);
  } catch (error) {
    mount(root, h("div.empty", error.message));
    return;
  }

  let analysis = null;
  if (project.status === "ready") {
    analysis = await api.getAnalysis(projectId).catch(() => null);
  }

  const jobPanel = createJobPanel();
  const queue = createQueue(projectId);
  const body = h("div");
  let activeTab = resolveTab(params.get("tab") || "overview");
  let pendingPreset = null;

  const tabBar = h(
    "div.tabs",
    TABS.map(([key, label]) =>
      h(
        "button",
        {
          "aria-selected": String(key === activeTab),
          disabled:
            !analysis &&
            key !== "overview" &&
            key !== "outputs" &&
            !(key === "editor" && project.source_exists),
          onclick: () => goToTab(key),
        },
        label
      )
    )
  );

  // The chip lives in the tab row so what is running is visible from every tab,
  // not only the one that started it. Clicking a job goes to where it belongs.
  const chip = createJobsChip({
    projectId: project.id,
    onOpenJob: (job) => {
      if (job.tab) goToTab(job.tab);
    },
  });
  const tabRow = h("div.tabrow", tabBar, chip.el);

  function goToTab(key, preset = null) {
    activeTab = resolveTab(key);
    pendingPreset = preset;
    for (const [index, [tabKey]] of TABS.entries()) {
      tabBar.children[index]?.setAttribute("aria-selected", String(tabKey === activeTab));
    }
    drawTab();
  }

  function drawTab() {
    // Keep the tab in the URL so finishing a job — which re-reads the project —
    // leaves the user where they were instead of bouncing them to Overview.
    // replaceState avoids firing hashchange, which would re-route and flicker.
    if (ctx.isCurrent?.() === false) return;
    history.replaceState(null, "", `#/project/${projectId}?tab=${activeTab}`);

    // A preset is handed over once, by whichever tab navigated here.
    const preset = pendingPreset;
    pendingPreset = null;

    const view = {
      overview: () => overviewTab(project, analysis, jobPanel, ctx),
      transcript: () => transcriptTab(project, analysis, jobPanel, ctx, queue),
      clip: () => clipTab(project, analysis, jobPanel, ctx, preset, queue),
      editor: () => editorTab(project, analysis, jobPanel, ctx, goToTab, queue),
      outputs: () => outputsTab(project, ctx, jobPanel),
    }[activeTab];
    mount(body, view ? view() : h("div.empty", "Nothing here."));
  }

  mount(
    root,
    header(project, analysis, ctx),
    jobPanel.el,
    queuePanel(queue, { onRan: () => refreshJobs(project.id) }),
    h("div", { style: { height: "16px" } }),
    tabRow,
    body
  );
  drawTab();
  queue.refresh();
  restoreActiveProjectJob();

  if (params.get("autostart") === "1" && project.status !== "ready") {
    startAnalysis(project, jobPanel, ctx);
  }

  async function restoreActiveProjectJob() {
    try {
      const payload = await api.listJobs(project.id);
      if (ctx.isCurrent?.() === false || jobPanel.busy) return;
      const active = (payload.jobs || []).find(
        (job) => job.status === "queued" || job.status === "running"
      );
      if (!active) return;
      jobPanel.follow(active.id, {
        title: active.label || active.message || "Working",
        onDone: () => ctx.refresh(),
        onError: () => ctx.refresh(),
      });
    } catch {
      // The global jobs store and health indicator already handle an unavailable server.
    }
  }
}

function header(project, analysis, ctx) {
  return h(
    "div.page-head",
    h(
      "div.row-between",
      h(
        "div",
        h("h1", project.title || project.source_filename),
        h(
          "div.sub.row",
          h("span.mono.small", project.source_filename),
          h("span.faint", "·"),
          h("span", duration(project.duration_s)),
          h("span.faint", "·"),
          h("span", bytes(project.size_bytes)),
          analysis?.transcript?.source
            ? h(
                "span.tag",
                analysis.transcript.source === "uploaded"
                  ? "transcript supplied"
                  : `transcribed (${analysis.transcript.model || "whisper"})`
              )
            : null
        )
      ),
      h(
        "div.row",
        h("button.btn.btn-sm", { onclick: () => ctx.navigate("#/") }, "← Library"),
        h(
          "button.btn.btn-sm.btn-ghost.btn-danger",
          {
            onclick: async () => {
              if (!(await confirmAction("Delete this recording and all its outputs?"))) return;
              await api.deleteProject(project.id);
              ctx.navigate("#/");
            },
          },
          "Delete"
        )
      )
    )
  );
}

function startAnalysis(project, jobPanel, ctx, options = {}) {
  jobPanel.run(api.analyze(project.id, { skip_llm: false, ...options }), {
    title: "Analysing",
    onDone: () => {
      toast("Analysis finished.", "ok");
      ctx.refresh();
    },
  });
}

function jumpTo(seconds) {
  const video = document.querySelector("video");
  if (!video) return;
  video.currentTime = seconds;
  video.scrollIntoView({ behavior: "smooth", block: "nearest" });
  video.play().catch(() => {});
}

// --- overview ----------------------------------------------------------------
function overviewTab(project, analysis, jobPanel, ctx) {
  if (!analysis) {
    return h(
      "div.card",
      h("h2", "Not analysed yet"),
      h(
        "p.muted",
        project.status === "failed"
          ? `The last attempt failed: ${project.error}`
          : "Extract the transcript and work out what is in this recording. " +
            "Everything else is built from the result."
      ),
      h(
        "div.row",
        h(
          "button.btn.btn-primary",
          { onclick: () => startAnalysis(project, jobPanel, ctx) },
          "Analyse"
        ),
        h(
          "button.btn",
          {
            onclick: () => startAnalysis(project, jobPanel, ctx, { skip_llm: true }),
            title: "Transcript and silence only — no language model",
          },
          "Transcript only"
        )
      )
    );
  }

  return h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(0, 2fr) minmax(280px, 1fr)" } },
    h(
      "div",
      analysis.warnings?.length
        ? h(
            "div.card",
            { style: { borderColor: "#4d3f1c" } },
            h("h3", "Worth knowing"),
            h(
              "ul",
              { style: { margin: 0, paddingLeft: "18px" } },
              analysis.warnings.map((warning) => h("li.small", warning))
            )
          )
        : null,
      h(
        "div.card",
        h("h2", "Summary"),
        analysis.abstract ? h("p", { style: { marginTop: 0 } }, analysis.abstract) : null,
        h("p.muted", analysis.summary || "No summary was produced."),
        analysis.keywords?.length
          ? h("div.row", analysis.keywords.map((word) => h("span.tag", word)))
          : null
      ),
      timelineCard(analysis),
      h(
        "div.card",
        h("h2", `Chapters (${analysis.chapters.length})`),
        analysis.chapters.length
          ? analysis.chapters.map((chapter) =>
              h(
                "div.chapter",
                h(
                  "div.row",
                  h("h4", chapter.title),
                  h(
                    "button.btn.btn-sm.btn-ghost.mono",
                    { onclick: () => jumpTo(chapter.start) },
                    `${timecode(chapter.start)} – ${timecode(chapter.end)}`
                  )
                ),
                chapter.summary ? h("div.muted.small", chapter.summary) : null,
                chapter.key_points?.length
                  ? h(
                      "ul.small.muted",
                      { style: { margin: "6px 0 0", paddingLeft: "18px" } },
                      chapter.key_points.map((point) => h("li", point))
                    )
                  : null
              )
            )
          : h("div.faint", "No chapters were identified.")
      )
    ),
    h(
      "div.stack",
      h(
        "div.card",
        h("h3", "Preview"),
        h("video", {
          controls: true,
          preload: "metadata",
          src: `/api/projects/${project.id}/preview`,
        })
      ),
      analysis.decisions?.length
        ? h(
            "div.card",
            h("h3", "Decisions"),
            h(
              "ul.small",
              { style: { margin: 0, paddingLeft: "18px" } },
              analysis.decisions.map((decision) =>
                h(
                  "li",
                  decision.text,
                  decision.time !== null
                    ? h("span.faint.mono", ` ${timecode(decision.time)}`)
                    : null
                )
              )
            )
          )
        : null,
      analysis.action_items?.length
        ? h(
            "div.card",
            h("h3", "Action items"),
            h(
              "ul.small",
              { style: { margin: 0, paddingLeft: "18px" } },
              analysis.action_items.map((item) =>
                h(
                  "li",
                  item.text,
                  item.owner ? h("strong", ` — ${item.owner}`) : null,
                  item.time !== null ? h("span.faint.mono", ` ${timecode(item.time)}`) : null
                )
              )
            )
          )
        : null,
      h(
        "div.card",
        h("h3", "Re-run"),
        h("p.muted.small", "Re-analyse if you changed the model or the transcript."),
        h(
          "button.btn.btn-sm",
          { onclick: () => startAnalysis(project, jobPanel, ctx) },
          "Analyse again"
        )
      )
    )
  );
}

function timelineCard(analysis) {
  const total = analysis.media.duration_s || 1;
  const keep = analysis.cleanup_keep_spans || [];
  const kept = keep.reduce((sum, span) => sum + (span.end - span.start), 0);
  const percent = (value) => `${(value / total) * 100}%`;
  const width = (span) => `${Math.max(0.15, ((span.end - span.start) / total) * 100)}%`;

  const marker = h("i.playhead", { style: { left: "0%" } });
  const bars = [
    ...(analysis.silences || []).map((span) =>
      h("i.silence", {
        title: `Silence ${timecode(span.start)} – ${timecode(span.end)}`,
        style: { left: percent(span.start), width: width(span) },
      })
    ),
    ...keep.map((span) =>
      h("i", {
        title: `Kept ${timecode(span.start)} – ${timecode(span.end)}`,
        style: { left: percent(span.start), width: width(span) },
      })
    ),
    marker,
  ];

  // Clicking the bar seeks, so the whole recording is reachable from one strip.
  const strip = h("div.timeline.timeline-seek", {
    title: "Click anywhere to jump there",
    onclick: (event) => {
      const box = strip.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
      jumpTo(ratio * total);
    },
  });
  mount(strip, bars);

  const textById = new Map(
    (analysis.transcript?.segments || []).map((segment) => [segment.id, segment.text])
  );
  const silences = (analysis.silences || []).map((span) => ({
    start: span.start,
    end: span.end,
    kind: "silence",
    reason: "",
    text: "",
  }));
  const dropped = (analysis.segment_analyses || [])
    .filter((item) => !item.keep && item.end > item.start)
    .map((item) => ({
      start: item.start,
      end: item.end,
      kind: item.kind,
      reason: item.reason || "",
      text: textById.get(item.segment_id) || "",
    }));
  const removals = [...dropped, ...silences].sort((a, b) => a.start - b.start);

  let activeRow = null;
  const rows = removals.map((item) => {
    const row = h(
      "button.cut-row",
      {
        type: "button",
        onclick: () => {
          jumpTo(item.start);
          activeRow?.classList.remove("active");
          row.classList.add("active");
          activeRow = row;
          marker.style.left = percent(item.start);
        },
      },
      h(
        "div.cut-row-head",
        h("span.mono.small", timecode(item.start)),
        h("span.tag", KIND_LABEL[item.kind] || item.kind),
        h("span.faint.small", duration(item.end - item.start))
      ),
      item.text
        ? h("div.cut-row-text", `“${item.text}”`)
        : item.reason
          ? h("div.faint.small", item.reason)
          : null
    );
    return row;
  });

  return h(
    "div.card",
    h(
      "div.row-between",
      h("h2", { style: { margin: 0 } }, "Where the value is"),
      h(
        "span.small.muted",
        `${duration(kept)} worth keeping of ${duration(total)} · ` +
          `${Math.round((1 - kept / total) * 100)}% could be cut`
      )
    ),
    h("div", { style: { marginTop: "12px" } }, strip),
    h(
      "div.row.faint.small",
      { style: { marginTop: "6px" } },
      h("span", "▮ kept"),
      h("span", { style: { color: "#7a4d88" } }, "▮ silence"),
      h("div.spacer"),
      h("span.mono", timecode(total, { hours: true }))
    ),
    removals.length
      ? h(
          "div",
          { style: { marginTop: "12px" } },
          h(
            "div.row-between",
            h("h3", { style: { margin: 0 } }, `What would be cut (${removals.length})`),
            h("span.faint.small", "Click any line to jump there")
          ),
          h("div.cut-list", { style: { marginTop: "8px" } }, rows)
        )
      : null
  );
}

// --- transcript & notes ------------------------------------------------------
/**
 * One place for what was said and what is written from it. The two views keep
 * everything they had; sharing a tab just stops the user hunting for which of
 * the two holds the download they want.
 */
function transcriptTab(project, analysis, jobPanel, ctx, queue) {
  const state = { view: "transcript" };
  const body = h("div.subtab-body");
  const views = [
    ["transcript", `Transcript (${analysis.transcript.segments.length})`, () => transcriptView(project, analysis)],
    ["notes", "Notes", () => notesView(project, analysis, jobPanel, ctx, queue)],
    ["article", "Article", () => articleView(project, analysis, jobPanel, ctx, queue)],
  ];
  const bar = h(
    "div.segmented",
    views.map(([key, label]) =>
      h(
        "button",
        {
          "data-view": key,
          "aria-selected": String(key === state.view),
          onclick: () => {
            state.view = key;
            for (const button of bar.children) {
              button.setAttribute("aria-selected", String(button.dataset.view === key));
            }
            draw();
          },
        },
        label
      )
    )
  );

  function draw() {
    const selected = views.find(([key]) => key === state.view) || views[0];
    mount(body, selected[2]());
  }

  draw();
  return h("div.stack", h("div.subtab-bar", bar), body);
}

function transcriptView(project, analysis) {
  const keepById = new Map(
    (analysis.segment_analyses || []).map((item) => [item.segment_id, item])
  );
  const search = h("input", { type: "text", placeholder: "Search the transcript…" });
  const list = h("div.seg-list");

  function draw(filter = "") {
    const needle = filter.trim().toLowerCase();
    const rows = analysis.transcript.segments
      .filter((segment) => !needle || segment.text.toLowerCase().includes(needle))
      .slice(0, 4000)
      .map((segment) => {
        const verdict = keepById.get(segment.id);
        const dropped = verdict && !verdict.keep;
        return h(
          "div",
          {
            class: `seg${dropped ? " dropped" : ""}`,
            title: verdict
              ? `${KIND_LABEL[verdict.kind] || verdict.kind} · importance ${verdict.importance.toFixed(
                  2
                )}${verdict.reason ? ` · ${verdict.reason}` : ""}`
              : "",
            onclick: () => jumpTo(segment.start),
          },
          h("time", timecode(segment.start)),
          h("div", segment.speaker ? h("strong", `${segment.speaker}: `) : null, segment.text)
        );
      });
    mount(list, rows.length ? rows : h("div.faint", { style: { padding: "20px" } }, "No matches."));
  }

  search.addEventListener("input", debounce((event) => draw(event.target.value), 180));
  draw();

  return h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(0, 2fr) minmax(260px, 1fr)" } },
    h(
      "div.card",
      h(
        "div.row-between",
        h("h2", { style: { margin: 0 } }, `Transcript (${analysis.transcript.segments.length})`),
        h("div", { style: { width: "240px" } }, search)
      ),
      h("div", { style: { marginTop: "12px" } }, list),
      h(
        "div.faint.small",
        { style: { marginTop: "10px" } },
        "Struck-through lines are what the clean cut would remove. Hover for the reason; click to jump."
      )
    ),
    h(
      "div.stack",
      h(
        "div.card",
        h("h3", "Download"),
        h(
          "div.stack",
          ["srt", "vtt", "md", "txt"].map((format) =>
            h(
              "button.btn.btn-sm",
              {
                onclick: async () => {
                  try {
                    const result = await api.exportTranscript(project.id, format);
                    window.open(result.url, "_blank");
                  } catch (error) {
                    toast(error.message, "err");
                  }
                },
              },
              `Transcript (.${format})`
            )
          ),
          h(
            "button.btn.btn-sm",
            {
              onclick: async () => {
                const result = await api.exportSummary(project.id);
                window.open(result.url, "_blank");
              },
            },
            "Summary (.md)"
          )
        )
      ),
      h(
        "div.card",
        h("h3", "Preview"),
        h("video", {
          controls: true,
          preload: "metadata",
          src: `/api/projects/${project.id}/preview`,
        })
      )
    )
  );
}

/**
 * "How much the AI may add", shared by notes and articles. Both documents get
 * forwarded, so both need the same guarantee about what the model contributed.
 */
function enrichmentSlider(warning) {
  let level = 0;
  const levelTitle = h("strong", ENRICHMENT[0][0]);
  const levelPosition = h("span.tag", `Level 1 of ${ENRICHMENT.length}`);
  const levelHelp = h("div.muted.small", ENRICHMENT[0][1]);
  const levelWarning = h("div.faint.small", { style: { display: "none" } }, warning);
  const slider = h("input", {
    class: "enrichment-slider",
    type: "range",
    min: "0",
    max: String(ENRICHMENT.length - 1),
    step: "1",
    value: "0",
    "aria-label": "AI enrichment level",
    oninput: (event) => {
      level = Number(event.target.value);
      levelTitle.textContent = ENRICHMENT[level][0];
      levelPosition.textContent = `Level ${level + 1} of ${ENRICHMENT.length}`;
      levelHelp.textContent = ENRICHMENT[level][1];
      levelWarning.style.display = level > 0 ? "" : "none";
      slider.setAttribute("aria-valuetext", ENRICHMENT[level][0]);
      slider.style.setProperty(
        "--enrichment-progress",
        `${(level / (ENRICHMENT.length - 1)) * 100}%`
      );
    },
  });
  slider.setAttribute("aria-valuetext", ENRICHMENT[0][0]);
  slider.style.setProperty("--enrichment-progress", "0%");

  const el = h(
    "div",
    { style: { marginTop: "6px" } },
    h(
      "div.row-between",
      h("span.small.muted", "How much the AI may add"),
      h("div.row", { style: { gap: "8px" } }, levelPosition, levelTitle)
    ),
    slider,
    h(
      "div.row.faint.small",
      { style: { justifyContent: "space-between", marginTop: "-2px" } },
      h("span", "Transcript"),
      h("span", "Expert reference")
    ),
    h("div", { style: { marginTop: "6px" } }, levelHelp),
    h("div", { style: { marginTop: "6px" } }, levelWarning)
  );
  el.value = () => level;
  return el;
}

// --- notes -------------------------------------------------------------------
function notesView(project, analysis, jobPanel, ctx, queue) {
  const mermaid = h("input", { type: "checkbox", checked: true });
  const timestamps = h("input", { type: "checkbox", checked: true });
  const perFile = numberField({ value: 6, min: 1, max: 50 });
  const preview = h("div.md");

  const enrichment = enrichmentSlider(
    "Anything the model adds beyond the recording is marked “Added context” in the " +
      "document, so a reader can always tell it from what was actually said."
  );

  const noteFiles = (project.artifacts || []).filter((artifact) => artifact.kind === "notes");

  const notesBody = () => ({
    include_mermaid: mermaid.checked,
    include_timestamps: timestamps.checked,
    chapters_per_file: perFile.value,
    enrichment: enrichment.value(),
  });

  async function showFile(artifact) {
    try {
      mount(preview, h("div", { html: markdownToHtml(await documentMarkdown(project, artifact)) }));
      preview.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      toast(error.message || "Could not load that file.", "err");
    }
  }

  return h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(300px, 1fr) minmax(0, 2fr)" } },
    h(
      "div.stack",
      h(
        "div.card",
        h("h2", "Generate notes"),
        h(
          "p.muted.small",
          "One Markdown document per group of chapters, written for someone who will " +
            "not watch the recording."
        ),
        enrichment,
        h("hr"),
        h(
          "div.stack",
          h("label.check", mermaid, "Include Mermaid diagrams"),
          h("label.check", timestamps, "Include timestamps"),
          h("label.field", h("span", "Chapters per file"), perFile)
        ),
        h(
          "div",
          { style: { marginTop: "14px" } },
          runOrQueue({
            label: `Write notes for ${analysis.chapters.length} chapter(s)`,
            onRun: () =>
              jobPanel.run(api.notes(project.id, notesBody()), {
                title: "Writing notes",
                onDone: () => {
                  toast("Notes ready.", "ok");
                  ctx.refresh();
                },
              }),
            onQueue: () => queue.add((body) => api.notes(project.id, body), notesBody()),
          })
        )
      ),
      noteFiles.length
        ? fileListCard(project, noteFiles, ctx, {
            title: "Generated files",
            onPreview: showFile,
          })
        : null
    ),
    h(
      "div.card",
      h("h2", "Preview"),
      noteFiles.length ? preview : h("div.empty", "Generate notes to preview them here.")
    )
  );
}

// --- article -----------------------------------------------------------------
const ARTICLE_SHAPE_HELP = {
  "break-fix": "Something is broken. Fills symptoms, cause and a fix the reader can follow.",
  "how-to": "Nothing is broken. States what the task achieves, then the procedure.",
  reference: "Explains how something works, with the detail under More Information.",
};

function articleView(project, analysis, jobPanel, ctx, queue) {
  const title = h("input", { type: "text", placeholder: analysis.title || "Taken from the analysis" });
  const audience = h("input", {
    type: "text",
    placeholder: "e.g. front-line support engineers",
  });
  const outputName = h("input", { type: "text", placeholder: "article" });
  const extras = h("input", {
    type: "text",
    placeholder: "e.g. Escalation path, Known issues",
  });
  const preview = h("div.md");

  const enrichment = enrichmentSlider(
    "Anything the model adds beyond the recording is marked “Added context” in the " +
      "article, so a reader can always tell it from what was actually said. Specifics — " +
      "versions, error codes, paths, commands — are never invented at any level."
  );
  const diagram = h("input", { type: "checkbox", checked: true });
  const diagramRow = h(
    "div",
    // Shown only for Markdown, and the format starts as Word.
    { style: { display: "none" } },
    h("label.check", diagram, "Include a Mermaid diagram"),
    h(
      "div.faint.small",
      "A flowchart or sequence of what the article describes, drawn from the recording."
    )
  );

  let format = "docx";
  let shape = "break-fix";
  const shapeHelp = h("div.muted.small", ARTICLE_SHAPE_HELP[shape]);
  const templateNote = h("div.faint.small");
  const sectionList = h("div.stack.article-sections");
  const actions = h("div");

  const formatChoice = h("div.segmented.article-formats");
  const shapeChoice = h("div.segmented.article-shapes");

  const articleBody = () => ({
    format,
    shape,
    title: title.value.trim(),
    audience: audience.value.trim(),
    output_name: outputName.value.trim(),
    enrichment: enrichment.value(),
    include_diagram: diagram.checked,
    extra_sections: extras.value
      .split(",")
      .map((piece) => piece.trim())
      .filter(Boolean)
      .slice(0, 8),
  });

  function drawActions() {
    mount(
      actions,
      runOrQueue({
        label: `Write the article (.${format})`,
        onRun: () =>
          jobPanel.run(api.article(project.id, articleBody()), {
            title: "Writing the article",
            onDone: () => {
              toast("Article ready — see Outputs.", "ok");
              ctx.refresh();
            },
          }),
        onQueue: () => queue.add((body) => api.article(project.id, body), articleBody()),
      })
    );
  }

  function pick(container, options, current, onPick) {
    mount(
      container,
      options.map(([value, label]) =>
        h(
          "button",
          {
            "aria-selected": String(value === current),
            onclick: () => onPick(value),
          },
          label
        )
      )
    );
  }

  function drawFormats(formats) {
    pick(formatChoice, formats, format, (value) => {
      format = value;
      drawFormats(formats);
      drawActions();
      templateNote.textContent =
        format === "docx"
          ? "Written into the supplied Word template. Its sections, styling and " +
            "instructions are kept exactly as they are."
          : "Free-form Markdown, laid out for reading in a wiki or a repo.";
      sectionList.style.display = format === "docx" ? "" : "none";
      // Word cannot draw a Mermaid diagram, so offering one there would lie.
      diagramRow.style.display = format === "md" ? "" : "none";
    });
  }

  function drawShapes(shapes) {
    pick(shapeChoice, shapes, shape, (value) => {
      shape = value;
      drawShapes(shapes);
      shapeHelp.textContent = ARTICLE_SHAPE_HELP[shape] || "";
    });
  }

  const articleFiles = (project.artifacts || []).filter(
    (artifact) => artifact.kind === "article"
  );

  async function showFile(artifact) {
    mount(preview, h("div.faint.small", `Reading ${artifact.filename}…`));
    try {
      mount(preview, h("div", { html: markdownToHtml(await documentMarkdown(project, artifact)) }));
    } catch (error) {
      mount(preview, h("div.empty", error.message || "Could not load that file."));
    }
  }

  api
    .articleOptions()
    .then((options) => {
      drawFormats((options.formats || []).map((item) => [item.id, item.label]));
      drawShapes((options.shapes || []).map((item) => [item.id, item.label]));
      mount(
        sectionList,
        h("div.faint.small", "The template's own fields:"),
        h(
          "div.row.article-chips",
          (options.template_sections || []).map((name) => h("span.tag", name))
        )
      );
      if (!options.template_available) {
        templateNote.textContent =
          "The Word template is missing from tools/template/file.docx, so only " +
          "Markdown can be written.";
      }
    })
    .catch(() => {
      drawFormats([["docx", "Word (.docx)"], ["md", "Markdown (.md)"]]);
      drawShapes([["break-fix", "Break/fix"], ["how-to", "How-to"], ["reference", "Reference"]]);
    });

  drawActions();
  // Show the newest article straight away: an empty pane beside a list of files
  // reads as something being broken.
  if (articleFiles.length) showFile(articleFiles[articleFiles.length - 1]);

  return h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(320px, 1fr) minmax(0, 2fr)" } },
    h(
      "div.stack",
      h(
        "div.card",
        h("h2", "Write an article"),
        h(
          "p.muted.small",
          "One document that stands on its own, for someone who will never see " +
            "the recording. Everything in it comes from what was actually said."
        ),
        h("label.field", h("span", "Output"), formatChoice),
        templateNote,
        sectionList,
        diagramRow,
        h("hr"),
        h("label.field", h("span", "Article type"), shapeChoice),
        shapeHelp,
        h("hr"),
        enrichment,
        h("hr"),
        h("label.field", h("span", "Title"), title),
        h("label.field", h("span", "Written for"), audience),
        h(
          "label.field",
          h("span", "Extra sections"),
          extras,
          h("div.faint.small", "Comma separated. Added after the template's own sections.")
        ),
        h("label.field", h("span", "Save as"), outputName),
        h("div", { style: { marginTop: "14px" } }, actions)
      ),
      articleFiles.length
        ? fileListCard(project, articleFiles, ctx, {
            title: "Articles",
            onPreview: showFile,
          })
        : null
    ),
    h(
      "div.card",
      h("h2", "Preview"),
      articleFiles.length
        ? preview
        : h(
            "div.empty",
            "Write an article to see it here. A Word article is shown as it reads, " +
              "without the form's instructions."
          )
    )
  );
}

//: What the preview pane can render. Anything else is download-only, and the
//: Preview button is left off rather than offered and then quietly downloading.
const PREVIEWABLE = /\.(md|markdown|txt|docx)$/i;

/**
 * A file as Markdown. A Word document is read back by the server, which knows
 * how to unpack it; everything else is already text.
 */
async function documentMarkdown(project, artifact) {
  if (/\.docx$/i.test(artifact.filename)) {
    return (await api.outputDocument(project.id, artifact.filename)).markdown;
  }
  const response = await fetch(artifact.url);
  if (!response.ok) throw new Error(`Could not load ${artifact.filename}.`);
  return response.text();
}

/** A list of output files with select-to-zip, preview, download and delete. */
function fileListCard(project, artifacts, ctx, { title = "Files", onPreview = null } = {}) {
  const selected = new Set();
  const zipButton = h(
    "button.btn.btn-sm",
    { disabled: true, onclick: () => downloadZip() },
    "Download as ZIP"
  );

  function refresh() {
    zipButton.disabled = selected.size < 1;
    zipButton.textContent = selected.size
      ? `Download ${selected.size} as ZIP`
      : "Download as ZIP";
  }

  async function downloadZip() {
    zipButton.disabled = true;
    zipButton.textContent = "Zipping…";
    try {
      const result = await api.bundleOutputs(project.id, [...selected]);
      window.open(result.url, "_blank");
      toast(`${result.file} ready (${bytes(result.size_bytes)}).`, "ok");
      ctx.refresh();
    } catch (error) {
      toast(error.message, "err");
      refresh();
    }
  }

  const rows = artifacts.map((artifact) =>
    h(
      "div.artifact",
      h("input", {
        type: "checkbox",
        onchange: (event) => {
          if (event.target.checked) selected.add(artifact.filename);
          else selected.delete(artifact.filename);
          refresh();
        },
      }),
      h(
        "div",
        { style: { flex: "1", minWidth: "0" } },
        h("div.small", artifact.label),
        h("div.faint.small.mono", `${artifact.filename} · ${bytes(artifact.size_bytes)}`)
      ),
      onPreview && PREVIEWABLE.test(artifact.filename)
        ? h("button.btn.btn-sm.btn-ghost", { onclick: () => onPreview(artifact) }, "Preview")
        : null,
      h("a.btn.btn-sm", { href: artifact.url, download: artifact.filename }, "Download"),
      deleteButton(project, artifact, ctx)
    )
  );

  return h(
    "div.card",
    h("div.row-between", h("h3", { style: { margin: 0 } }, title), zipButton),
    h("div.stack", { style: { marginTop: "12px" } }, rows)
  );
}

function deleteButton(project, artifact, ctx) {
  return h(
    "button.btn.btn-sm.btn-ghost.btn-danger.icon-btn",
    {
      title: `Delete ${artifact.filename}`,
      "aria-label": `Delete ${artifact.filename}`,
      onclick: async () => {
        if (!(await confirmAction(`Delete ${artifact.filename}?`))) return;
        try {
          await api.deleteOutput(project.id, artifact.filename);
          toast(`${artifact.filename} deleted.`, "ok");
          ctx.refresh();
        } catch (error) {
          toast(error.message, "err");
        }
      },
    },
    // A bin drawn inline so the UI needs no icon font or sprite sheet.
    h("svg", {
      viewBox: "0 0 16 16",
      width: "14",
      height: "14",
      "aria-hidden": "true",
      html:
        '<path fill="currentColor" d="M6.5 1a1 1 0 0 0-1 1v.5H2.75a.75.75 0 0 0 0 1.5h.32l.64 9a1.75 1.75 0 0 0 1.75 1.62h5.08a1.75 1.75 0 0 0 1.75-1.62l.64-9h.32a.75.75 0 0 0 0-1.5H10.5V2a1 1 0 0 0-1-1h-3ZM7 2.5h2v.5H7v-.5Zm-2.42 2h6.84l-.63 8.9a.25.25 0 0 1-.25.22H5.46a.25.25 0 0 1-.25-.22l-.63-8.9ZM6.75 6a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 6.75 6Zm2.5 0a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 9.25 6Z"/>',
    })
  );
}

function renameButton(project, artifact, details, ctx) {
  return h(
    "button.btn.btn-sm.btn-ghost",
    {
      title: `Rename ${artifact.filename}`,
      onclick: () => {
        const input = h("input", {
          type: "text",
          value: artifact.filename,
          "aria-label": `New name for ${artifact.filename}`,
        });
        const save = h(
          "button.btn.btn-sm.btn-primary",
          {
            onclick: async () => {
              const name = input.value.trim();
              if (!name || name === artifact.filename) {
                ctx.refresh();
                return;
              }
              save.disabled = true;
              try {
                const renamed = await api.renameOutput(project.id, artifact.filename, name);
                toast(`${artifact.filename} renamed to ${renamed.file}.`, "ok");
                ctx.refresh();
              } catch (error) {
                save.disabled = false;
                toast(error.message, "err");
              }
            },
          },
          "Save"
        );
        mount(
          details,
          h("div.small", `Rename ${artifact.label}`),
          h("div.output-rename", input, save)
        );
        input.focus();
        input.select();
      },
    },
    "Rename"
  );
}

// --- make a clip -------------------------------------------------------------
/**
 * Put a tab back the way the user left it.
 *
 * A search that takes half a minute has to survive someone switching tabs, and
 * the result is a set of options they still have to choose from — so a running
 * job is reattached to, and a finished one is offered back rather than silently
 * replayed, which would be startling if they had moved on.
 */
function resumeSearch({ projectId, tab, kinds, jobPanel, results, searching, onRestore }) {
  const job = latestForTab(projectId, tab, kinds);
  if (!job) return;

  if (job.status === "queued" || job.status === "running") {
    searching(true);
    jobPanel.follow(job.id, {
      title: job.label,
      onDone: (result) => {
        searching(false);
        onRestore({ ...job, result });
      },
      onError: (error) => {
        searching(false);
        mount(results, h("div.empty", error.message));
      },
    });
    return;
  }

  if (job.status === "done" && job.result?.candidates?.length) {
    const when = job.meta?.query ? `for “${job.meta.query}”` : "from your last search";
    mount(
      results,
      h(
        "div.resume-note",
        h("span.small", `${job.result.candidates.length} option(s) ${when}, still here.`),
        h(
          "button.btn.btn-sm.btn-primary",
          { onclick: () => onRestore(job) },
          "Show them"
        )
      )
    );
  }
}

function clipTab(project, analysis, jobPanel, ctx, preset = null, queue = null) {
  let mode = preset?.mode === "span" ? "duration" : preset?.mode || "cleanup";
  const results = h("div");
  const paneHost = h("div");

  const minutes = numberField({ value: 1, min: 0, max: 180 });
  const seconds = numberField({ value: 30, min: 0, max: 59, step: 5 });
  const about = h("input", {
    type: "text",
    placeholder: "Optional — e.g. the retry policy design",
  });
  const topic = h("textarea", {
    rows: 3,
    placeholder:
      "Describe what you want. For example: the part where they explain how the " +
      "storage account firewall rules are configured.",
  });
  const highlightCount = numberField({ value: 5, min: 1, max: 20 });
  const highlightQuery = h("input", {
    type: "text",
    placeholder: "Optional — e.g. moments that explain a decision",
  });

  const durationPane = h(
    "div.stack",
    h(
      "p.muted.small",
      "Length is a soft target. If a thought runs a little long, the clip runs a " +
        "little long — a clean start and end matters more than an exact runtime."
    ),
    h(
      "div.grid.grid-2",
      h("label.field", h("span", "Minutes"), minutes),
      h("label.field", h("span", "Seconds"), seconds)
    ),
    h("label.field", h("span", "About (optional)"), about)
  );

  const topicPane = h(
    "div.stack",
    { style: { display: "none" } },
    h(
      "p.muted.small",
      "Finds every place that topic is discussed. If it comes up in several places " +
        "you can join them into one video, or export them separately."
    ),
    h("label.field", h("span", "What are you looking for?"), topic)
  );

  const highlightPane = h(
    "div.stack",
    { style: { display: "none" } },
    h(
      "p.muted.small",
      "Finds self-contained moments worth sharing on their own. Selected moments are " +
        "rendered on a 16:9 canvas and can be exported separately or joined into a reel."
    ),
    h("label.field", h("span", "How many to look for"), highlightCount),
    h(
      "label.field",
      h("span", "Looking for anything in particular?"),
      highlightQuery
    )
  );

  const findButton = h("button.btn.btn-primary", { onclick: () => find() }, "Find options");
  const queueFind = h(
    "button.btn.btn-ghost.btn-sm",
    {
      title: "Add this search to the queue and keep setting things up.",
      onclick: async () => {
        const body = findBody();
        if (!body) return;
        try {
          await queue.add((payload) => api.findClips(project.id, payload), body);
        } catch (error) {
          toast(error.message, "err");
        }
      },
    },
    "Add to queue"
  );

  function setMode(next) {
    mode = next;
    durationPane.style.display = next === "duration" ? "" : "none";
    topicPane.style.display = next === "topic" ? "" : "none";
    highlightPane.style.display = next === "highlight" ? "" : "none";
    for (const button of modeBar.children) {
      button.setAttribute("aria-selected", String(button.dataset.mode === next));
    }
    findButton.textContent = next === "highlight" ? "Find highlights" : "Find options";
    if (next === "cleanup") {
      mount(paneHost, cleanupView(project, analysis, jobPanel, ctx, queue));
      return;
    }
    mount(paneHost, clipGrid);
    mount(results, h("div.empty", "Choose your options, then find."));
  }

  const modeBar = h(
    "div.segmented",
    h(
      "button",
      { "data-mode": "cleanup", "aria-selected": "true", onclick: () => setMode("cleanup") },
      "Clean cut"
    ),
    h(
      "button",
      { "data-mode": "duration", "aria-selected": "false", onclick: () => setMode("duration") },
      "By length"
    ),
    h(
      "button",
      { "data-mode": "topic", "aria-selected": "false", onclick: () => setMode("topic") },
      "By topic"
    ),
    h(
      "button",
      {
        "data-mode": "highlight",
        "aria-selected": "false",
        onclick: () => setMode("highlight"),
      },
      "Highlights"
    )
  );

  async function find() {
    const body = findBody();
    if (!body) return;
    const query = body.query;

    searching(true);
    jobPanel.run(
      api.findClips(project.id, body),
      {
        title:
          mode === "topic"
            ? `Searching for “${query}”`
            : mode === "highlight"
              ? "Finding highlights"
              : "Finding clip options",
        onDone: (result) => {
          searching(false);
          showResults(result, result.query || query);
        },
        onError: (error) => {
          searching(false);
          mount(results, h("div.empty", error.message));
        },
      }
    );
  }

  /** The search as the endpoint wants it, or null when the form is incomplete. */
  function findBody() {
    const query =
      mode === "topic"
        ? topic.value.trim()
        : mode === "highlight"
          ? highlightQuery.value.trim()
          : about.value.trim();
    if (mode === "topic" && !query) {
      toast("Describe what the clip should be about.", "err");
      return null;
    }
    const target = minutes.value * 60 + seconds.value;
    if (mode === "duration" && target < 2) {
      toast("Choose a length of at least a couple of seconds.", "err");
      return null;
    }
    return {
      mode,
      target_seconds: target,
      query,
      count: mode === "highlight" ? highlightCount.value : null,
      use_llm: true,
    };
  }

  function searching(active) {
    findButton.disabled = active;
    findButton.textContent = active
      ? "Searching…"
      : mode === "highlight"
        ? "Find highlights"
        : "Find options";
    if (active) {
      mount(
        results,
        h(
          "div.empty",
          "Reading the transcript. This keeps running if you switch tabs — the " +
            "Jobs chip above will bring you back."
        )
      );
    }
  }

  function showResults(found, query) {
    if (!found.found || !found.candidates.length) {
      mount(
        results,
        h(
          "div.card",
          h("h3", "Nothing matched"),
          h(
            "p.muted",
            found.note ||
              (mode === "highlight"
                ? "No self-contained moments stood out. Try a specific topic or make " +
                  "a clip by length instead."
                : "That topic does not appear to be discussed in this recording. Try " +
                  "different wording, or search the Transcript tab directly.")
          )
        )
      );
      return;
    }

    mount(
      results,
      h(
        "div.card",
        h(
          "div.row-between",
          h(
            "h3",
            { style: { margin: 0 } },
            mode === "highlight"
              ? `${found.candidates.length} highlight(s)`
              : mode === "topic"
              ? `${found.candidates.length} section(s) about “${query}”`
              : `${found.candidates.length} option(s)`
          ),
          h(
            "span.faint.small",
            mode === "highlight" ? "Rendered on a 16:9 canvas" : "Pick what you want, then render"
          )
        ),
        found.warnings?.length
          ? h(
              "div.faint.small",
              { style: { marginBottom: "10px" } },
              found.warnings.join(" ")
            )
          : null,
        candidatePicker({
          candidates: found.candidates,
          actionLabel: mode === "highlight" ? "Export selected" : "Render selected",
          allowCombine: true,
          combineDefault: mode === "topic",
          combineLabel:
            mode === "highlight"
              ? "Join into one reel instead of separate files"
              : "Join into a single video, with a short transition between each section",
          outputNameDefault:
            mode === "highlight" ? "highlight.mp4" : mode === "topic" ? "topic.mp4" : "clip.mp4",
          onPreview: (candidate) => jumpTo(candidate.start),
          onRender: (selection, options) =>
            jobPanel.run(
              api.renderClips(project.id, {
                selections: selection.map((candidate) => ({
                  title: candidate.title,
                  start: candidate.start,
                  end: candidate.end,
                  summary: candidate.summary || "",
                })),
                combine: options.combine,
                reframe: mode === "highlight",
                prefix: mode === "highlight" ? "highlight" : mode === "topic" ? "topic" : "clip",
                title: query || (mode === "highlight" ? "highlights" : selection[0].title),
                output_name: options.outputName,
              }),
              {
                title:
                  mode === "highlight"
                    ? "Rendering highlights"
                    : options.combine
                      ? "Joining and encoding"
                      : "Rendering clips",
                onDone: () => {
                  toast(
                    mode === "highlight"
                      ? "Highlights ready — see Outputs."
                      : "Ready — see Outputs.",
                    "ok"
                  );
                  ctx.refresh();
                },
              }
            ),
        })
      )
    );
  }

  const clipGrid = h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(300px, 1fr) minmax(0, 1.5fr)" } },
    h(
      "div.card",
      h("h2", "Cut a clip"),
      h("div", { style: { marginTop: "4px" } }, durationPane, topicPane, highlightPane),
      h("div.row.run-row", { style: { marginTop: "14px" } }, findButton, queueFind)
    ),
    results
  );

  mount(results, h("div.empty", "Choose your options, then find."));
  resumeSearch({
    projectId: project.id,
    tab: "clip",
    kinds: ["clips-find", "highlights-find"],
    jobPanel,
    results,
    searching,
    onRestore: (job) => {
      setMode(job.result.mode || (job.kind === "highlights-find" ? "highlight" : "duration"));
      showResults(job.result, job.result.query || "");
    },
  });

  setMode(mode);
  if (preset && preset.mode && preset.mode !== "span") {
    if (preset.query) {
      topic.value = preset.query;
      highlightQuery.value = preset.query;
    }
    if (preset.count) highlightCount.value = preset.count;
    find();
  }

  return h("div.stack", h("div.subtab-bar", modeBar), paneHost);
}

// --- cleanup -----------------------------------------------------------------
function cleanupView(project, analysis, jobPanel, ctx, queue) {
  const options = {
    remove_silence: true,
    remove_filler: true,
    remove_off_topic: true,
    remove_admin: true,
    remove_qa: false,
    remove_intro: false,
    remove_outro: false,
  };

  const summary = h("div.muted", "Calculating…");
  const bar = h("div.bar", h("i", { style: { width: "0%" } }));
  const detail = h("div.faint.small");
  const breakdown = h("div", { style: { marginTop: "12px" } });
  const protectedNote = h("div.faint.small", { style: { marginTop: "8px" } });
  const outputName = h("input", { type: "text", value: "cleaned.mp4" });
  const cleanupFiles = (project.artifacts || []).filter(
    (artifact) => artifact.kind === "cleanup"
  );

  const cleanupBody = () => ({
    ...options,
    keep_threshold: Number(threshold.value),
    output_name: outputName.value || "cleaned.mp4",
  });

  const refreshPlan = debounce(async () => {
    try {
      const plan = await api.cleanupPlan(project.id, {
        ...options,
        keep_threshold: Number(threshold.value),
      });
      summary.textContent = `${duration(plan.kept_seconds)} kept of ${duration(
        plan.original_seconds
      )} — ${plan.removed_percent}% removed`;
      bar.firstElementChild.style.width = `${
        (plan.kept_seconds / Math.max(1, plan.original_seconds)) * 100
      }%`;
      detail.textContent = `${plan.span_count} segments will be joined.`;

      const entries = Object.entries(plan.removed_by_kind || {});
      mount(
        breakdown,
        entries.length
          ? h(
              "table",
              h("thead", h("tr", h("th", "Removed"), h("th", "Time"))),
              h(
                "tbody",
                entries.map(([kind, secs]) =>
                  h("tr", h("td", KIND_LABEL[kind] || kind), h("td.nowrap", duration(secs)))
                )
              )
            )
          : h("div.faint.small", "Nothing is being removed by category.")
      );

      protectedNote.textContent = plan.protected_seconds
        ? `${duration(plan.protected_seconds)} was kept despite matching a category ` +
          `you ticked, because the analysis rated it highly. Classification is not ` +
          `perfect, and losing a valuable tangent costs more than a few extra seconds.`
        : "";
    } catch (error) {
      summary.textContent = error.message;
    }
  }, 220);

  const threshold = h("input", {
    type: "range",
    min: "0",
    max: "0.9",
    step: "0.01",
    value: "0.38",
    oninput: () => {
      thresholdLabel.textContent = Number(threshold.value).toFixed(2);
      refreshPlan();
    },
  });
  const thresholdLabel = h("span.mono", "0.38");

  const qaNote = h(
    "div.faint.small",
    { style: { display: "none", margin: "-4px 0 4px 24px" } },
    "Every question and answer will be removed, including ones that contain useful " +
      "detail. Unlike the other categories, this is not filtered by importance."
  );

  const toggles = [
    ["remove_silence", "Long silences", ""],
    ["remove_filler", "Filler and false starts", ""],
    ["remove_admin", "Logistics (“can you hear me”, waiting)", ""],
    [
      "remove_off_topic",
      "Off-topic chatter",
      "High-value moments are kept even if labelled off-topic.",
    ],
    ["remove_qa", "Q&A", ""],
    ["remove_intro", "Intro", ""],
    ["remove_outro", "Outro and sign-off", ""],
  ].flatMap(([key, label, hint]) => {
    const row = h(
      "label.check",
      h("input", {
        type: "checkbox",
        checked: options[key],
        onchange: (event) => {
          options[key] = event.target.checked;
          if (key === "remove_qa") qaNote.style.display = event.target.checked ? "" : "none";
          refreshPlan();
        },
      }),
      label
    );
    const extras = [row];
    if (hint) {
      extras.push(h("div.faint.small", { style: { margin: "-4px 0 4px 24px" } }, hint));
    }
    if (key === "remove_qa") extras.push(qaNote);
    return extras;
  });

  refreshPlan();

  return h(
    "div.grid",
    { style: { gridTemplateColumns: "minmax(300px, 1fr) minmax(0, 1.2fr)" } },
    h(
      "div.card",
      h("h2", "Remove"),
      h("div.stack", toggles),
      h(
        "label.field",
        { style: { marginTop: "16px" } },
        h("span", ["Keep anything above importance ", thresholdLabel]),
        threshold
      ),
      h("label.field", { style: { marginTop: "16px" } }, h("span", "Save as"), outputName),
      h(
        "div",
        { style: { marginTop: "16px" } },
        runOrQueue({
          label: "Render the clean cut",
          onRun: () =>
            jobPanel.run(api.cleanup(project.id, cleanupBody()), {
              title: "Rendering the clean cut",
              onDone: () => {
                toast("Clean cut ready — see Outputs.", "ok");
                ctx.refresh();
              },
            }),
          onQueue: () => queue.add((body) => api.cleanup(project.id, body), cleanupBody()),
        })
      )
    ),
    h(
      "div.stack",
      h(
        "div.card",
        h("h2", "What you will get"),
        summary,
        h("div", { style: { marginTop: "10px" } }, bar),
        h("div", { style: { marginTop: "8px" } }, detail),
        breakdown,
        protectedNote,
        h("hr"),
        h(
          "p.faint.small",
          "The cut is re-encoded so every join lands exactly on the word boundary from " +
            "the transcript, not on the nearest keyframe. Long gaps get a short fade so " +
            "a jump reads as intentional."
        )
      ),
      cleanupFiles.length
        ? fileListCard(project, cleanupFiles, ctx, { title: "Clean-cut outputs" })
        : null
    )
  );
}

// --- editor ------------------------------------------------------------------
function editorTab(project, analysis, jobPanel, ctx, goToTab, queue) {
  const state = { assets: [], active: "prompt", filter: "" };
  const container = h("div.stack");

  const videoOutputs = (project.artifacts || []).filter((artifact) =>
    /\.(mp4|mov|mkv|webm)$/i.test(artifact.filename)
  );

  const preview = h("video.media-preview", {
    controls: true,
    preload: "metadata",
    style: { display: "none" },
  });
  const showPreview = (url) => {
    preview.src = url;
    preview.style.display = "block";
  };

  const mediaList = h("div.media-scroller");
  const mediaCount = h("span.pill");
  const search = h("input.media-search", {
    type: "search",
    placeholder: "Filter media…",
    oninput: (event) => {
      state.filter = event.target.value.trim().toLowerCase();
      drawMediaList();
    },
  });

  function mediaRow({ name, meta, badge, url, onRemove, sourceKind }) {
    return h(
      "div.media-row",
      h("div.kind", "VID"),
      h(
        "div.media-row-details",
        h("strong.small", name),
        h("div.faint.small", meta)
      ),
      h("span.tag", badge),
      h("button.btn.btn-sm.btn-ghost", { onclick: () => showPreview(url) }, "Play"),
      h(
        "button.btn.btn-sm.btn-ghost",
        {
          onclick: () =>
            openExportDialog({
              project,
              filename: name,
              sourceKind,
              directUrl: url,
              jobPanel,
              ctx,
            }),
        },
        "Save as…"
      ),
      onRemove
        ? h("button.btn.btn-sm.btn-ghost.btn-danger", { onclick: onRemove }, "Remove")
        : null
    );
  }

  function drawMediaList() {
    const needle = state.filter;
    const imported = state.assets
      .filter((asset) => !needle || asset.name.toLowerCase().includes(needle))
      .map((asset) =>
        mediaRow({
          name: asset.name,
          meta: bytes(asset.size_bytes),
          badge: "Imported",
          url: asset.url,
          sourceKind: "media",
          onRemove: async () => {
            const ok = await confirmAction(
              `Remove ${asset.name} from this project?\n\n` +
                "It is deleted from this project's media folder. Anything already " +
                "rendered from it is kept, and other projects are untouched."
            );
            if (!ok) return;
            await api.deleteMedia(project.id, asset.name);
            toast(`${asset.name} removed.`, "ok");
            draw();
          },
        })
      );
    const generated = videoOutputs
      .filter((artifact) => !needle || artifact.filename.toLowerCase().includes(needle))
      .map((artifact) =>
        mediaRow({
          name: artifact.filename,
          meta:
            `${artifact.label} · ${bytes(artifact.size_bytes)}` +
            (artifact.duration_s ? ` · ${duration(artifact.duration_s)}` : ""),
          badge: "Generated",
          url: artifact.url,
          sourceKind: "output",
        })
      );
    const rows = [...imported, ...generated];
    mediaCount.textContent = `${rows.length}`;
    mount(
      mediaList,
      rows.length
        ? rows
        : h(
            "div.empty.empty-sm",
            needle ? "Nothing matches that filter." : "No media in this project yet."
          )
    );
  }

  async function openLibrary() {
    let payload;
    try {
      payload = await api.mediaLibrary(project.id);
    } catch (error) {
      toast(error.message, "err");
      return;
    }
    const sources = payload.sources || [];
    const body = sources.length
      ? h(
          "div.stack",
          sources.map((source) =>
            h(
              "div.library-group",
              h(
                "div.library-group-head",
                h("strong.small", source.title),
                source.project_id ? h("span.tag", "Project") : null
              ),
              source.items.map((item) =>
                h(
                  "div.media-row",
                  h("div.kind", "VID"),
                  h(
                    "div.media-row-details",
                    h("strong.small", item.name),
                    h(
                      "div.faint.small",
                      `${bytes(item.size_bytes)} · from ${source.title}`
                    )
                  ),
                  h(
                    "button.btn.btn-sm",
                    {
                      onclick: async (event) => {
                        event.target.disabled = true;
                        try {
                          await api.adoptMedia(project.id, item.name, source.project_id);
                          toast(`${item.name} copied in from ${source.title}.`, "ok");
                          await draw();
                        } catch (error) {
                          toast(error.message, "err");
                          event.target.disabled = false;
                        }
                      },
                    },
                    "Copy here"
                  )
                )
              )
            )
          )
        )
      : h("div.empty.empty-sm", "No other project has imported media yet.");

    openDialog({
      title: "Copy media from another project",
      hint:
        "Media stays with the project it was imported into. Copying makes an " +
        "independent copy here.",
      body,
    });
  }

  async function draw() {
    state.assets = await api.listMedia(project.id).catch(() => []);
    drawMediaList();

    const toolBody = h("div.tool-body");
    const tools = [
      ["prompt", "Prompt edit", () => promptEditPane()],
      ["intro", "Intro studio", () => introPane()],
      ["clipping", "Clipping", () => clippingPane()],
    ];
    const toolTabs = h(
      "div.tabs.editor-subtabs",
      { role: "tablist", "aria-label": "Editor tools" },
      tools.map(([key, label]) =>
        h(
          "button",
          {
            role: "tab",
            "aria-selected": String(key === state.active),
            onclick: () => {
              state.active = key;
              for (const button of toolTabs.children) {
                button.setAttribute("aria-selected", String(button.dataset.tool === key));
              }
              showTool();
            },
            "data-tool": key,
          },
          label
        )
      )
    );

    function showTool() {
      const selected = tools.find(([key]) => key === state.active) || tools[0];
      mount(toolBody, selected[2]());
    }

    mount(
      container,
      h(
        "div.card.media-dashboard",
        h(
          "div.media-dashboard-grid",
          h(
            "section.media-panel",
            h("div.panel-head", h("h2", "Add media")),
            createAssetImporter({ projectId: project.id, jobPanel, onChanged: draw }),
            h("hr"),
            h(
              "div.row-between",
              h("span.faint.small", "Already imported somewhere else?"),
              h("button.btn.btn-sm", { onclick: openLibrary }, "Browse other projects")
            )
          ),
          h(
            "section.media-panel",
            h(
              "div.panel-head",
              h("div.row", h("h2", "Project media"), mediaCount),
              h("div.panel-head-actions", search)
            ),
            mediaList,
            preview
          )
        )
      ),
      h("div.card", h("h2", "Editing tools"), toolTabs, toolBody)
    );
    showTool();
  }

  function clippingPane() {
    return createClippingPane({ project, state, videoOutputs, jobPanel, ctx, queue });
  }

  function promptEditPane() {
    return createPromptPane({
      project,
      analysis,
      state,
      jobPanel,
      ctx,
      queue,
      onGoToTab: (tab, preset) => goToTab?.(tab, preset),
    });
  }
  function introPane() {
    return createIntroPane({ project, analysis, jobPanel, ctx, queue });
  }
  draw();
  return container;
}

// --- outputs -----------------------------------------------------------------
//: Outputs are grouped by the tab that made them, in the order those tabs appear.
const OUTPUT_GROUPS = [
  [
    "Transcript & Notes",
    "Written from the recording",
    new Set(["transcript", "notes", "article", "summary"]),
  ],
  ["Cuts", "Clean cuts, clips and highlights", new Set(["clip", "cleanup", "highlight"])],
  ["Editor", "Intros, prompt edits and assembled cuts", new Set(["intro", "edit", "bookend"])],
  ["Exports", "Re-encoded copies", new Set(["export"])],
  ["Bundles", "Zipped selections", new Set(["bundle"])],
];

/**
 * Intros and prompt edits were both stored as "clip" before they had their own
 * kinds, so older projects are placed by the metadata each one still carries.
 */
function outputGroupOf(artifact) {
  if (artifact.kind !== "clip") return artifact.kind;
  if (artifact.meta?.style) return "intro";
  if (artifact.meta?.operations) return "edit";
  return "clip";
}

function outputsTab(project, ctx, jobPanel) {
  const container = h("div.outputs-page");

  (async () => {
    let info;
    try {
      info = await api.listOutputs(project.id);
    } catch (error) {
      mount(container, h("div.empty", error.message));
      return;
    }

    const artifacts = (info.artifacts || []).filter((artifact) => artifact.exists);
    const player = h("video", { controls: true, style: { display: "none" } });
    const document_ = h("div.md.doc-preview", { style: { display: "none" } });
    const previewHint = h(
      "div.faint.small",
      "Choose Play on a video, or Preview on a document."
    );
    const previewTitle = h("h3", "Preview");
    const selected = new Set();

    function showVideo(artifact) {
      document_.style.display = "none";
      player.src = `/api/projects/${project.id}/preview/${encodeURIComponent(
        artifact.filename
      )}`;
      player.style.display = "";
      previewTitle.textContent = artifact.filename;
      previewHint.textContent = artifact.label;
      player.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    async function showDocument(artifact) {
      player.pause?.();
      player.style.display = "none";
      document_.style.display = "";
      previewTitle.textContent = artifact.filename;
      previewHint.textContent = artifact.label;
      mount(document_, h("div.faint.small", "Loading…"));
      try {
        const response = await fetch(artifact.url);
        if (!response.ok) throw new Error(`Could not read ${artifact.filename}.`);
        const body = await response.text();
        // Markdown is rendered; subtitles and plain text stay verbatim.
        mount(
          document_,
          /\.md$/i.test(artifact.filename)
            ? h("div", { html: markdownToHtml(body) })
            : h("pre.snippet", body)
        );
      } catch (error) {
        mount(document_, h("div.faint.small", error.message));
      }
      document_.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    const zipButton = h(
      "button.btn.btn-sm",
      { disabled: true, onclick: () => downloadZip() },
      "Download as ZIP"
    );

    function refresh() {
      zipButton.disabled = selected.size < 1;
      zipButton.textContent = selected.size
        ? `Download ${selected.size} as ZIP`
        : "Download as ZIP";
    }

    async function downloadZip() {
      zipButton.disabled = true;
      zipButton.textContent = "Zipping…";
      try {
        const result = await api.bundleOutputs(project.id, [...selected], project.title);
        window.open(result.url, "_blank");
        toast(`${result.file} ready (${bytes(result.size_bytes)}).`, "ok");
        ctx.refresh();
      } catch (error) {
        toast(error.message, "err");
        refresh();
      }
    }

    const openFolder = h(
      "button.btn.btn-sm",
      {
        disabled: !info.can_reveal,
        title: info.can_reveal
          ? info.folder
          : "Only available when ClipDesk runs on this machine",
        onclick: async () => {
          try {
            await api.revealOutputs(project.id);
          } catch (error) {
            toast(error.message, "err");
          }
        },
      },
      "Open folder"
    );

    if (!artifacts.length) {
      mount(
        container,
        sourceCard(project, info.source, ctx),
        h(
          "div.card",
          h(
            "div.row-between",
            h("h2", { style: { margin: 0 } }, "Outputs"),
            h("div.row", openFolder)
          ),
          h(
            "div.empty",
            { style: { marginTop: "14px" } },
            "Nothing produced yet. Generate notes, a clip or a clean cut."
          ),
          h("div.faint.small.mono", { style: { marginTop: "10px" } }, info.folder)
        )
      );
      return;
    }

    const rows = new Map(
      artifacts.map((artifact) => {
        const extension = artifact.filename.split(".").pop();
        const isVideo = ["mp4", "mov", "mkv", "webm"].includes(extension);
        const details = h(
          "div",
          { style: { flex: "1", minWidth: "0" } },
          h("div", artifact.label),
          h(
            "div.faint.small.mono",
            `${artifact.filename} · ${bytes(artifact.size_bytes)}` +
              (artifact.duration_s ? ` · ${duration(artifact.duration_s)}` : "")
          ),
          artifact.meta?.summary
            ? h("div.faint.small", { style: { marginTop: "3px" } }, artifact.meta.summary)
            : null
        );
        return [
          artifact,
          h(
            "div.artifact",
            h("input", {
              type: "checkbox",
              onchange: (event) => {
                if (event.target.checked) selected.add(artifact.filename);
                else selected.delete(artifact.filename);
                refresh();
              },
            }),
            h("div.kind", extension),
            details,
            isVideo
              ? h("button.btn.btn-sm", { onclick: () => showVideo(artifact) }, "Play")
              : h("button.btn.btn-sm", { onclick: () => showDocument(artifact) }, "Preview"),
            renameButton(project, artifact, details, ctx),
            isVideo
              ? h(
                  "button.btn.btn-sm",
                  {
                    onclick: () =>
                      openExportDialog({
                        project,
                        filename: artifact.filename,
                        sourceKind: "output",
                        directUrl: artifact.url,
                        jobPanel,
                        ctx,
                      }),
                  },
                  "Save as…"
                )
              : null,
            h("a.btn.btn-sm", { href: artifact.url, download: artifact.filename }, "Download"),
            deleteButton(project, artifact, ctx)
          ),
        ];
      })
    );

    const grouped = OUTPUT_GROUPS.map(([label, hint, kinds]) => {
      const members = artifacts.filter((artifact) => kinds.has(outputGroupOf(artifact)));
      return { label, hint, members };
    }).filter((group) => group.members.length);

    // A kind nobody mapped must still be reachable, never silently hidden.
    const placed = new Set(grouped.flatMap((group) => group.members));
    const leftover = artifacts.filter((artifact) => !placed.has(artifact));
    if (leftover.length) {
      grouped.push({ label: "Other", hint: "", members: leftover });
    }

    mount(
      container,
      sourceCard(project, info.source, ctx),
      h(
        "div.grid",
        { style: { gridTemplateColumns: "minmax(0, 1fr) minmax(300px, 0.8fr)" } },
        h(
          "div.card",
          h(
            "div.row-between",
            h("h2", { style: { margin: 0 } }, `Outputs (${artifacts.length})`),
            h("div.row", zipButton, openFolder)
          ),
          h(
            "div.output-groups",
            grouped.map((group) =>
              h(
                "section.output-group",
                h(
                  "div.output-group-head",
                  h("h3", group.label),
                  h("span.pill", String(group.members.length)),
                  h("span.faint.small", group.hint)
                ),
                h("div.stack", group.members.map((artifact) => rows.get(artifact)))
              )
            )
          ),
          h("div.faint.small.mono", { style: { marginTop: "12px" } }, info.folder)
        ),
        h(
          "div.card",
          previewTitle,
          player,
          document_,
          previewHint
        )
      )
    );
    refresh();
  })();

  return container;
}

// Importing from a link downloads the whole recording, which the user never
// explicitly asked to keep and which is usually by far the largest file on disk.
// It is not an artifact, so without this it is invisible and unremovable.
function sourceCard(project, source, ctx) {
  if (!source) return null;

  if (!source.exists) {
    return h(
      "div.card",
      h("h2", "Original recording"),
      h(
        "div.empty",
        { style: { marginTop: "12px" } },
        "The original file has been deleted. The transcript, notes and anything " +
          "already rendered are still here, but no new clips or cuts can be made."
      )
    );
  }

  const deleteButton = h(
    "button.btn.btn-sm.btn-danger",
    {
      onclick: async () => {
        const ok = await confirmAction(
          "Delete the original recording?\n\n" +
            `This frees ${bytes(source.size_bytes)}. The transcript, notes and ` +
            "anything already in Outputs are kept.\n\n" +
            "But every clip, clean cut and render is made from this file, so once " +
            "it is gone you cannot produce anything new from this recording.\n\n" +
            (source.imported
              ? "You can import it from the same link again."
              : "You would have to upload the file again.")
        );
        if (!ok) return;
        try {
          await api.deleteSource(project.id);
          toast(`Freed ${bytes(source.size_bytes)}.`, "ok");
          ctx.refresh();
        } catch (error) {
          toast(error.message, "err");
        }
      },
    },
    "Delete original"
  );

  return h(
    "div.card",
    h(
      "div.row-between",
      h("h2", { style: { margin: 0 } }, "Original recording"),
      h("div.row", deleteButton)
    ),
    h(
      "div.artifact",
      { style: { marginTop: "12px" } },
      h("div.kind", (source.filename.split(".").pop() || "file").slice(0, 4)),
      h(
        "div",
        { style: { flex: "1", minWidth: "0" } },
        h("div", source.filename),
        h("div.faint.small.mono", bytes(source.size_bytes)),
        source.imported
          ? h(
              "div.faint.small",
              { style: { marginTop: "3px" } },
              "Downloaded by ClipDesk from ",
              h(
                "a",
                { href: source.source_url, target: "_blank", rel: "noreferrer noopener" },
                source.source_url
              )
            )
          : h(
              "div.faint.small",
              { style: { marginTop: "3px" } },
              "Uploaded by you. ClipDesk keeps its own copy."
            )
      )
    ),
    source.imported
      ? h(
          "div.faint.small",
          { style: { marginTop: "10px" } },
          "ClipDesk had to download the full video to work on it. It is safe to " +
            "delete once you have the outputs you need."
        )
      : null
  );
}
