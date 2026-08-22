// Library: bring a recording in — by link or by upload — and pick up past work.

import { api, uploadProject, uploadProjects } from "../api.js";
import { confirmAction, debounce, h, mount, toast } from "../dom.js";
import { bytes, compactCount, duration, relativeTime } from "../format.js";
import { createJobPanel } from "../jobpanel.js";

const STATUS_PILL = {
  ready: ["pill-good", "Ready"],
  analyzing: ["pill-warn", "Analysing"],
  transcribed: ["pill-good", "Transcript ready"],
  downloading: ["pill-warn", "Downloading"],
  failed: ["pill-bad", "Failed"],
  new: ["pill-muted", "Not analysed"],
};

const TASK_LABEL = {
  analyse: "Analysis",
  notes: "Notes",
  article: "Article",
  clips: "Clip search",
};

/** What this recording has cost in model tokens, with the breakdown on hover. */
function tokenCell(tokens) {
  const total = tokens?.total_tokens || 0;
  if (!total) return h("td.nowrap.faint.small", "—");

  const lines = Object.entries(tokens.by_task || {})
    .sort((a, b) => b[1].prompt + b[1].completion - (a[1].prompt + a[1].completion))
    .map(([task, entry]) => {
      const label = TASK_LABEL[task] || task;
      return `${label}: ${(entry.prompt + entry.completion).toLocaleString()} (${entry.calls} call${
        entry.calls === 1 ? "" : "s"
      })`;
    });
  lines.push(
    `In ${tokens.prompt_tokens.toLocaleString()} · out ${tokens.completion_tokens.toLocaleString()}`
  );
  if (tokens.models?.length) lines.push(`Models: ${tokens.models.join(", ")}`);
  // The tilde is always shown, so the hover has to say which kind of
  // approximation this is rather than leaving the reader to assume the worse.
  lines.push(
    tokens.measured
      ? "Counted by the model's own tokenizer, so the token figure is exact."
      : "Estimated from character counts — this provider does not report usage."
  );

  return h(
    "td.nowrap",
    { title: lines.join("\n") },
    h("span", compactCount(total)),
    h("span.faint", { style: { marginLeft: "3px" } }, "~")
  );
}

/** What those tokens cost, in the AI credits GitHub bills Copilot usage in. */
function creditCell(tokens) {
  if (!tokens?.total_tokens) return h("td.nowrap.faint.small", "—");
  const credits = tokens.credits ?? 0;
  const unpriced = tokens.unpriced || [];
  // Everything priced but rounding to zero is real: a small model answering a
  // short prompt genuinely costs a fraction of a cent.
  if (!credits && !unpriced.length) return h("td.nowrap.faint.small", "<0.01~");

  const lines = Object.entries(tokens.by_model || {})
    .map(([model, entry]) => [model, entry, (entry.prompt + entry.completion) || 0])
    .sort((a, b) => b[2] - a[2])
    .map(([model, entry]) => `${model}: ${(entry.prompt + entry.completion).toLocaleString()} tokens`);
  lines.push(`1 credit = $0.01 — about $${(tokens.usd ?? credits * 0.01).toFixed(2)}`);
  if (unpriced.length) lines.push(`No published price for: ${unpriced.join(", ")}`);
  // The two ways this can differ from a GitHub bill, said rather than hidden.
  lines.push("Cached input is charged less and is not counted here, so this is a ceiling.");
  lines.push(
    tokens.measured
      ? "Priced from exact token counts at GitHub's published rates."
      : "Based on estimated tokens, so the cost is estimated too."
  );

  return h(
    "td.nowrap",
    { title: lines.join("\n") },
    h("span", credits.toFixed(2)),
    h("span.faint", { style: { marginLeft: "3px" } }, "~")
  );
}

const LINK_LABEL = {
  youtube: "Video link",
  sharepoint: "SharePoint / Stream",
  onedrive: "OneDrive",
  google_drive: "Google Drive",
  streaming_site: "Video platform",
  direct: "Direct file link",
  unknown: "Link",
};

export async function renderLibrary(root, ctx) {
  const projects = await api.listProjects().catch((error) => {
    toast(error.message, "err");
    return [];
  });

  const jobPanel = createJobPanel();

  mount(
    root,
    h(
      "div.page-head",
      h("h1", "Library"),
      h("div.sub", "Paste a link or upload files, then let ClipDesk work out what is in them.")
    ),
    jobPanel.el,
    importCard(ctx, jobPanel),
    h("div", { style: { marginTop: "22px" } }, projectList(projects, ctx))
  );
}

