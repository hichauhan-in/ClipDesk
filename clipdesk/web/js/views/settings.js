// Settings: what is installed, which model answers, and how ClipDesk behaves.

import { api } from "../api.js";
import { h, loadingView, mount, numberField, toast } from "../dom.js";
import { createJobPanel } from "../jobpanel.js";

// The two Copilot routes are first-class choices; everything else is one
// "other provider" picker, because they are all the same shape with different
// values and listing them individually would bury the common case.
const COPILOT = [
  {
    key: "vscode",
    label: "GitHub Copilot — via VS Code",
    recommended: true,
    blurb:
      "Uses your Copilot seat through VS Code's Language Model API, the way Microsoft " +
      "intends extensions to use it. Fastest and most reliable. Needs a VS Code window " +
      "open with the ClipDesk Bridge extension installed.",
  },
  {
    key: "copilot_cli",
    label: "GitHub Copilot — via the CLI",
    blurb:
      "Same seat, no VS Code needed. Slower (25–45s per request) and occasionally " +
      "refuses bulk text work, because the CLI is a coding agent. Use it when VS Code " +
      "is not available.",
  },
];

const VIEW_KEY = "clipdesk.settings.view";

export async function renderSettings(root, ctx) {
  // Basic needs only the cached health probe and the config file. Advanced also
  // probes every provider, which is the slow part, so it is fetched on demand.
  const [settings, health] = await Promise.all([
    api.getSettings().catch(() => null),
    api.health().catch(() => null),
  ]);

  if (!settings) {
    mount(root, h("div.empty", "Could not reach the ClipDesk server."));
    return;
  }

  const jobPanel = createJobPanel();
  const body = h("div");
  let mode = localStorage.getItem(VIEW_KEY) === "advanced" ? "advanced" : "basic";
  let setupRequest = null;

  /**
   * The provider probe behind Advanced, fetched once and reused. Failure is
   * resolved rather than thrown so a background warm-up can never surface as an
   * unhandled rejection; the caller reads the outcome instead.
   */
  function loadSetup({ retry = false } = {}) {
    if (retry) setupRequest = null;
    if (!setupRequest) {
      const request = api.setup().then(
        (payload) => ({ payload }),
        (error) => ({ error })
      );
      request.then(() => {
        request.settled = true;
      });
      setupRequest = request;
    }
    return setupRequest;
  }

  const toggle = h(
    "div.segmented.view-toggle",
    { role: "tablist", "aria-label": "Settings detail" },
    ["basic", "advanced"].map((key) =>
      h(
        "button",
        {
          role: "tab",
          "data-view": key,
          "aria-selected": String(key === mode),
          onclick: () => setMode(key),
        },
        key === "basic" ? "Basic" : "Advanced"
      )
    )
  );

  async function setMode(next) {
    mode = next;
    localStorage.setItem(VIEW_KEY, next);
    for (const button of toggle.children) {
      button.setAttribute("aria-selected", String(button.dataset.view === next));
    }
    await draw();
  }

  async function draw() {
    if (mode === "basic") {
      mount(body, basicView(settings, health, jobPanel, ctx, () => setMode("advanced")));
      // Anyone who opens Settings is quite likely heading for Advanced, so warm
      // the slow provider probe now and the switch costs nothing.
      loadSetup();
      return;
    }

    const pending = loadSetup();
    // A warmed-up switch goes straight to the real thing; only a genuinely
    // in-flight probe gets the placeholder, so there is no flash either way.
    if (!pending.settled) {
      mount(body, loadingView("Checking every model provider…", { cards: 3 }));
    }

    const { payload, error } = await pending;
    if (mode !== "advanced" || ctx.isCurrent?.() === false) return;
    if (error) {
      setupRequest = null;
      mount(
        body,
        h(
          "div.card",
          h("h2", "Could not load the full settings"),
          h("p.muted", error.message),
          h("button.btn", { onclick: () => draw() }, "Try again")
        )
      );
      return;
    }
    mount(
      body,
      h(
        "div.settings-advanced",
        dependenciesCard(payload, jobPanel, ctx),
        modelCard(payload, settings, ctx),
        downloadsCard(settings, ctx),
        preferencesCard(settings, ctx)
      )
    );
  }

  mount(
    root,
    h(
      "div.page-head",
      h(
        "div.row-between",
        h(
          "div",
          h("h1", "Settings"),
          h(
            "div.sub",
            "Everything ClipDesk needs lives in this folder. Nothing is installed system-wide."
          )
        ),
        h(
          "div.row",
          h("div.faint.small.mono", `ClipDesk v${settings.version}`),
          toggle
        )
      )
    ),
    jobPanel.el,
    body
  );
  await draw();
}

