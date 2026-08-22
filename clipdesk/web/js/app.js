// App shell: sidebar, hash routing, and the global health indicator.

import { api } from "./api.js";
import { resetBridgeAlert, updateBridgeAlert } from "./bridgealert.js";
import { confirmAction, h, loadingView, mount, toast } from "./dom.js";
import { binIcon, pencilIcon, powerIcon } from "./icons.js";
import { onJobSettled, startJobStore } from "./jobs.js";
import { createGlobalJobsBadge } from "./jobschip.js";
import { createNotificationCentre } from "./notificationcentre.js";
import { notify } from "./notifications.js";
import { renderLibrary } from "./views/library.js";
import { renderProject } from "./views/project.js";
import { renderSettings } from "./views/settings.js";

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const statusPill = document.getElementById("statusPill");
let routeGeneration = 0;
//: The route last drawn, so a re-route onto the same page is treated as a refresh.
let lastRouteKey = null;

const ctx = {
  navigate(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
  refresh() {
    route();
  },
  refreshHealth() {
    return refreshHealth();
  },
};

function parseRoute() {
  // Read from location directly rather than a cached copy: views use
  // replaceState to remember which tab is open, and refresh() must see that.
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, queryString] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  return { parts, params: new URLSearchParams(queryString || "") };
}

async function route() {
  const generation = ++routeGeneration;
  const { parts, params } = parseRoute();
  // Finishing a job re-routes the page the user is already on. That is a
  // refresh, not a navigation: showing the skeleton and scrolling to the top
  // would throw them out of whatever they were reading.
  const key = `${parts.join("/")}?${params.get("tab") || ""}`;
  const sameRoute = key === lastRouteKey;
  lastRouteKey = key;
  const keptScroll = sameRoute ? { main: main.scrollTop, page: window.scrollY } : null;

  if (!sameRoute) {
    mount(
      main,
      loadingView(
        parts[0] === "settings"
          ? "Checking what is installed\u2026"
          : parts[0] === "project"
            ? "Opening the project\u2026"
            : "Loading\u2026",
        { cards: parts[0] === "settings" ? 4 : 2 }
      )
    );
  }
  drawNav(parts, generation);
  const staged = document.createDocumentFragment();
  const routeCtx = {
    ...ctx,
    isCurrent: () => generation === routeGeneration,
  };

  try {
    if (parts[0] === "settings") await renderSettings(staged, routeCtx);
    else if (parts[0] === "project" && parts[1]) {
      await renderProject(staged, routeCtx, parts[1], params);
    } else await renderLibrary(staged, routeCtx);
  } catch (error) {
    console.error(error);
    mount(
      staged,
      h(
        "div.card",
        h("h2", "Something went wrong"),
        h("p.muted", error.message || String(error)),
        h("button.btn", { onclick: () => ctx.refresh() }, "Try again")
      )
    );
  }
  if (generation !== routeGeneration) return;
  mount(main, staged);
  if (keptScroll) {
    main.scrollTop = keptScroll.main;
    window.scrollTo(0, keptScroll.page);
  } else {
    main.scrollTop = 0;
  }
}

function navButton(label, { active, onclick }) {
  return h("button.nav-item", { "aria-current": String(active), onclick }, h("span", label));
}

async function drawNav(parts, generation = routeGeneration) {
  const active = parts[0] || "library";
  let projects = [];
  try {
    projects = await api.listProjects();
  } catch {
    /* the status indicator already reports the outage */
  }
  if (generation !== routeGeneration) return;

  mount(
    nav,
    navButton("Library", {
      active: active === "library",
      onclick: () => ctx.navigate("#/"),
    }),
    navButton("Settings", {
      active: active === "settings",
      onclick: () => ctx.navigate("#/settings"),
    }),
    projects.length ? h("div.nav-group", "Recent") : null,
    projects.slice(0, 10).map((project) => {
      const current = parts[0] === "project" && parts[1] === project.id;
      const name = project.title || project.source_filename;
      const row = h(
        "div.nav-row",
        { "aria-current": String(current) },
        h(
          "button.nav-item",
          { title: name, onclick: () => ctx.navigate(`#/project/${project.id}`) },
          h("span", name)
        ),
        h(
          "button.nav-edit",
          {
            title: `Rename ${name}`,
            "aria-label": `Rename ${name}`,
            onclick: (event) => {
              event.stopPropagation();
              beginRename();
            },
          },
          pencilIcon()
        ),
        h(
          "button.nav-del",
          {
            title: `Delete ${name}`,
            "aria-label": `Delete ${name}`,
            onclick: async (event) => {
              event.stopPropagation();
              if (!(await confirmAction(`Delete "${name}" and everything made from it?`))) return;
              try {
                await api.deleteProject(project.id);
                toast(`${name} deleted.`, "ok");
                if (current) ctx.navigate("#/");
                else ctx.refresh();
              } catch (error) {
                toast(error.message, "err");
              }
            },
          },
          binIcon()
        )
      );

      function beginRename() {
        const input = h("input.nav-rename-input", {
          type: "text",
          value: name,
          maxlength: 180,
          "aria-label": `New title for ${name}`,
        });
        let closing = false;
        const finish = async (save) => {
          if (closing) return;
          closing = true;
          const title = input.value.trim();
          if (!save) {
            drawNav(parts);
            return;
          }
          if (!title) {
            closing = false;
            toast("Project title cannot be blank.", "err");
            input.focus();
            return;
          }
          try {
            await api.renameProject(project.id, title);
            toast(`Renamed to ${title}.`, "ok");
            ctx.refresh();
          } catch (error) {
            closing = false;
            toast(error.message, "err");
            input.focus();
          }
        };
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") finish(true);
          if (event.key === "Escape") finish(false);
        });
        input.addEventListener("blur", () => finish(true));
        mount(row, input);
        input.focus();
        input.select();
      }
      return row;
    })
  );
}