// --- bringing a recording in --------------------------------------------------
function importCard(ctx, jobPanel) {
  const linkPane = linkPanel(ctx, jobPanel, () => setMode("cloud"));
  const uploadPane = uploadPanel(ctx);
  const cloudPane = cloudPanel(ctx, jobPanel);
  uploadPane.style.display = "none";
  cloudPane.style.display = "none";

  const modes = [
    ["link", "From a link", linkPane],
    ["upload", "Upload files", uploadPane],
    ["cloud", "From OneDrive", cloudPane],
  ];

  const modeBar = h(
    "div.segmented",
    modes.map(([key, label]) =>
      h(
        "button",
        {
          "data-mode": key,
          "aria-selected": String(key === "link"),
          onclick: () => setMode(key),
        },
        label
      )
    )
  );

  function setMode(next) {
    for (const [key, , pane] of modes) pane.style.display = key === next ? "" : "none";
    for (const button of modeBar.children) {
      button.setAttribute("aria-selected", String(button.dataset.mode === next));
    }
  }

  api.signInCapability().then((capability) => {
    if (!capability.hosted) return;
    const cloudButton = modeBar.querySelector('[data-mode="cloud"]');
    if (cloudButton) {
      cloudButton.disabled = true;
      cloudButton.title = "Synced folders on the server are not exposed to hosted users.";
    }
  }).catch(() => {});

  return h(
    "div.card",
    h("div.row-between", h("h2", { style: { margin: 0 } }, "New recording"), modeBar),
    h("div", { style: { marginTop: "16px" } }, linkPane, uploadPane, cloudPane)
  );
}