// --- basic -------------------------------------------------------------------
const PROVIDER_LABEL = {
  vscode: "GitHub Copilot — via VS Code",
  copilot_cli: "GitHub Copilot — via the CLI",
};

/** One line per thing worth knowing, each with a way through to the detail. */
function summaryRow({ label, value, status, action }) {
  return h(
    "div.summary-row",
    h(
      "div.summary-text",
      h("strong.small", label),
      h("div.muted.small", value)
    ),
    status || null,
    action || null
  );
}

function basicView(settings, health, jobPanel, ctx, goAdvanced) {
  const components = health?.components || [];
  const missing = components.filter((component) => !component.installed);
  const llm = health?.llm;
  const providerLabel =
    PROVIDER_LABEL[settings.llm_provider] ||
    llm?.label ||
    settings.llm_provider ||
    "Not configured";

  const change = (title) =>
    h("button.btn.btn-sm.btn-ghost", { onclick: goAdvanced, title }, "Change");

  return h(
    "div.card",
    h(
      "div.row-between",
      h("h2", { style: { margin: 0 } }, "At a glance"),
      missing.length
        ? h(
            "button.btn.btn-primary.btn-sm",
            {
              onclick: () =>
                jobPanel.run(api.provision("all"), {
                  title: "Installing dependencies",
                  onDone: () => {
                    toast("All dependencies installed.", "ok");
                    ctx.refresh();
                  },
                }),
            },
            `Install all (${missing.length})`
          )
        : h("span.pill.pill-good", h("i.dot"), "Everything is installed")
    ),
    h(
      "p.muted.small",
      { style: { marginTop: "10px" } },
      "The settings most people ever need. Switch to Advanced for every option, " +
        "including per-provider models, keys and encoder tuning."
    ),
    h(
      "div.summary-list",
      summaryRow({
        label: "Dependencies",
        value: missing.length
          ? `${missing.map((component) => component.label).join(", ")} still to install`
          : `${components.length} installed — ffmpeg, yt-dlp and the speech model`,
        status: missing.length
          ? h("span.pill.pill-warn", h("i.dot"), `${missing.length} missing`)
          : h("span.pill.pill-good", h("i.dot"), "Ready"),
        action: change("See each dependency"),
      }),
      summaryRow({
        label: "Language model",
        value: llm?.active_model ? `${providerLabel} · ${llm.active_model}` : providerLabel,
        status: llm
          ? llm.available
            ? h("span.pill.pill-good", h("i.dot"), "Ready")
            : h("span.pill.pill-muted", h("i.dot"), "Not available")
          : null,
        action: change("Choose a different provider"),
      }),
      summaryRow({
        label: "Speech-to-text",
        value: `Whisper “${settings.whisper_model}” — used when a video has no transcript`,
        action: change("Choose a different model"),
      }),
      summaryRow({
        label: "Render quality",
        value: `Quality ${settings.render_crf} · ${settings.render_preset} encoder`,
        action: change("Tune the encoder"),
      }),
      summaryRow({
        label: "Importing from a link",
        value: settings.ingest_browser_cookies
          ? `Reusing the sign-in from ${settings.ingest_browser_cookies}`
          : "Not reusing a browser sign-in",
        action: change("Choose a browser"),
      })
    ),
    h(
      "div.faint.small",
      { style: { marginTop: "14px" } },
      `Workspace: ${health?.workspace || "—"}`
    )
  );
}

