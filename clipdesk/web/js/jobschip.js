// The jobs indicator: a chip that says what is running, and a panel that lists
// it. Used twice — once in the sidebar for everything, once in a project's tab
// row for that project.

import { api } from "./api.js";
import { h, mount, toast } from "./dom.js";
import { isActive, refreshJobs, subscribeJobs } from "./jobs.js";

const STATUS_PILL = {
  queued: "pill-muted",
  running: "pill-warn",
  done: "pill-good",
  failed: "pill-bad",
  cancelled: "pill-muted",
};

const STATUS_WORD = {
  queued: "Waiting",
  running: "Running",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

function shortDuration(seconds) {
  const value = Math.max(0, Math.round(seconds || 0));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

/**
 * @param {{projectId?: string, onOpenJob?: (job) => void}} options
 *   projectId scopes the chip to one recording; omit it for the global one.
 */
export function createJobsChip({ projectId = "", onOpenJob = null, align = "right" } = {}) {
  const count = h("span.jobs-chip-count");
  const word = h("span.jobs-chip-word", "Jobs");
  const spinner = h("i.spin");
  const chip = h(
    "button.jobs-chip",
    {
      type: "button",
      "aria-expanded": "false",
      onclick: () => toggle(),
    },
    spinner,
    count,
    word
  );

  const list = h("div.jobs-list");
  const panel = h(
    "div.jobs-panel",
    { class: align === "left" ? "jobs-panel-left" : "", hidden: true },
    h(
      "div.row-between",
      h("strong.small", projectId ? "This recording" : "All work"),
      h("button.btn.btn-sm", { onclick: () => refreshJobs() }, "Refresh")
    ),
    list
  );

  const wrapper = h("div.jobs-wrap", chip, panel);
  let open = false;

  function toggle(next = !open) {
    open = next;
    panel.hidden = !open;
    chip.setAttribute("aria-expanded", String(open));
    if (open) refreshJobs();
  }

  // Clicking anywhere else closes it, the way a menu should behave.
  document.addEventListener("click", (event) => {
    if (open && !wrapper.contains(event.target)) toggle(false);
  });

  function draw(jobs) {
    const mine = projectId ? jobs.filter((job) => job.project_id === projectId) : jobs;
    const running = mine.filter(isActive);
    const attention = mine.filter(
      (job) => job.status === "done" && job.needs_choice && job.result?.candidates?.length
    );
    const failed = mine.filter((job) => job.status === "failed");

    // The chip is always present. A control that vanishes when idle is one the
    // user cannot learn, and "nothing is running" is itself worth being able to
    // check.
    chip.classList.toggle("is-busy", running.length > 0);
    chip.classList.toggle("has-attention", running.length === 0 && attention.length > 0);
    chip.classList.toggle(
      "has-failed",
      running.length === 0 && attention.length === 0 && failed.length > 0
    );
    spinner.style.display = running.length ? "" : "none";

    if (running.length) {
      count.textContent = String(running.length);
      word.textContent = running.length === 1 ? "running" : "running";
    } else if (attention.length) {
      count.textContent = String(attention.length);
      word.textContent = "ready";
    } else if (failed.length) {
      count.textContent = String(failed.length);
      word.textContent = failed.length === 1 ? "failed" : "failed";
    } else {
      count.textContent = "";
      word.textContent = "Jobs";
    }
    chip.title = mine.length
      ? `${mine.length} recent job(s) — click to see them`
      : "Nothing has run yet";

    if (!mine.length) {
      mount(
        list,
        h(
          "div.faint.small",
          { style: { padding: "12px 0" } },
          projectId
            ? "Nothing has run for this recording yet. Anything you start will appear here and keep running while you move around."
            : "Nothing has run yet."
        )
      );
      return;
    }

    mount(
      list,
      mine.slice(0, 12).map((job) => row(job, onOpenJob, () => toggle(false)))
    );
  }

  const unsubscribe = subscribeJobs(draw);
  return { el: wrapper, destroy: unsubscribe, open: () => toggle(true) };
}

function row(job, onOpenJob, close) {
  const bar = h("i", {
    style: {
      width: `${Math.round((job.fraction ?? (job.status === "running" ? 0.08 : 1)) * 100)}%`,
    },
  });
  const track = h(
    "div.bar.bar-slim",
    { class: job.fraction === null && job.status === "running" ? "indeterminate" : "" },
    bar
  );

  const detail =
    job.status === "queued"
      ? job.depends_on?.length
        ? job.message || "Waiting for the step before it"
        : job.queue_position > 0
          ? `${job.queue_position} ahead of it`
          : "Next in line"
      : job.status === "failed" || job.status === "cancelled"
        ? job.error
        : job.message || "";

  const canOpen = Boolean(onOpenJob && job.tab);
  return h(
    "div.jobs-row",
    {
      class: canOpen ? "is-clickable" : "",
      onclick: canOpen
        ? () => {
            close();
            onOpenJob(job);
          }
        : null,
    },
    h(
      "div.row-between",
      h("span.small", job.label),
      h("span.pill", { class: STATUS_PILL[job.status] || "pill-muted" }, STATUS_WORD[job.status])
    ),
    job.status === "running" || job.status === "queued" ? track : null,
    h(
      "div.row-between",
      { style: { marginTop: "4px" } },
      h("span.faint.small", { style: { minWidth: "0" } }, detail),
      h(
        "span.faint.small",
        job.status === "running"
          ? shortDuration(job.elapsed_s)
          : job.status === "done"
            ? `took ${shortDuration(job.elapsed_s)}`
            : ""
      )
    ),
    job.status === "queued"
      ? h(
          "button.btn.btn-sm",
          {
            style: { marginTop: "6px" },
            onclick: async (event) => {
              event.stopPropagation();
              try {
                await api.cancelJob(job.id);
                toast("Removed from the queue.", "ok");
                refreshJobs();
              } catch (error) {
                toast(error.message, "err");
              }
            },
          },
          "Cancel"
        )
      : null
  );
}

/** The compact global counter that sits at the foot of the sidebar. */
export function createGlobalJobsBadge(onOpen) {
  const text = h("span");
  const badge = h(
    "button.jobs-badge",
    { type: "button", hidden: true, title: "Go to what is running" },
    h("i.spin"),
    text
  );
  let first = null;

  subscribeJobs((jobs) => {
    const running = jobs.filter(isActive);
    first = running[0] || null;
    badge.hidden = running.length === 0;
    text.textContent =
      running.length === 1 ? "1 job running" : `${running.length} jobs running`;
  });

  badge.addEventListener("click", () => onOpen(first));
  return badge;
}