// --- files this machine already has -------------------------------------------
// OneDrive is signed in and syncing already, so nothing here needs a token, a
// cookie or a pasted request. A SharePoint library added with "Add shortcut to
// OneDrive" appears as an ordinary folder.
function cloudPanel(ctx, jobPanel) {
  const container = h("div.stack");
  const crumbs = h("div.row", { style: { flexWrap: "wrap", gap: "4px" } });
  const listing = h("div");
  let root = null;
  let path = "";
  const chosen = new Map();

  // Folders get deep, and a folder someone has just shared could be anywhere, so
  // clicking down to it is not a reasonable way to find a recording.
  const searchBox = h("input", {
    type: "search",
    placeholder: "Search this OneDrive by name…",
    style: { maxWidth: "260px" },
  });
  const runSearch = debounce(async () => {
    const query = searchBox.value.trim();
    if (query.length < 2) {
      if (!query) load(path);
      return;
    }
    mount(listing, h("div.faint.small", { style: { padding: "10px 0" } }, "Searching…"));
    try {
      const found = await api.searchSource(root.id, query);
      drawEntries(found.entries, { searched: query });
    } catch (error) {
      mount(listing, h("div.subcard", h("div.small", error.message)));
    }
  }, 400);
  searchBox.addEventListener("input", runSearch);

  async function load(next = "") {
    path = next;
    searchBox.value = "";
    mount(listing, h("div.faint.small", { style: { padding: "10px 0" } }, "Reading…"));
    drawCrumbs();
    let payload;
    try {
      payload = await api.browseSource(root.id, path);
    } catch (error) {
      mount(listing, h("div.subcard", h("div.small", error.message)));
      return;
    }
    drawEntries(payload.entries);
  }

  function drawEntries(entries, { searched = "" } = {}) {
    const folders = entries.filter((entry) => entry.is_dir);
    const files = entries.filter((entry) => !entry.is_dir);
    chosen.clear();

    if (!folders.length && !files.length) {
      mount(
        listing,
        h(
          "div.empty",
          searched
            ? `Nothing matching “${searched}”. If someone just shared it, open the link ` +
              "and choose Add shortcut to My files first."
            : "Nothing here that ClipDesk can work with. Videos and audio only."
        )
      );
      return;
    }

    const batchButton = h(
      "button.btn.btn-primary.btn-sm",
      { disabled: true, onclick: () => importChosen() },
      "Import & analyse selected"
    );
    const refreshBatch = () => {
      batchButton.disabled = chosen.size === 0;
      batchButton.textContent = chosen.size
        ? `Import & analyse ${chosen.size} file${chosen.size === 1 ? "" : "s"}`
        : "Import & analyse selected";
    };

    mount(
      listing,
      searched
        ? h(
            "div.row-between",
            { style: { marginTop: "6px" } },
            h("div.faint.small", `${files.length} match(es) for “${searched}”`),
            batchButton
          )
        : h("div.row-between", h("div.faint.small", `${files.length} file(s)`), batchButton),
      h(
        "div.filelist",
        folders.map((entry) =>
          h(
            "button.filerow.is-dir",
            { onclick: () => load(entry.path) },
            h("span.fileicon", folderIcon()),
            h("span", { style: { flex: "1", minWidth: "0" } }, entry.name),
            h("span.faint.small", "›")
          )
        ),
        files.map((entry) =>
          h(
            "div.filerow",
            h("input", {
              type: "checkbox",
              "aria-label": `Select ${entry.name}`,
              onchange: (event) => {
                if (event.target.checked) chosen.set(entry.path, entry);
                else chosen.delete(entry.path);
                refreshBatch();
              },
            }),
            h("span.fileicon", "▶"),
            h(
              "span",
              { style: { flex: "1", minWidth: "0" } },
              h("div", entry.name),
              h(
                "div.faint.small.mono",
                (searched ? entry.path : bytes(entry.size_bytes)) +
                  (searched ? ` · ${bytes(entry.size_bytes)}` : "") +
                  (entry.cloud_only ? " · online only" : "")
              )
            ),
            entry.cloud_only ? h("span.tag", "cloud") : null,
            h("button.btn.btn-sm", { onclick: () => pick(entry) }, "Import")
          )
        )
      )
    );
  }

  async function importChosen() {
    const items = [...chosen.values()].map((entry) => ({
      root: root.id,
      path: entry.path,
      title: "",
    }));
    if (!items.length) return;
    try {
      const started = await api.importLocalBatch(items);
      toast(`${started.count} recordings queued for import and analysis.`, "ok");
      ctx.navigate(`#/project/${started.project_id}`);
    } catch (error) {
      toast(error.message, "err");
    }
  }

  function drawCrumbs() {
    const parts = path ? path.split("/") : [];
    mount(
      crumbs,
      h("button.crumb", { onclick: () => load("") }, root.label),
      parts.map((part, index) =>
        h(
          "span.row",
          { style: { gap: "4px" } },
          h("span.faint.small", "/"),
          h(
            "button.crumb",
            { onclick: () => load(parts.slice(0, index + 1).join("/")) },
            part
          )
        )
      )
    );
  }

  function pick(entry) {
    if (entry.cloud_only) {
      toast("Stored online only — OneDrive will fetch it first.", "");
    }
    jobPanel.run(
      api.importLocal({ root: root.id, path: entry.path, title: "" }),
      {
        title: entry.cloud_only ? `Fetching ${entry.name} from OneDrive` : `Copying ${entry.name}`,
        onDone: (result) => {
          toast("Added. Analyse it when you are ready.", "ok");
          ctx.navigate(`#/project/${result.project_id}`);
        },
      }
    );
  }

  (async () => {
    let payload;
    try {
      payload = await api.listSources();
    } catch (error) {
      mount(container, h("div.empty", error.message));
      return;
    }
    const usable = (payload.roots || []).filter((entry) => entry.available);
    if (!usable.length) {
      mount(
        container,
        h(
          "div.empty",
          "No synced OneDrive folder was found on this machine. Sign in to OneDrive, " +
            "or use a link instead."
        )
      );
      return;
    }

    root = usable[0];
    const picker =
      usable.length > 1
        ? h(
            "select",
            {
              style: { maxWidth: "280px" },
              onchange: (event) => {
                root = usable.find((entry) => entry.id === event.target.value);
                load("");
              },
            },
            usable.map((entry) => h("option", { value: entry.id }, entry.label))
          )
        : null;

    mount(
      container,
      h("div.row-between", crumbs, h("div.row", searchBox, picker)),
      listing,
      h(
        "div.faint.small",
        "Anything OneDrive has synced. To reach a file or folder someone shared " +
          "with you, open their link and choose \"Add shortcut to My files\" — it " +
          "then appears here like your own. Files stored online only are fetched " +
          "when you pick them."
      )
    );
    load("");
  })();

  return container;
}

function folderIcon() {
  return h("svg", {
    viewBox: "0 0 16 16",
    width: "13",
    height: "13",
    "aria-hidden": "true",
    html:
      '<path fill="currentColor" d="M1.75 3A1.75 1.75 0 0 0 0 4.75v6.5C0 12.22.78 13 1.75 13h12.5A1.75 1.75 0 0 0 16 11.25v-5A1.75 1.75 0 0 0 14.25 4.5H8.6L7.3 3.2A.75.75 0 0 0 6.77 3H1.75Z"/>',
  });
}