// --- dependencies ------------------------------------------------------------
function dependenciesCard(setup, jobPanel, ctx) {
  const missing = setup.components.filter((component) => !component.installed);

  const rows = setup.components.map((component) =>
    h(
      "div",
      { style: { padding: "12px 0", borderBottom: "1px solid var(--border-soft)" } },
      h(
        "div.row-between",
        h(
          "div",
          h(
            "div.row",
            h("strong", component.label),
            component.installed
              ? h("span.pill.pill-good", h("i.dot"), "Installed")
              : h("span.pill.pill-warn", h("i.dot"), `~${component.approx_mb} MB download`)
          ),
          h("div.muted.small", { style: { marginTop: "3px" } }, component.description)
        ),
        component.installed
          ? null
          : h(
              "button.btn.btn-sm",
              {
                onclick: () =>
                  jobPanel.run(api.provision(component.key), {
                    title: `Installing ${component.label}`,
                    onDone: () => {
                      toast(`${component.label} installed.`, "ok");
                      ctx.refresh();
                    },
                  }),
              },
              "Install"
            )
      ),
      h("div.faint.small.mono", { style: { marginTop: "5px" } }, component.location),
      component.notes ? h("div.faint.small", { style: { marginTop: "4px" } }, component.notes) : null
    )
  );

  return h(
    "div.card",
    h(
      "div.row-between",
      h("h2", { style: { margin: 0 } }, "Dependencies"),
      missing.length
        ? h(
            "button.btn.btn-primary.btn-sm",
            {
              onclick: () =>
                jobPanel.run(api.provision("all"), {
                  title: "Installing dependencies",
                  onDone: () => {
                    toast("All dependencies installed.", "ok");
                    ctx.refresh();
                  },
                }),
            },
            `Install all (${missing.length})`
          )
        : h("span.pill.pill-good", h("i.dot"), "Everything is installed")
    ),
    h("div", { style: { marginTop: "8px" } }, rows)
  );
}

// --- language model ----------------------------------------------------------
function modelCard(setup, settings, ctx) {
  const statusByKey = Object.fromEntries(setup.providers.map((p) => [p.key, p]));
  const active = settings.llm_provider;
  const usingOther = !COPILOT.some((option) => option.key === active);

  const body = h("div", { style: { marginTop: "14px" } });

  function drawBody() {
    if (usingOther) mount(body, otherProviderPanel(setup, settings, statusByKey, ctx));
    else if (active === "copilot_cli") mount(body, cliPanel(statusByKey[active], settings, statusByKey, ctx));
    else mount(body, copilotPanel(statusByKey[active], settings, ctx));
  }

  const options = [
    ...COPILOT.map((option) => ({
      key: option.key,
      label: option.label,
      blurb: option.blurb,
      recommended: option.recommended,
      status: statusByKey[option.key],
    })),
    {
      key: "__other",
      label: "Another provider",
      blurb:
        "OpenAI, Azure OpenAI, Anthropic Claude, Google Gemini, OpenRouter, a local " +
        "Ollama server, or any internal OpenAI-compatible gateway.",
      status: usingOther ? statusByKey[active] : null,
    },
  ];

  const cards = options.map((option) => {
    const selected = option.key === "__other" ? usingOther : option.key === active;
    return h(
      "button",
      {
        class: `choice${selected ? " selected" : ""}`,
        onclick: async () => {
          if (selected) return;
          // Picking "another provider" opens the panel; the actual provider is
          // chosen there, so nothing is saved until a preset is selected.
          if (option.key === "__other") {
            mount(body, otherProviderPanel(setup, settings, statusByKey, ctx, { pending: true }));
            for (const card of cards) card.classList.remove("selected");
            option.node.classList.add("selected");
            return;
          }
          await api.putSettings({ llm_provider: option.key });
          toast(`Now using ${option.label}.`, "ok");
          ctx.refresh();
        },
      },
      h(
        "div.row",
        h("strong", option.label),
        option.recommended ? h("span.tag", "recommended") : null,
        option.status
          ? option.status.available
            ? h("span.pill.pill-good", h("i.dot"), "Ready")
            : h("span.pill.pill-muted", h("i.dot"), "Not available")
          : null
      ),
      h("div.muted.small", { style: { marginTop: "5px" } }, option.blurb)
    );
  });
  options.forEach((option, index) => (option.node = cards[index]));

  drawBody();

  return h(
    "div.card",
    h("h2", "Language model"),
    h(
      "div.muted.small",
      { style: { marginBottom: "12px" } },
      "The transcript is the only thing sent to the model — never the video or the audio."
    ),
    h("div.choices", cards),
    body
  );
}

function copilotPanel(status, settings, ctx) {
  if (!status) return h("div");

  const models = [...new Set(status.models)];
  const model = h(
    "select",
    h("option", { value: "", selected: !settings.llm_model }, "Automatic"),
    models.map((name) =>
      h("option", { value: name, selected: name === settings.llm_model }, name)
    )
  );
  const effort = effortSelect(settings.vscode_reasoning_effort);
  const context = contextTokenSelect(settings.vscode_context_window_tokens);

  return h(
    "div.subcard",
    h("div.small", status.detail),
    status.setup_hint
      ? h("div.faint.small", { style: { marginTop: "6px" } }, status.setup_hint)
      : null,
    h(
      "div.model-controls",
      h("label.field", h("span", "Model"), model),
      h("label.field", h("span", "Thinking effort"), effort),
      h("label.field", h("span", "Context window"), context)
    ),
    h(
      "button.btn.btn-sm.btn-primary",
      {
        style: { marginTop: "12px" },
        onclick: async () => {
          await api.putSettings({
            llm_model: model.value,
            vscode_reasoning_effort: effort.value,
            vscode_context_window_tokens: Number(context.value),
          });
          toast("VS Code model settings saved.", "ok");
          ctx.refresh();
        },
      },
      "Save model settings"
    )
  );
}

