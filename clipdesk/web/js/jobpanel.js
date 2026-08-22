// The live job panel: a progress bar, a stage label and a scrolling log.

import { api, followJob } from "./api.js";
import { h, mount, toast } from "./dom.js";
import { refreshJobs } from "./jobs.js";

const LINE_CLASS = {
  warning: "ln-warn",
  error: "ln-err",
  done: "ln-ok",
  stage_end: "ln-ok",
  log: "ln-dim",
};

export function createJobPanel() {
  const label = h("div.small.muted", "Idle");
  const fill = h("i", { style: { width: "0%" } });
  const bar = h("div.bar", fill);
  const console_ = h("div.console.small");
  const cancelButton = h(
    "button.btn.btn-sm.btn-ghost.btn-danger",
    { type: "button", style: { display: "none" } },
    "Cancel"
  );
  const wrapper = h(
    "div.stack",
    { style: { display: "none" } },
    h("div.row-between", label, cancelButton),
    bar,
    h("details", h("summary", { class: "small" }, "Details"), console_)
  );

  let stop = null;

  function line(text, kind) {
    const el = h("div", { class: LINE_CLASS[kind] || "" }, text);
    console_.append(el);
    while (console_.childElementCount > 400) console_.firstElementChild.remove();
    console_.scrollTop = console_.scrollHeight;
  }

  function setProgress(fraction) {
    if (fraction === null || fraction === undefined) {
      bar.classList.add("indeterminate");
      fill.style.width = "35%";
    } else {
      bar.classList.remove("indeterminate");
      fill.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;
    }
  }

  /**
   * @param {Promise<{job_id:string}>} startPromise a call that starts the job
   * @param {{title?:string, onDone?:Function, onError?:Function}} options
   */
  async function run(startPromise, { title = "Working", onDone, onError, onEvent } = {}) {
    cancel();
    wrapper.style.display = "";
    console_.replaceChildren();
    label.textContent = title;
    setProgress(null);

    let started;
    try {
      started = await startPromise;
    } catch (error) {
      wrapper.style.display = "none";
      toast(error.message, "err");
      onError?.(error);
      return;
    }

    refreshJobs();
    attach(started.job_id, { onDone, onError, onEvent });
    return started;
  }

  /**
   * Reattach to a job that is already running, which is what makes leaving a tab
   * safe: the work never belonged to the view, only the progress bar did.
   */
  function follow(jobId, { title = "Working", onDone, onError, onEvent } = {}) {
    cancel();
    wrapper.style.display = "";
    console_.replaceChildren();
    label.textContent = title;
    setProgress(null);
    attach(jobId, { onDone, onError, onEvent });
  }

  function attach(jobId, { onDone, onError, onEvent } = {}) {
    cancelButton.style.display = "";
    cancelButton.disabled = false;
    cancelButton.textContent = "Cancel";
    cancelButton.onclick = async () => {
      cancelButton.disabled = true;
      cancelButton.textContent = "Cancelling…";
      try {
        const result = await api.cancelJob(jobId);
        if (!result.cancelled) throw new Error("This job can no longer be cancelled.");
      } catch (error) {
        toast(error.message, "err");
        cancelButton.disabled = false;
        cancelButton.textContent = "Cancel";
      }
    };
    stop = followJob(jobId, {
      onEvent(event) {
        onEvent?.(event);
        if (event.message) {
          const stage = event.stage ? `[${event.stage}] ` : "";
          line(`${stage}${event.message}`, event.type);
        }
        if (event.type === "progress" || event.type === "stage_start") {
          if (event.message) label.textContent = event.message;
          setProgress(event.fraction);
        }
        if (event.type === "stage_end") setProgress(1);
        if (event.type === "warning") toast(event.message, "");
      },
      onDone(result) {
        stop = null;
        cancelButton.style.display = "none";
        setProgress(1);
        label.textContent = "Finished";
        refreshJobs();
        onDone?.(result);
      },
      onError(message) {
        stop = null;
        cancelButton.style.display = "none";
        bar.classList.remove("indeterminate");
        fill.style.width = "100%";
        fill.style.background = "var(--bad)";
        label.textContent = "Failed";
        line(message, "error");
        // The job store records this with its own context; a second copy would
        // just be the same failure twice.
        toast(message, "err", { record: false });
        refreshJobs();
        onError?.(new Error(message));
      },
      onCancelled() {
        stop = null;
        cancelButton.style.display = "none";
        bar.classList.remove("indeterminate");
        fill.style.width = "100%";
        fill.style.background = "var(--warn)";
        label.textContent = "Cancelled";
        line("Cancelled by the user", "warning");
        refreshJobs();
        onError?.(new Error("Cancelled by the user"));
      },
    });
  }

  function cancel() {
    stop?.();
    stop = null;
    cancelButton.style.display = "none";
    cancelButton.disabled = false;
    cancelButton.textContent = "Cancel";
    fill.style.background = "";
  }

  function hide() {
    cancel();
    wrapper.style.display = "none";
  }

  return {
    el: wrapper,
    run,
    follow,
    cancel,
    hide,
    get busy() {
      return stop !== null;
    },
  };
}