function linkPanel(ctx, jobPanel, onUseOneDrive) {
  const url = h("input", {
    type: "url",
    placeholder: "Paste a SharePoint, Stream or OneDrive link",
  });
  const title = h("input", { type: "text", placeholder: "Optional — taken from the video if blank" });
  const verdict = h("div.faint.small", { style: { minHeight: "18px" } });
  const useCookies = h("input", { type: "checkbox" });
  const browser = h(
    "select",
    ["edge", "chrome", "firefox", "brave", "vivaldi", "opera"].map((name) =>
      h("option", { value: name, selected: name === "firefox" }, name.charAt(0).toUpperCase() + name.slice(1))
    )
  );

  const sessionState = h("div.faint.small");
  const paste = h("textarea", {
    rows: 3,
    placeholder:
      "Paste here. It looks like: curl 'https://contoso.sharepoint.com/...' -H 'cookie: ...'",
    spellcheck: "false",
    style: { fontFamily: "var(--mono)", fontSize: "11px" },
  });
  const saveSession = h(
    "button.btn.btn-sm.btn-primary",
    { onclick: () => storeSession() },
    "Save this session"
  );
  const sessionPrivacy = h(
    "div.faint.small",
    { style: { marginTop: "8px" } },
    "The session is stored on this machine only, and only for that site. It expires " +
      "when your browser session does."
  );
  const fetchPrivacy = h(
    "span.faint.small",
    "Nothing is uploaded anywhere — the file is fetched straight to this machine."
  );

  const pasteBlock = h(
    "div",
    h(
      "ol.steps",
      { style: { marginTop: 0 } },
      h("li", "Open the video in your browser so it plays."),
      h("li", ["Press ", h("kbd", "F12"), ", pick the ", h("strong", "Network"), " tab, then reload."]),
      h("li", "Right-click the top request, choose Copy, then Copy as cURL."),
      h("li", "Paste it below.")
    ),
    paste,
    h("div.row", { style: { marginTop: "8px" } }, saveSession, sessionState),
    sessionPrivacy
  );

  // A tenant link always ends at a Microsoft sign-in, and ClipDesk cannot sign in
  // on the user's behalf. It can, however, open its own browser window and keep
  // the session that comes out of it -- which on a joined machine is usually one
  // click. Everything else here is a fallback for when that is blocked.
  const signInState = h("div.faint.small");
  const signInButton = h(
    "button.btn.btn-primary.btn-sm",
    { onclick: () => runSignIn() },
    "Sign in"
  );
  let signInStatus = "idle";

  function setSignInStatus(status, message = "") {
    signInStatus = status;
    signInButton.classList.remove(
      "btn-primary", "btn-success", "btn-warning", "btn-error", "btn-reauth"
    );
    const states = {
      idle: ["Sign in", "btn-primary", false],
      signing: ["Signing in…", "btn-warning", true],
      success: ["Signed in", "btn-success", false],
      failed: ["Sign in failed", "btn-error", false],
      reauth: ["Reauthenticate", "btn-reauth", false],
    };
    const [label, className, disabled] = states[status] || states.idle;
    signInButton.textContent = label;
    signInButton.classList.add(className);
    signInButton.disabled = disabled;
    signInState.textContent = message;
  }

  function needsReauthentication(message) {
    return /session has expired|sign in (?:to microsoft )?again|returned a sign-in page/i.test(
      message || ""
    );
  }

  async function runSignIn() {
    setSignInStatus("signing");
    jobPanel.run(api.startSignIn(url.value.trim()), {
      title: "Signing in",
      onDone: (result) => {
        setSignInStatus(
          "success",
          `Session ready for ${(result.hosts || []).join(", ")}.`
        );
        // The signed-in URL carries the real library path; the share token does
        // not, so keeping it is what makes the next step work.
        if (result.resolved_url) url.value = result.resolved_url;
        toast(isFolder ? "Signed in. Reading the folder…" : "Signed in.", "ok");
        inspect();
        if (isFolder) browse();
      },
      onError: (error) => {
        setSignInStatus("failed", error.message || "Microsoft sign-in did not complete.");
      },
    });
  }

  const signInDescription = h(
    "div.muted.small",
    { style: { marginTop: "6px" } },
    "ClipDesk opens its own browser window for you to sign in to Microsoft, then " +
      "remembers that session. Nothing to copy or paste. On a work machine this " +
      "is usually a single click, and it only happens once."
  );
  const signInRoute = h(
    "div",
    h("strong.small", "Sign in once, then links just work"),
    signInDescription,
    h("div.row", { style: { marginTop: "10px" } }, signInButton, signInState)
  );

  const oneDriveDescription = h(
    "div.muted.small",
    { style: { marginTop: 0 } },
    "OneDrive on this machine is already signed in. This works for someone " +
      "else's OneDrive too, as long as they have shared it with you."
  );
  const oneDriveButton = h(
    "button.btn.btn-sm",
    { style: { marginTop: "8px" }, onclick: () => onUseOneDrive?.() },
    "Browse OneDrive"
  );
  const oneDriveRoute = h(
    "div",
    oneDriveDescription,
    h(
      "ol.steps",
      { style: { marginTop: "8px" } },
      h("li", "Open the link in your browser."),
      h("li", [
        "For a folder, choose ",
        h("strong", "Add shortcut to My files"),
        ". For a single file, do the same on the folder holding it, or use ",
        h("strong", "Download"),
        ".",
      ]),
      h("li", "Give OneDrive a moment to sync, then pick it here.")
    ),
    oneDriveButton
  );

  const browserCookieRoute = h(
    "div",
    h(
      "div.row",
      { style: { marginTop: 0 } },
      h("label.check", useCookies, "Use my browser session"),
      h("div", { style: { width: "140px" } }, browser)
    ),
    h(
      "div.faint.small",
      { style: { marginTop: "6px" } },
      "Close the browser first — it locks its cookie store while running. Firefox is " +
        "the reliable option; Edge and Chrome protect their cookies from other apps."
    )
  );

  const authMethods = [
    ["signin", "Microsoft sign-in", signInRoute],
    ["onedrive", "Open in OneDrive", oneDriveRoute],
    ["paste", "Paste session", pasteBlock],
    ["cookies", "Browser cookies", browserCookieRoute],
  ];
  const authTabs = h(
    "div.tabs.auth-tabs",
    { role: "tablist", "aria-label": "Ways to open this link" },
    authMethods.map(([key, label]) =>
      h(
        "button",
        {
          role: "tab",
          "data-auth-mode": key,
          "aria-selected": String(key === "signin"),
          onclick: () => showAuthMethod(key),
        },
        label
      )
    )
  );

  function showAuthMethod(next) {
    for (const [key, , pane] of authMethods) pane.style.display = key === next ? "" : "none";
    for (const button of authTabs.children) {
      button.setAttribute("aria-selected", String(button.dataset.authMode === next));
    }
  }
  showAuthMethod("signin");

  api.signInCapability().then((capability) => {
    if (!capability.hosted) return;
    signInButton.disabled = true;
    oneDriveButton.disabled = true;
    useCookies.disabled = true;
    browser.disabled = true;
    signInDescription.textContent =
      "Interactive sign-in opens a browser on the server, so it is disabled for hosted users.";
    oneDriveDescription.textContent =
      "The server's synced OneDrive is never exposed to hosted users.";
    sessionPrivacy.textContent =
      "The session is stored only in your isolated ClipDesk account and sent only " +
      "to that site. It expires with the browser session.";
    fetchPrivacy.textContent =
      "The file is fetched into your isolated workspace on the ClipDesk server.";
    for (const key of ["signin", "onedrive", "cookies"]) {
      const tab = authTabs.querySelector(`[data-auth-mode="${key}"]`);
      if (tab) tab.disabled = true;
    }
    showAuthMethod("paste");
  }).catch(() => {});

  const cookieRow = h(
    "div.subcard.auth-methods",
    { style: { display: "none", marginTop: "10px" } },
    authTabs,
    authMethods.map(([, , pane]) => pane)
  );

  async function storeSession() {
    const text = paste.value.trim();
    if (!text) {
      toast("Paste the copied request first.", "err");
      return;
    }
    saveSession.disabled = true;
    try {
      const saved = await api.saveSession(text, url.value.trim());
      sessionState.textContent = `Saved for ${saved.host} (${saved.cookies} cookies).`;
      paste.value = "";
      toast(`Session saved for ${saved.host}.`, "ok");
    } catch (error) {
      toast(error.message, "err");
    } finally {
      saveSession.disabled = false;
    }
  }

  const importButton = h(
    "button.btn.btn-primary",
    { disabled: true, onclick: () => (isFolder ? browse() : start()) },
    "Fetch"
  );
  let isFolder = false;

  const inspect = debounce(async () => {
    const value = url.value.trim();
    importButton.disabled = !value;
    if (!value) {
      verdict.textContent = "";
      cookieRow.style.display = "none";
      return;
    }
    try {
      const info = await api.inspectLink(value);
      const label = LINK_LABEL[info.kind] || "Link";
      // The note often opens with the service name; avoid saying it twice.
      const note = (info.note || "").startsWith(label)
        ? info.note.slice(label.length).replace(/^\s*[—-]\s*/, "")
        : info.note;
      const parts = [`Recognised as ${label}.`];
      if (note) parts.push(note.charAt(0).toUpperCase() + note.slice(1));
      if (info.uses_extractor && !info.extractor_available) {
        parts.push(
          "The media extractor is not installed — open Settings and install it under Dependencies."
        );
      }
      verdict.textContent = parts.join(" ");
      cookieRow.style.display = info.needs_auth ? "" : "none";
      if (info.has_saved_session) {
        sessionState.textContent =
          "A Microsoft session is saved. If it has expired, sign in again here.";
        if (signInStatus === "idle") {
          signInState.textContent = "A saved session is available and will be checked when used.";
        }
      }
      // A folder cannot be fetched as one file, so the primary action becomes
      // "show me what is in there" instead of a download that cannot work.
      isFolder = Boolean(info.is_folder);
      importButton.textContent = isFolder ? "See what is in the folder" : "Fetch";
      if (!isFolder) picker.style.display = "none";
    } catch (error) {
      verdict.textContent = error.message;
      cookieRow.style.display = "none";
      picker.style.display = "none";
    }
  }, 350);

  url.addEventListener("input", inspect);

  // --- a folder, not a file ---------------------------------------------
  // A folder share has no single file to fetch, so the contents are listed and
  // the user chooses. Guessing would be worse than asking.
  const picker = h("div", { style: { display: "none", marginTop: "12px" } });
  const chosen = new Set();

  async function browse() {
    mount(picker, h("div.faint.small", "Reading the folder…"));
    picker.style.display = "";
    let listing;
    try {
      listing = await api.browseLink(url.value.trim());
    } catch (error) {
      if (needsReauthentication(error.message)) {
        setSignInStatus("reauth", "The saved Microsoft session has expired.");
        showAuthMethod("signin");
      }
      mount(
        picker,
        h("div.subcard", h("div.small", error.message))
      );
      return;
    }

    if (listing.has_saved_session) {
      setSignInStatus("success", "The saved Microsoft session is working.");
    }

    chosen.clear();
    if (!listing.items.length) {
      mount(
        picker,
        h(
          "div.subcard",
          h(
            "div.small",
            listing.total
              ? `That folder has ${listing.total} item(s), but none of them are video or audio.`
              : "That folder is empty."
          )
        )
      );
      return;
    }

    const importSelected = h(
      "button.btn.btn-primary.btn-sm",
      { disabled: true, onclick: () => importChosen(listing) },
      "Import selected"
    );

    function refreshButton() {
      importSelected.disabled = chosen.size === 0;
      importSelected.textContent =
        chosen.size > 1 ? `Import ${chosen.size} recordings` : "Import selected";
    }

    const rows = listing.items.map((item) =>
      h(
        "label.pick",
        h("input", {
          type: "checkbox",
          onchange: (event) => {
            if (event.target.checked) chosen.add(item.url);
            else chosen.delete(item.url);
            refreshButton();
          },
        }),
        h(
          "div",
          { style: { flex: "1", minWidth: "0" } },
          h("div", item.name),
          h("div.faint.small.mono", bytes(item.size_bytes))
        )
      )
    );

    mount(
      picker,
      h(
        "div.subcard",
        h(
          "div.row-between",
          h("strong.small", `${listing.items.length} video(s) in this folder`),
          importSelected
        ),
        listing.skipped
          ? h(
              "div.faint.small",
              { style: { marginTop: "4px" } },
              `${listing.skipped} other file(s) hidden.`
            )
          : null,
        h("div.stack", { style: { marginTop: "10px" } }, rows)
      )
    );
  }

  async function importChosen(listing) {
    const picks = listing.items.filter((item) => chosen.has(item.url));
    if (picks.length > 1) {
      try {
        const started = await api.importFromLinks(
          picks.map((item) => ({
            url: item.url,
            title: item.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "),
          }))
        );
        toast(`${started.count} recordings queued for import and analysis.`, "ok");
        ctx.navigate(`#/project/${started.project_id}`);
      } catch (error) {
        if (needsReauthentication(error.message)) {
          setSignInStatus("reauth", "The saved Microsoft session has expired.");
          showAuthMethod("signin");
        }
        toast(error.message, "err");
      }
      return;
    }

    let first = null;
    for (const item of picks) {
      try {
        const started = await api.importFromLink({
          url: item.url,
          title: item.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "),
        });
        first = first || started;
      } catch (error) {
        if (needsReauthentication(error.message)) {
          setSignInStatus("reauth", "The saved Microsoft session has expired.");
          showAuthMethod("signin");
        }
        toast(`${item.name}: ${error.message}`, "err");
      }
    }
    if (!first) return;
    toast(
      picks.length > 1
        ? `${picks.length} downloads queued — see the Jobs chip.`
        : "Downloading…",
      "ok"
    );
    ctx.navigate(`#/project/${first.project_id}`);
  }

  async function start() {
    const value = url.value.trim();
    if (!value) return;
    importButton.disabled = true;

    let started;
    try {
      started = await api.importFromLink({
        url: value,
        title: title.value.trim(),
        browser_cookies: useCookies.checked ? browser.value : "",
      });
    } catch (error) {
      if (needsReauthentication(error.message)) {
        setSignInStatus("reauth", "The saved Microsoft session has expired.");
        showAuthMethod("signin");
      }
      toast(error.message, "err");
      importButton.disabled = false;
      return;
    }

    jobPanel.run(Promise.resolve(started), {
      title: "Downloading the recording",
      onDone: () => {
        // Analysis is not started for you: importing a video is often only a way
        // to get at it for editing, and analysing is slow and optional.
        toast("Downloaded. Analyse it when you are ready.", "ok");
        ctx.navigate(`#/project/${started.project_id}`);
      },
      onError: (error) => {
        importButton.disabled = false;
        if (needsReauthentication(error.message)) {
          setSignInStatus("reauth", "The saved Microsoft session has expired.");
          showAuthMethod("signin");
        }
      },
    });
  }

  return h(
    "div.stack",
    h("label.field", h("span", "Video link"), url),
    verdict,
    cookieRow,
    picker,
    h("label.field", { style: { marginTop: "4px" } }, h("span", "Title"), title),
    h(
      "div.row",
      { style: { marginTop: "6px" } },
      importButton,
      fetchPrivacy
    )
  );
}