// The CLI has no way to list the models it accepts: there is no `models`
// subcommand, and an unknown name only produces "not available" with no
// alternatives. So the field is free text, and the suggestions are borrowed from
// the VS Code bridge, which reads the same Copilot account. If the bridge has
// never run there is a small fallback list from the CLI's own documentation.
const CLI_MODEL_FALLBACK = ["claude-sonnet-5", "claude-haiku-5", "gpt-5.4"];

function cliPanel(status, settings, statusByKey, ctx) {
  if (!status) return h("div");

  const current = settings.copilot_cli_model || "";
  const bridgeModels = statusByKey.vscode?.models || [];
  const suggestions = [...new Set([
    "auto",
    ...(current ? [current] : []),
    ...(bridgeModels.length ? bridgeModels : CLI_MODEL_FALLBACK),
  ])];
  const model = h(
    "select",
    suggestions.map((name) =>
      h("option", { value: name, selected: name === (current || "auto") }, name)
    )
  );
  const effort = effortSelect(settings.copilot_cli_reasoning_effort);
  const context = h(
    "select",
    h(
      "option",
      { value: "default", selected: settings.copilot_cli_context_window === "default" },
      "Default"
    ),
    h(
      "option",
      { value: "long_context", selected: settings.copilot_cli_context_window === "long_context" },
      "Long context"
    )
  );

  return h(
    "div.subcard",
    h("div.small", status.detail),
    status.setup_hint
      ? h("div.faint.small", { style: { marginTop: "6px" } }, status.setup_hint)
      : null,
    h(
      "div.model-controls",
      h("label.field", h("span", "Model"), model),
      h("label.field", h("span", "Thinking effort"), effort),
      h("label.field", h("span", "Context window"), context)
    ),
    h(
      "button.btn.btn-sm.btn-primary",
      {
        style: { marginTop: "12px" },
        onclick: async () => {
          await api.putSettings({
            copilot_cli_model: model.value,
            copilot_cli_reasoning_effort: effort.value,
            copilot_cli_context_window: context.value,
          });
          toast("Copilot CLI model settings saved.", "ok");
          ctx.refresh();
        },
      },
      "Save model settings"
    )
  );
}

function effortSelect(current) {
  const options = [
    ["", "Automatic"],
    ["none", "None"],
    ["minimal", "Minimal"],
    ["low", "Low"],
    ["medium", "Medium"],
    ["high", "High"],
    ["xhigh", "Extra high"],
    ["max", "Maximum"],
  ];
  return h(
    "select",
    options.map(([value, label]) =>
      h("option", { value, selected: value === (current || "") }, label)
    )
  );
}

function contextTokenSelect(current) {
  const options = [
    [0, "Model default"],
    [8192, "8K tokens"],
    [16384, "16K tokens"],
    [32768, "32K tokens"],
    [65536, "64K tokens"],
    [128000, "128K tokens"],
    [256000, "256K tokens"],
    [1000000, "1M tokens"],
  ];
  return h(
    "select",
    options.map(([value, label]) =>
      h("option", { value: String(value), selected: value === Number(current || 0) }, label)
    )
  );
}

