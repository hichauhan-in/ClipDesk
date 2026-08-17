// Setup: install what is missing, and choose which model answers.

import { api } from "../api.js";
import { h, mount, numberField, toast } from "../dom.js";
import { createJobPanel } from "../jobpanel.js";

const PROVIDER_BLURB = {
  vscode:
    "Uses your Copilot seat through VS Code's Language Model API — the way Microsoft " +
    "intends extensions to use Copilot. Run install-bridge.ps1, restart VS Code, then " +
    "authorise it once from the command palette. A VS Code window must stay open.",
  copilot_cli:
    "Runs the GitHub Copilot CLI once per request. No VS Code needed, but noticeably " +
    "slower on a long video.",
  openai_compat:
    "Any endpoint that speaks the OpenAI chat-completions API — Azure OpenAI, Azure AI " +
    "Foundry, or an internal gateway. Use this if your organisation stands up a governed " +
    "endpoint.",
};

export async function renderSetup(root, ctx) {
  const [setup, settings] = await Promise.all([
    api.setup().catch(() => null),
    api.getSettings().catch(() => null),
  ]);

  if (!setup || !settings) {
    mount(root, h("div.empty", "Could not reach the ClipDesk server."));
    return;
  }

  const jobPanel = createJobPanel();

  mount(
    root,
    h(
      "div.page-head",
      h("h1", "Setup"),
      h("div.sub", "Everything ClipDesk needs lives in this folder. Nothing is installed system-wide.")
    ),
    jobPanel.el,
    componentsCard(setup, jobPanel, ctx),
    providersCard(setup, settings, ctx),
    preferencesCard(settings, ctx)
  );
}

function componentsCard(setup, jobPanel, ctx) {
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

function providersCard(setup, settings, ctx) {
  const cards = setup.providers.map((provider) => {
    const active = provider.key === setup.active_provider;
    return h(
      "div",
      {
        style: {
          padding: "14px",
          border: `1px solid ${active ? "var(--accent-dim)" : "var(--border)"}`,
          borderRadius: "var(--radius-sm)",
          background: active ? "#151d2e" : "transparent",
        },
      },
      h(
        "div.row-between",
        h(
          "div.row",
          h("strong", provider.label),
          provider.available
            ? h("span.pill.pill-good", h("i.dot"), "Reachable")
            : h("span.pill.pill-muted", h("i.dot"), "Not available")
        ),
        active
          ? h("span.tag", "in use")
          : h(
              "button.btn.btn-sm",
              {
                onclick: async () => {
                  await api.putSettings({ llm_provider: provider.key });
                  toast(`Switched to ${provider.label}.`, "ok");
                  ctx.refresh();
                },
              },
              "Use this"
            )
      ),
      h("div.muted.small", { style: { marginTop: "6px" } }, PROVIDER_BLURB[provider.key] || ""),
      h("div.small", { style: { marginTop: "8px" } }, provider.detail),
      provider.setup_hint
        ? h("div.faint.small", { style: { marginTop: "6px" } }, provider.setup_hint)
        : null,
      provider.models?.length
        ? h(
            "div",
            { style: { marginTop: "10px" } },
            h("div.faint.small", "Model"),
            h("select", {
              style: { marginTop: "5px", maxWidth: "360px" },
              onchange: async (event) => {
                const model = event.target.value;
                const body =
                  provider.key === "openai_compat"
                    ? { openai_model: model }
                    : { llm_model: model };
                await api.putSettings(body);
                toast(`Using ${model}.`, "ok");
                ctx.refresh();
              },
            }, provider.models.map((model) =>
              h("option", { value: model, selected: model === provider.active_model }, model)
            ))
          )
        : null,
      provider.key === "openai_compat"
        ? endpointFields(settings, ctx)
        : null
    );
  });

  return h(
    "div.card",
    h("h2", "Language model"),
    h(
      "div.muted.small",
      { style: { marginBottom: "12px" } },
      "The transcript is the only thing sent to the model — never the video or the audio."
    ),
    h("div.stack", cards)
  );
}

function endpointFields(settings, ctx) {
  const url = h("input", {
    type: "url",
    value: settings.openai_base_url || "",
    placeholder: "https://<resource>.openai.azure.com/openai/v1",
  });
  const model = h("input", { type: "text", value: settings.openai_model || "" });
  return h(
    "div.grid.grid-2",
    { style: { marginTop: "12px" } },
    h("label.field", h("span", "Endpoint base URL"), url),
    h("label.field", h("span", "Model / deployment name"), model),
    h(
      "div",
      h(
        "button.btn.btn-sm",
        {
          onclick: async () => {
            await api.putSettings({
              openai_base_url: url.value.trim(),
              openai_model: model.value.trim(),
            });
            toast("Endpoint saved.", "ok");
            ctx.refresh();
          },
        },
        "Save endpoint"
      ),
      h(
        "div.faint.small",
        { style: { marginTop: "6px" } },
        "The API key is read from the CLIPDESK_LLM_API_KEY environment variable so it " +
          "never lands in a config file."
      )
    )
  );
}

function preferencesCard(settings, ctx) {
  const whisper = h(
    "select",
    ...["tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "large-v3"].map(
      (name) =>
        h("option", { value: name, selected: name === settings.whisper_model }, name)
    )
  );
  const threshold = h("input", {
    type: "range",
    min: "0",
    max: "1",
    step: "0.01",
    value: String(settings.keep_importance_threshold),
  });
  const thresholdLabel = h("span.mono", settings.keep_importance_threshold.toFixed(2));
  threshold.addEventListener("input", () => {
    thresholdLabel.textContent = Number(threshold.value).toFixed(2);
  });
  const chapters = numberField({
    value: settings.notes_chapters_per_file,
    min: 1,
    max: 50,
  });
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
      "div.grid.grid-2",
      h(
        "label.field",
        h("span", "Speech-to-text model"),
        whisper,
        h(
          "div.faint.small",
          { style: { marginTop: "5px" } },
          "Smaller is faster and lighter. 'base' is the right default for a laptop; " +
            "changing this needs a new download."
        )
      ),
      h(
        "label.field",
        h("span", ["Keep threshold — ", thresholdLabel]),
        threshold,
        h(
          "div.faint.small",
          { style: { marginTop: "5px" } },
          "How important a moment has to be for the clean cut to keep it. Higher = shorter."
        )
      ),
      h("label.field", h("span", "Chapters per notes file"), chapters),
      h(
        "label.field",
        h("span", "Encode quality (CRF) and speed"),
        h("div.row", crf, preset),
        h(
          "div.faint.small",
          { style: { marginTop: "5px" } },
          "Lower CRF is better quality and a bigger file. Faster presets encode quicker."
        )
      )
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
      h("span.faint.small", "Saved to config/local.yaml.")
    )
  );
}