function uploadPanel(ctx) {
  let videoFiles = [];
  let transcriptFiles = [];

  const videoLabel = h("div.upload-selection.muted.small", "No video chosen");
  const transcriptLabel = h("div.faint.small", "None — speech-to-text will be used");
  const titleInput = h("input", { type: "text", placeholder: "Optional — defaults to the filename" });
  const progressFill = h("i", { style: { width: "0%" } });
  const progressBar = h("div.bar", { style: { display: "none" } }, progressFill);
  const progressText = h("div.faint.small", "Uploading 0%");
  const progressHost = h(
    "div.upload-progress",
    { style: { display: "none" } },
    progressText,
    progressBar
  );
  const startButton = h(
    "button.btn.btn-primary",
    { disabled: true, onclick: () => start() },
    "Upload and analyse"
  );

  const videoInput = h("input", {
    type: "file",
    accept: "video/*,audio/*",
    multiple: true,
    style: { display: "none" },
    onchange: (event) => setVideos([...event.target.files]),
  });
  const transcriptInput = h("input", {
    type: "file",
    accept: ".srt,.vtt,.json",
    multiple: true,
    style: { display: "none" },
    onchange: (event) => setTranscripts([...event.target.files]),
  });

  function setVideos(files) {
    videoFiles = files.filter(Boolean);
    mount(
      videoLabel,
      videoFiles.length
        ? videoFiles.map((file) =>
            h(
              "div.upload-file-row",
              h("span.upload-file-mark", "VID"),
              h("span.upload-file-name", file.name),
              h("span.faint.small", bytes(file.size))
            )
          )
        : "No video chosen"
    );
    videoLabel.className = videoFiles.length ? "upload-selection selected" : "upload-selection muted small";
    drop.classList.toggle("has-file", videoFiles.length > 0);
    startButton.disabled = videoFiles.length === 0;
    startButton.textContent = videoFiles.length > 1
      ? `Upload and analyse ${videoFiles.length} files`
      : "Upload and analyse";
  }

  function setTranscripts(files) {
    transcriptFiles = files.filter(Boolean);
    transcriptLabel.textContent = transcriptFiles.length
      ? `${transcriptFiles.map((file) => file.name).join(", ")} · matched by filename`
      : "None — speech-to-text will be used";
    transcriptLabel.className = transcriptFiles.length ? "small" : "faint small";
  }

  const drop = h(
    "div.drop",
    {
      onclick: () => videoInput.click(),
      ondragover: (event) => {
        event.preventDefault();
        drop.classList.add("over");
      },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (event) => {
        event.preventDefault();
        drop.classList.remove("over");
        for (const file of event.dataTransfer.files) {
          if (/\.(srt|vtt|json)$/i.test(file.name)) transcriptFiles.push(file);
          else videoFiles.push(file);
        }
        setVideos(videoFiles);
        setTranscripts(transcriptFiles);
      },
    },
    h("strong", "Drop a video here, or click to choose"),
    h("div.faint.small", "mp4, mov, mkv, webm, or an audio-only file"),
    h("div", { style: { marginTop: "10px" } }, videoLabel)
  );

  async function start() {
    if (!videoFiles.length) return;
    startButton.disabled = true;
    startButton.textContent = "Uploading…";
    progressHost.style.display = "";
    progressBar.style.display = "";

    try {
      const onProgress = (fraction) => {
        progressFill.style.width = `${fraction * 100}%`;
        progressText.textContent = `Uploading ${Math.round(fraction * 100)}%`;
      };
      if (videoFiles.length === 1) {
        const project = await uploadProject(
          { video: videoFiles[0], transcript: transcriptFiles[0] || null, title: titleInput.value },
          onProgress
        );
        toast("Uploaded. Starting analysis…", "ok");
        ctx.navigate(`#/project/${project.id}?autostart=1`);
      } else {
        const batch = await uploadProjects(
          { videos: videoFiles, transcripts: transcriptFiles, title: titleInput.value },
          onProgress
        );
        toast(`${batch.count} recordings uploaded and queued for analysis.`, "ok");
        ctx.navigate(`#/project/${batch.project_id}`);
      }
    } catch (error) {
      toast(error.message, "err");
      startButton.disabled = false;
      startButton.textContent = "Upload and analyse";
      progressHost.style.display = "none";
    }
  }

  return h(
    "div",
    videoInput,
    transcriptInput,
    drop,
    h(
      "div.grid.grid-2",
      { style: { marginTop: "14px" } },
      h("label.field", h("span", "Title"), titleInput),
      h(
        "div",
        h("div.small.muted", { style: { marginBottom: "5px" } }, "Existing transcript (optional)"),
        h(
          "div.row",
          h("button.btn.btn-sm", { onclick: () => transcriptInput.click() }, "Choose .srt / .vtt"),
          h("button.btn.btn-sm.btn-ghost", { onclick: () => setTranscripts([]) }, "Clear")
        ),
        h("div", { style: { marginTop: "6px" } }, transcriptLabel)
      )
    ),
    h(
      "div.row",
      { style: { marginTop: "16px" } },
      startButton,
      h(
        "span.faint.small",
        "Teams, Stream and Zoom recordings usually ship a .vtt — using it makes this near-instant."
      )
    ),
    progressHost
  );
}