function otherProviderPanel(setup, settings, statusByKey, ctx, { pending = false } = {}) {  const presets = settings.presets || [];
  const currentPreset =
    settings.llm_provider === "anthropic" ? "anthropic" : settings.llm_preset || "custom";

  const select = h(
    "select",
    { onchange: (event) => choose(event.target.value) },
    pending ? h("option", { value: "" }, "Choose a provider…") : null,
    presets.map((preset) =>
      h(
        "option",
        { value: preset.key, selected: !pending && preset.key === currentPreset },
        preset.label
      )
    )
  );

  const detail = h("div", { style: { marginTop: "12px" } });

  async function choose(key) {
    if (!key) return;
    await api.putSettings({ llm_preset: key });
    toast("Provider updated. Set the API key if you have not already.", "ok");
    ctx.refresh();
  }

  function drawDetail() {
    const preset = presets.find((p) => p.key === currentPreset);
    if (pending || !preset) {
      mount(
        detail,
        h("div.faint.small", "Pick a provider to see what it needs.")
      );
      return;
    }

    const isAnthropic = preset.provider === "anthropic";
    const status = statusByKey[preset.provider];
    const keyEnv = isAnthropic ? settings.anthropic_api_key_env : settings.openai_api_key_env;
    const keyPresent = (settings.keys_present || {})[keyEnv];

    const baseUrl = h("input", {
      type: "url",
      value: isAnthropic ? settings.anthropic_base_url : settings.openai_base_url || "",
      placeholder: preset.needs_base_url
        ? "https://<resource>.openai.azure.com/openai/v1"
        : preset.base_url,
    });
    const model = h("input", {
      type: "text",
      value: isAnthropic ? settings.anthropic_model : settings.openai_model || "",
    });

    mount(
      detail,
      preset.note ? h("div.muted.small", preset.note) : null,
      status
        ? h(
            "div",
            { style: { marginTop: "8px" } },
            status.available
              ? h("span.pill.pill-good", h("i.dot"), status.detail)
              : h("span.pill.pill-muted", h("i.dot"), status.detail)
          )
        : null,
      h(
        "div.grid.grid-2",
        { style: { marginTop: "12px" } },
        h(
          "label.field",
          h("span", preset.needs_base_url ? "Endpoint URL (required)" : "Endpoint URL"),
          baseUrl
        ),
        h(
          "label.field",
          h("span", isAnthropic ? "Model" : "Model / deployment name"),
          model,
          preset.suggested_models?.length
            ? h(
                "div.row",
                { style: { marginTop: "6px" } },
                preset.suggested_models.map((name) =>
                  h(
                    "button.btn.btn-sm.btn-ghost",
                    { onclick: () => (model.value = name) },
                    name
                  )
                )
              )
            : null
        )
      ),
      h(
        "div",
        { style: { marginTop: "12px" } },
        h(
          "div.row",
          h(
            "button.btn.btn-sm.btn-primary",
            {
              onclick: async () => {
                await api.putSettings(
                  isAnthropic
                    ? {
                        anthropic_base_url: baseUrl.value.trim() || preset.base_url,
                        anthropic_model: model.value.trim(),
                      }
                    : {
                        openai_base_url: baseUrl.value.trim() || preset.base_url,
                        openai_model: model.value.trim(),
                      }
                );
                toast("Saved.", "ok");
                ctx.refresh();
              },
            },
            "Save"
          ),
          keyPresent
            ? h("span.pill.pill-good", h("i.dot"), "API key found")
            : h("span.pill.pill-warn", h("i.dot"), "API key not set")
        ),
        h(
          "div.faint.small",
          { style: { marginTop: "8px" } },
          "The API key is read from the environment variable ",
          h("code", keyEnv),
          " so it never lands in a config file. Set it before starting ClipDesk:"
        ),
        h(
          "pre.snippet",
          `$env:${keyEnv} = "<your key>"\n.\\run.ps1`
        )
      )
    );
  }

  drawDetail();

  return h(
    "div.subcard",
    h("label.field", h("span", "Provider"), select),
    detail
  );
}

// --- downloading from links --------------------------------------------------
function downloadsCard(settings, ctx) {
  const browsers = settings.browsers || [];
  const select = h(
    "select",
    h("option", { value: "" , selected: !settings.ingest_browser_cookies }, "Never"),
    browsers.map((name) =>
      h(
        "option",
        { value: name, selected: name === settings.ingest_browser_cookies },
        name.charAt(0).toUpperCase() + name.slice(1)
      )
    )
  );

  return h(
    "div.card",
    h("h2", "Importing from a link"),
    h(
      "p.muted.small",
      "SharePoint, Stream and OneDrive only hand over a recording to someone signed in. " +
        "ClipDesk can reuse the sign-in you already have in a browser, so you do not have " +
        "to download and re-upload the file."
    ),
    h(
      "div.grid.grid-2",
      h(
        "label.field",
        h("span", "Reuse the sign-in from"),
        select,
        h(
          "div.faint.small",
          { style: { marginTop: "6px" } },
          "The browser must be fully closed when you import — it locks its cookie " +
            "store while running. Cookies are read locally and only sent to the site " +
            "you are downloading from."
        )
      )
    ),
    h(
      "button.btn.btn-sm.btn-primary",
      {
        style: { marginTop: "12px" },
        onclick: async () => {
          await api.putSettings({ ingest_browser_cookies: select.value });
          toast("Saved.", "ok");
          ctx.refresh();
        },
      },
      "Save"
    )
  );
}

