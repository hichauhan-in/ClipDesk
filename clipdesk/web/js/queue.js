// Line work up instead of waiting at each step.
//
// Rendering takes minutes, so the useful unit of work is rarely one action. A
// step added here names the file it will produce, which is what lets a later
// step point at a file that does not exist yet: by the time it runs, it does.

import { api } from "./api.js";
import { confirmAction, h, mount, toast } from "./dom.js";

const KIND_ICON = {
  notes: "NOTE",
  article: "DOC",
  cleanup: "CUT",
  clips: "CLIP",
  "clips-find": "FIND",
  "highlights-find": "FIND",
  intro: "INTRO",
  bookend: "JOIN",
  "prompt-edit": "EDIT",
  export: "SAVE",
};

/**
 * One queue per project view. Panes read `produces` to offer files the queue
 * will create, and call `add` to put work in it.
 */
export function createQueue(projectId, { onChange } = {}) {
  let steps = [];
  const listeners = new Set();
  if (onChange) listeners.add(onChange);

  function announce() {
    for (const listener of listeners) listener(steps);
  }

  function apply(payload) {
    steps = payload?.steps || [];
    announce();
    return steps;
  }

  return {
    get steps() {
      return steps;
    },
    /** Files the queue will write, in the order they will appear. */
    get produces() {
      return steps.map((step) => step.produces).filter(Boolean);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async refresh() {
      return apply(await api.queue(projectId).catch(() => ({ steps: [] })));
    },
    /**
     * Send an action with `queue: true`. The endpoint builds the same work it
     * would have run, and hands it back rather than starting it.
     */
    async add(call, body) {
      const result = await call({ ...body, queue: true });
      await this.refresh();
      toast(
        result.step?.produces
          ? `Queued — will write ${result.step.produces}.`
          : "Added to the queue.",
        "ok"
      );
      return result;
    },
    async remove(stepId) {
      return apply(await api.removeQueueStep(projectId, stepId));
    },
    async move(stepId, offset) {
      return apply(await api.moveQueueStep(projectId, stepId, offset));
    },
    async clear() {
      return apply(await api.clearQueue(projectId));
    },
    async run() {
      const result = await api.runQueue(projectId);
      await this.refresh();
      return result;
    },
  };
}

/**
 * A run button with "Add to queue" beside it. Queuing is the secondary action
 * everywhere: the common case is still to run one thing and watch it.
 */
export function runOrQueue({ label, onRun, onQueue, disabled = false, primary = true }) {
  const run = h(
    `button.btn${primary ? ".btn-primary" : ""}`,
    { disabled, onclick: onRun },
    label
  );
  const queue = h(
    "button.btn.btn-ghost.btn-sm",
    {
      disabled,
      title: "Add this to the queue and keep setting things up.",
      onclick: async () => {
        queue.disabled = true;
        try {
          await onQueue();
        } catch (error) {
          toast(error.message, "err");
        } finally {
          queue.disabled = false;
        }
      },
    },
    "Add to queue"
  );
  const row = h("div.row.run-row", run, queue);
  row.run = run;
  row.queue = queue;
  return row;
}

/** The pending list, with reordering, removal, and one button to run it all. */
export function queuePanel(queue, { onRan } = {}) {
  const el = h("div.queue-panel");

  function draw(steps) {
    if (!steps.length) {
      el.style.display = "none";
      mount(el);
      return;
    }
    el.style.display = "";

    const rows = steps.map((step, index) =>
      h(
        "div.queue-step",
        h("span.queue-index", String(index + 1)),
        h("span.tag.queue-kind", KIND_ICON[step.kind] || step.kind.toUpperCase()),
        h(
          "div.queue-text",
          h("strong.small", step.label),
          step.produces ? h("div.faint.small", `writes ${step.produces}`) : null
        ),
        h(
          "div.row.queue-controls",
          h(
            "button.icon-btn",
            {
              title: "Run this sooner",
              disabled: index === 0,
              onclick: () => queue.move(step.id, -1).catch((e) => toast(e.message, "err")),
            },
            "▲"
          ),
          h(
            "button.icon-btn",
            {
              title: "Run this later",
              disabled: index === steps.length - 1,
              onclick: () => queue.move(step.id, 1).catch((e) => toast(e.message, "err")),
            },
            "▼"
          ),
          h(
            "button.icon-btn",
            {
              title: "Remove this step",
              onclick: () => queue.remove(step.id).catch((e) => toast(e.message, "err")),
            },
            "✕"
          )
        )
      )
    );

    const runButton = h(
      "button.btn.btn-primary.btn-sm",
      {
        onclick: async () => {
          runButton.disabled = true;
          try {
            const result = await queue.run();
            toast(`Running ${result.job_ids.length} step(s) in order.`, "ok");
            onRan?.(result);
          } catch (error) {
            toast(error.message, "err");
          } finally {
            runButton.disabled = false;
          }
        },
      },
      `Run ${steps.length} step${steps.length === 1 ? "" : "s"}`
    );

    mount(
      el,
      h(
        "div.row-between.queue-head",
        h(
          "div.row",
          { style: { gap: "8px" } },
          h("strong.small", "Queued"),
          h("span.tag", `${steps.length}`)
        ),
        h(
          "div.row",
          { style: { gap: "8px" } },
          h(
            "button.btn.btn-ghost.btn-sm",
            {
              onclick: async () => {
                const ok = await confirmAction({
                  title: "Empty the queue?",
                  message: "Nothing has run yet, so nothing is lost.",
                  confirmLabel: "Empty it",
                });
                if (ok) queue.clear().catch((error) => toast(error.message, "err"));
              },
            },
            "Empty"
          ),
          runButton
        )
      ),
      h("div.stack.queue-steps", rows),
      h(
        "div.faint.small",
        "Each step waits for the one above it. Running anything else now puts these first."
      )
    );
  }

  queue.subscribe(draw);
  draw(queue.steps);
  return el;
}

/**
 * Options for a file picker, with anything the queue will write appended.
 * A queued name is real enough to choose: it will exist before it is read.
 */
export function withQueued(existing, queue, { label = "queued" } = {}) {
  const names = new Set(existing.map(([value]) => value));
  const pending = queue.produces
    .filter((name) => !names.has(name))
    .map((name) => [name, `${name} — ${label}`]);
  return [...existing, ...pending];
}