// --- existing work -----------------------------------------------------------
function projectList(projects, ctx) {
  if (!projects.length) {
    return h("div.empty", "Nothing here yet. Paste a link above to get started.");
  }

  return h(
    "div.card",
    h("h2", `Recordings (${projects.length})`),
    h(
      "table",
      h(
        "thead",
        h(
          "tr",
          h("th", "Title"),
          h("th", "Length"),
          h("th", "Size"),
          h("th", "Tokens"),
          h("th", { title: "GitHub AI Credits — 1 credit = $0.01" }, "Credits"),
          h("th", "Added"),
          h("th", "Status"),
          h("th", "")
        )
      ),
      h(
        "tbody",
        projects.map((project) => {
          const [pillClass, pillText] = STATUS_PILL[project.status] || STATUS_PILL.new;
          return h(
            "tr",
            h(
              "td",
              h(
                "a",
                {
                  href: `#/project/${project.id}`,
                  onclick: (event) => {
                    event.preventDefault();
                    ctx.navigate(`#/project/${project.id}`);
                  },
                },
                project.title || project.source_filename
              ),
              h("div.faint.small.mono", project.source_filename),
              project.source_url
                ? h("div.faint.small", "imported from a link")
                : null
            ),
            h("td.nowrap", duration(project.duration_s)),
            h("td.nowrap", bytes(project.size_bytes)),
            tokenCell(project.tokens),
            creditCell(project.tokens),
            h("td.nowrap.faint.small", relativeTime(project.created_at)),
            h(
              "td",
              h("span", { class: `pill ${pillClass}` }, h("i.dot"), pillText),
              project.has_uploaded_transcript
                ? h("span.tag", { style: { marginLeft: "6px" } }, "transcript")
                : null
            ),
            h(
              "td",
              { style: { textAlign: "right" } },
              h(
                "button.btn.btn-sm.btn-ghost.btn-danger",
                {
                  onclick: async () => {
                    if (
                      !(await confirmAction(
                        `Delete "${project.title}" and everything derived from it?`
                      ))
                    )
                      return;
                    await api.deleteProject(project.id).catch((e) => toast(e.message, "err"));
                    ctx.refresh();
                  },
                },
                "Delete"
              )
            )
          );
        })
      )
    )
  );
}
