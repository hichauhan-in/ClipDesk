// The "your model is not connected" alert.
//
// This is the one problem a new user is guaranteed to hit — the bridge extension
// is installed by the launcher, but VS Code will not load it until the window
// reloads. A tooltip on a status pill is far too quiet for something that blocks
// every AI feature in the app, so it gets a modal with the actual keystrokes.
//
// It re-checks on its own and closes when the bridge comes up, so the user never
// has to wonder whether it worked.

import { api } from "./api.js";
import { h, mount } from "./dom.js";

let host = null;
let timer = null;
let dismissed = false;

function key(text) {
  return h("kbd", text);
}

/** Work out which of the three failure states this is, and what to say. */
function diagnose(health) {
  const bridge = health.bridge || {};

  if (!bridge.extension_installed) {
    return {
      title: "Connect ClipDesk to Copilot",
      lead:
        "The VS Code extension that lets ClipDesk use your Copilot seat is not " +
        "installed yet.",
      steps: [
        h(
          "li",
          "Close ClipDesk, then double-click ",
          h("code", "Start ClipDesk.cmd"),
          " again — it installs the extension for you."
        ),
        h("li", "Or run ", h("code", ".\\install-bridge.ps1"), " from the ClipDesk folder."),
      ],
    };
  }

  if (!bridge.handshake_present) {
    return {
      title: "One step left — reload VS Code",
      lead:
        "The Copilot bridge is installed, but VS Code has not loaded it yet. " +
        "Extensions only start when the window reloads.",
      steps: [
        h("li", "Switch to ", h("strong", "VS Code"), "."),
        h("li", "Press ", key("Ctrl"), " + ", key("Shift"), " + ", key("P"), "."),
        h("li", "Type ", h("code", "Developer: Reload Window"), " and press ", key("Enter"), "."),
        h(
          "li",
          "If VS Code is not open at all, just open it — ClipDesk needs a window " +
            "running while you use it."
        ),
      ],
    };
  }

  // The handshake file is there but the bridge did not answer: VS Code was
  // closed, or it started and then stopped.
  return {
    title: "VS Code is not responding",
    lead:
      "ClipDesk found the bridge's details but could not reach it. This usually " +
      "means VS Code was closed.",
    steps: [
      h("li", "Open ", h("strong", "VS Code"), " and leave a window running."),
      h(
        "li",
        "Still not working? Press ",
        key("Ctrl"),
        " + ",
        key("Shift"),
        " + ",
        key("P"),
        " and run ",
        h("code", "ClipDesk Bridge: Restart Server"),
        "."
      ),
      h(
        "li",
        "If VS Code asks for permission the first time, accept it — an extension " +
          "cannot use Copilot until you do."
      ),
    ],
  };
}

function build(health, ctx, onClose) {
  const { title, lead, steps } = diagnose(health);
  const statusLine = h("div.faint.small");

  const checkButton = h(
    "button.btn.btn-primary",
    {
      onclick: async () => {
        checkButton.disabled = true;
        checkButton.textContent = "Checking…";
        statusLine.textContent = "";
        const fresh = await api.health().catch(() => null);
        if (fresh?.llm?.available) {
          onClose();
          ctx.refreshHealth();
          ctx.refresh();
          return;
        }
        checkButton.disabled = false;
        checkButton.textContent = "Check again";
        statusLine.textContent =
          "Still not connected. Give VS Code a moment after reloading, then try again.";
      },
    },
    "Check again"
  );

  return h(
    "div.modal-backdrop",
    h(
      "div.modal",
      { role: "dialog", "aria-modal": "true", "aria-label": title },
      h(
        "div.row-between",
        h("h2", { style: { margin: 0 } }, title),
        h(
          "button.btn.btn-sm.btn-ghost",
          { title: "Dismiss", "aria-label": "Dismiss", onclick: onClose },
          "✕"
        )
      ),
      h("p.muted", { style: { marginBottom: "6px" } }, lead),
      h("ol.steps", steps),
      statusLine,
      h(
        "div.row",
        { style: { marginTop: "16px" } },
        checkButton,
        h(
          "button.btn",
          {
            onclick: () => {
              onClose();
              ctx.navigate("#/settings");
            },
          },
          "Use a different model"
        ),
        h("button.btn.btn-ghost", { onclick: onClose }, "Not now")
      ),
      h(
        "div.faint.small",
        { style: { marginTop: "14px" } },
        "Transcripts, exports and the clean cut all work without a model. Notes, " +
          "chapters and clip search need one."
      )
    )
  );
}

function close() {
  host?.remove();
  host = null;
}

/**
 * Show the alert when the Copilot bridge is the chosen provider and is not
 * reachable. Does nothing for other providers — they have their own setup, and
 * Settings is the right place for that.
 */
export function updateBridgeAlert(health, ctx) {
  const needed =
    health?.llm?.key === "vscode" && !health.llm.available && !dismissed;

  if (!needed) {
    if (host) {
      close();
      clearInterval(timer);
      timer = null;
    }
    return;
  }
  if (host) return;

  host = build(health, ctx, () => {
    dismissed = true;
    close();
    clearInterval(timer);
    timer = null;
  });
  document.body.append(host);

  // Poll while it is open so a reload in VS Code clears it without the user
  // having to come back and press anything.
  timer = setInterval(async () => {
    const fresh = await api.health().catch(() => null);
    if (fresh?.llm?.available) {
      close();
      clearInterval(timer);
      timer = null;
      // Update the indicator now rather than waiting for its own slower poll,
      // which would otherwise still read "No model" after the alert vanished.
      ctx.refreshHealth();
      ctx.refresh();
    }
  }, 4000);
}

/** Let the user re-open it after dismissing, from the status indicator. */
export function resetBridgeAlert() {
  dismissed = false;
}