// --- preferences -------------------------------------------------------------
/** A labelled control with a plain-English explanation underneath. */
function setting(label, control, explanation, extra) {
  return h(
    "div",
    { style: { padding: "14px 0", borderBottom: "1px solid var(--border-soft)" } },
    h("div.row-between", h("strong.small", label), extra || null),
    h("div", { style: { margin: "8px 0 6px", maxWidth: "420px" } }, control),
    h("div.muted.small", explanation)
  );
}

function preferencesCard(settings, ctx) {
  const whisper = h(
    "select",
    ...["tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "large-v3"].map(
      (name) => h("option", { value: name, selected: name === settings.whisper_model }, name)
    )
  );

  const thresholdValue = h("span.mono.tag");
  const thresholdMeaning = h("div.faint.small");
  const threshold = h("input", {
    type: "range",
    min: "0",
    max: "0.9",
    step: "0.01",
    value: String(settings.keep_importance_threshold),
    oninput: () => describeThreshold(),
  });

  function describeThreshold() {
    const value = Number(threshold.value);
    thresholdValue.textContent = value.toFixed(2);
    thresholdMeaning.textContent =
      value < 0.25
        ? "Currently: keeps almost everything. The cut will be barely shorter than the original."
        : value < 0.45
        ? "Currently: a balanced cut — removes filler and dead air, keeps the discussion."
        : value < 0.65
        ? "Currently: aggressive — keeps only the clearly substantial parts. Expect a much shorter video."
        : "Currently: very aggressive — only the strongest moments survive. Check the result before sharing it.";
  }
  describeThreshold();

  const chapters = numberField({ value: settings.notes_chapters_per_file, min: 1, max: 50 });
  const crf = numberField({ value: settings.render_crf, min: 0, max: 51 });
  const preset = h(
    "select",
    ...["ultrafast", "veryfast", "faster", "medium", "slow"].map((name) =>
      h("option", { value: name, selected: name === settings.render_preset }, name)
    )
  );

  return h(
    "div.card",
    h("h2", "Preferences"),
    h(
      "p.muted.small",
      { style: { marginTop: 0 } },
      "Defaults for new work. Most screens let you override these per run."
    ),

    setting(
      "Speech-to-text model",
      whisper,
      "Only used when you upload a video without a transcript. Bigger models are more " +
        "accurate but slower, and each one is a separate download. 'base' is the right " +
        "starting point on a laptop: roughly 5–10× faster than real time on a CPU. " +
        "Move to 'small' if names and technical terms come out wrong; drop to 'tiny' " +
        "if transcription is too slow."
    ),

    setting(
      "How much the clean cut keeps",
      h("div", threshold, thresholdMeaning),
      "During analysis every moment is scored from 0 to 1 on how much a viewer would " +
        "lose if it were cut — based on how much information it carries, how much of it " +
        "is filler, and whether it is on the video's main topic. This slider is the " +
        "cut-off: anything scoring below it is removed. Lower keeps more, higher removes " +
        "more.",
      thresholdValue
    ),

    setting(
      "Chapters per notes file",
      chapters,
      "Long recordings are split into several notes documents rather than one unreadable " +
        "wall of text. This is how many chapters go into each file — raise it for fewer, " +
        "longer documents."
    ),

    setting(
      "Video quality when rendering",
      h("div.row", crf, preset),
      "The first number is quality: lower means better and a bigger file (18 is visually " +
        "lossless, 23 is a good default, above 28 starts to look soft). The second is how " +
        "hard the encoder works: 'ultrafast' finishes soonest at a larger file size, " +
        "'slow' takes longer for a smaller one. On a laptop, 'veryfast' is the sensible " +
        "trade."
    ),

    h(
      "div.row",
      { style: { marginTop: "16px" } },
      h(
        "button.btn.btn-primary",
        {
          onclick: async () => {
            await api.putSettings({
              whisper_model: whisper.value,
              keep_importance_threshold: Number(threshold.value),
              notes_chapters_per_file: chapters.value,
              render_crf: crf.value,
              render_preset: preset.value,
            });
            toast("Preferences saved.", "ok");
            ctx.refresh();
          },
        },
        "Save preferences"
      ),
      h("span.faint.small", "Written to config/local.yaml.")
    )
  );
}