async function refreshHealth() {
  try {
    const health = await api.health();
    closeButton.hidden = Boolean(health.hosted);

    const missing = health.components.filter((component) => !component.installed);
    if (!health.ffmpeg.available) {
      setStatus("pill-bad", "Setup", "ffmpeg is not installed yet", "#/settings");
    } else if (!health.llm.available) {
      setStatus("pill-warn", "No model", health.llm.detail, "#/settings");
    } else if (missing.length) {
      setStatus(
        "pill-warn",
        "Optional",
        `Not installed: ${missing.map((c) => c.label).join(", ")}`,
        "#/settings"
      );
    } else {
      setStatus("pill-good", "Ready", "Everything is set up");
    }

    updateBridgeAlert(health, ctx);
  } catch {
    setStatus("pill-bad", "Offline", "The ClipDesk server is not responding");
  }
}

function setStatus(kind, text, tooltip, href) {
  mount(statusPill, h("i.dot"), text);
  statusPill.className = `pill ${kind}`;
  statusPill.title = tooltip || text;
  statusPill.style.cursor = href ? "pointer" : "default";
  statusPill.onclick = href
    ? () => {
        // Clicking the indicator is a deliberate "tell me again", so bring the
        // instructions back even if they were dismissed earlier.
        resetBridgeAlert();
        refreshHealth();
        ctx.navigate(href);
      }
    : null;
}

window.addEventListener("hashchange", route);
window.addEventListener("unhandledrejection", (event) => {
  if (event.reason?.message) toast(event.reason.message, "err");
});

// A job that finishes while the user is on another screen has to say so, or the
// work is done and nobody knows. The toast is the nudge; the message centre is
// where it can still be read afterwards, which matters most for failures.
onJobSettled((job) => {
  const href = job.project_id && job.project_id !== "-"
    ? `#/project/${job.project_id}?tab=${job.tab || "overview"}`
    : "";

  if (job.status === "failed") {
    notify({ kind: "error", title: `${job.label} failed`, detail: job.error, href });
    toast(`${job.label} failed — see Messages.`, "err", { record: false });
    return;
  }
  if (job.status !== "done") return;

  const here = parseRoute().parts;
  const onThisProject = here[0] === "project" && here[1] === job.project_id;
  if (job.needs_choice) {
    notify({
      kind: "info",
      title: `${job.label} finished`,
      detail: "Options are waiting for you to choose from.",
      href,
    });
  }
  if (onThisProject && !job.needs_choice) return;
  toast(
    job.needs_choice
      ? `${job.label} finished — options are waiting.`
      : `${job.label} finished.`,
    "ok"
  );
});

document.getElementById("notifications")?.append(
  createNotificationCentre((href) => ctx.navigate(href))
);

document.getElementById("jobsFoot")?.append(
  createGlobalJobsBadge((job) => {
    // The counter spans every recording, so it goes to whichever one is busy.
    if (job) ctx.navigate(`#/project/${job.project_id}?tab=${job.tab || "overview"}`);
    else ctx.navigate("#/");
  })
);

const closeButton = h(
  "button.sidebar-close",
  {
    title: "Close ClipDesk and stop its localhost server",
    onclick: () => closeApplication(false),
  },
  powerIcon({ size: 14 }),
  h("span", "Close ClipDesk")
);
document.getElementById("closeApp")?.append(closeButton);

async function closeApplication(force) {
  closeButton.disabled = true;
  try {
    await api.shutdown(force);
    setStatus("pill-muted", "Closing", "ClipDesk is shutting down");
    mount(main, h("div.empty", h("h2", "ClipDesk is closed.")));
  } catch (error) {
    if (error.status === 409) {
      const confirmed = await confirmAction(
        `${error.message}\n\nClosing now will terminate that work and may leave an incomplete output. Close ClipDesk anyway?`
      );
      if (confirmed) return closeApplication(true);
    } else {
      toast(error.message, "err");
    }
    closeButton.disabled = false;
  }
}

route();
refreshHealth();
startJobStore();
setInterval(refreshHealth, 30000);
