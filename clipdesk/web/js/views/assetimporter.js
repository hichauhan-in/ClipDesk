// Add intro/outro and B-roll videos to this project from disk or any link.

import { api, uploadAsset } from "../api.js";
import { debounce, h, mount, toast } from "../dom.js";
import { bytes } from "../format.js";

const LINK_LABEL = {
  youtube: "YouTube",
  sharepoint: "SharePoint / Stream",
  onedrive: "OneDrive",
  google_drive: "Google Drive",
  streaming_site: "Video platform",
  direct: "Direct video link",
  unknown: "Link",
};

export function createAssetImporter({ projectId, jobPanel, onChanged }) {
  const fileInput = h("input", {
    type: "file",
    accept: "video/mp4,video/quicktime,video/x-matroska,video/webm",
    style: { display: "none" },
  });
  const uploadStatus = h("div.faint.small", "MP4, MOV, MKV or WebM.");
  const uploadButton = h(
    "button.btn.btn-primary.btn-sm",
    { onclick: () => fileInput.click() },
    "Choose video"
  );
  fileInput.onchange = async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    uploadButton.disabled = true;
    uploadStatus.textContent = `Uploading ${file.name}…`;
    try {
      const saved = await uploadAsset(projectId, file, (fraction) => {
        uploadStatus.textContent = `Uploading ${file.name} — ${Math.round(fraction * 100)}%`;
      });
      uploadStatus.textContent = `${saved.name} added (${bytes(saved.size_bytes)}).`;
      toast(`${saved.name} added to this project.`, "ok");
      onChanged?.();
    } catch (error) {
      uploadStatus.textContent = error.message;
      toast(error.message, "err");
    } finally {
      uploadButton.disabled = false;
      fileInput.value = "";
    }
  };
  const uploadPane = h(
    "div.stack",
    { style: { display: "none" } },
    h(
      "p.muted.small",
      "Choose a video already on this device. It is copied into this project."
    ),
    h("div.row", uploadButton, uploadStatus),
    fileInput
  );

  const url = h("input", {
    type: "url",
    placeholder: "Paste a SharePoint, OneDrive, YouTube or direct video link",
  });
  const verdict = h("div.faint.small", { style: { minHeight: "18px" } });
  const results = h("div");
  const auth = h("div", { style: { display: "none" } });
  const signInMessage = h("span.faint.small");
  const signIn = h("button.btn.btn-sm.btn-primary", { onclick: () => runSignIn() }, "Sign in");
  const paste = h("textarea", {
    rows: 3,
    placeholder: "Paste a signed-in request copied as cURL",
    spellcheck: "false",
    style: { fontFamily: "var(--mono)", fontSize: "11px" },
  });
  const pasteStatus = h("span.faint.small");
  let folder = false;
  let authState = "idle";

  function setAuthState(state, message = "") {
    authState = state;
    signIn.classList.remove(
      "btn-primary", "btn-success", "btn-warning", "btn-error", "btn-reauth"
    );
    const states = {
      idle: ["Sign in", "btn-primary", false],
      signing: ["Signing in…", "btn-warning", true],
      success: ["Signed in", "btn-success", false],
      failed: ["Sign in failed", "btn-error", false],
      reauth: ["Reauthenticate", "btn-reauth", false],
    };
    const [label, className, disabled] = states[state] || states.idle;
    signIn.textContent = label;
    signIn.classList.add(className);
    signIn.disabled = disabled;
    signInMessage.textContent = message;
  }

  function needsReauthentication(message) {
    return /session has expired|sign in (?:to microsoft )?again|returned a sign-in page/i.test(
      message || ""
    );
  }

  async function runSignIn() {
    if (!url.value.trim()) return;
    setAuthState("signing");
    jobPanel.run(api.startSignIn(url.value.trim()), {
      title: "Signing in",
      onDone: (result) => {
        setAuthState("success", "Microsoft session ready.");
        if (result.resolved_url) url.value = result.resolved_url;
        inspect();
        if (folder) browse();
      },
      onError: (error) => setAuthState("failed", error.message),
    });
  }

  async function savePastedSession() {
    if (!paste.value.trim()) {
      toast("Paste the signed-in request first.", "err");
      return;
    }
    try {
      const saved = await api.saveSession(paste.value.trim(), url.value.trim());
      paste.value = "";
      pasteStatus.textContent = `Saved for ${saved.host}.`;
      setAuthState("success", "Saved session ready.");
    } catch (error) {
      pasteStatus.textContent = error.message;
    }
  }

  mount(
    auth,
    h("div.row", signIn, signInMessage)
  );

  const action = h("button.btn.btn-primary.btn-sm", { disabled: true }, "Add to project");
  action.onclick = () => (folder ? browse() : addItems([{ url: url.value.trim(), name: "" }]));

  const inspect = debounce(async () => {
    const value = url.value.trim();
    action.disabled = !value;
    mount(results);
    if (!value) {
      verdict.textContent = "";
      auth.style.display = "none";
      return;
    }
    try {
      const info = await api.inspectLink(value);
      folder = Boolean(info.is_folder);
      const label = LINK_LABEL[info.kind] || "Link";
      verdict.textContent = folder
        ? `${label} folder — choose the videos to add.`
        : `${label} — ready to add to the library.`;
      action.textContent = folder ? "Show videos" : "Add to project";
      auth.style.display = info.needs_auth ? "" : "none";
      if (info.has_saved_session && authState === "idle") {
        signInMessage.textContent = "A saved Microsoft session will be checked when used.";
      }
    } catch (error) {
      verdict.textContent = error.message;
      action.disabled = true;
      auth.style.display = "none";
    }
  }, 350);
  url.addEventListener("input", inspect);

  async function browse() {
    action.disabled = true;
    mount(results, h("div.faint.small", "Reading the folder…"));
    let listing;
    try {
      listing = await api.browseLink(url.value.trim());
    } catch (error) {
      action.disabled = false;
      if (needsReauthentication(error.message)) {
        auth.style.display = "";
        setAuthState("reauth", "The saved Microsoft session has expired.");
      }
      mount(results, h("div.faint.small", error.message));
      return;
    }
    action.disabled = false;
    if (listing.has_saved_session) setAuthState("success", "The saved session is working.");
    const selected = new Set();
    const addSelected = h(
      "button.btn.btn-primary.btn-sm",
      { disabled: true },
      "Add selected"
    );
    const rows = listing.items.map((item) =>
      h(
        "label.pick",
        h("input", {
          type: "checkbox",
          onchange: (event) => {
            if (event.target.checked) selected.add(item.url);
            else selected.delete(item.url);
            addSelected.disabled = selected.size === 0;
            addSelected.textContent =
              selected.size > 1 ? `Add ${selected.size} videos` : "Add selected";
          },
        }),
        h(
          "div.pick-body",
          h("strong", item.name),
          h("div.faint.small.mono", bytes(item.size_bytes))
        )
      )
    );
    addSelected.onclick = () =>
      addItems(
        listing.items
          .filter((item) => selected.has(item.url))
          .map((item) => ({ url: item.url, name: item.name }))
      );
    mount(
      results,
      listing.items.length
        ? h(
            "div.stack",
            h("div.row-between", h("strong.small", `${listing.items.length} video(s)`), addSelected),
            rows
          )
        : h("div.faint.small", "This folder has no video files ClipDesk can use.")
    );
  }

  function addItems(items) {
    if (!items.length) return;
    jobPanel.run(api.importMediaFromLinks(projectId, items), {
      title: items.length > 1 ? `Adding ${items.length} editor assets` : "Adding editor asset",
      onDone: (result) => {
        const count = result.assets?.length || items.length;
        toast(`${count} video${count === 1 ? "" : "s"} added to the library.`, "ok");
        onChanged?.();
      },
      onError: (error) => {
        if (needsReauthentication(error.message)) {
          auth.style.display = "";
          setAuthState("reauth", "The saved Microsoft session has expired.");
        }
      },
    });
  }

  const linkPane = h(
    "div.stack",
    h("label.field", h("span", "Video or folder link"), url),
    verdict,
    auth,
    h("div.row", action),
    results
  );
  const curlPane = h(
    "div.stack",
    { style: { display: "none" } },
    h(
      "p.muted.small",
      "For a protected SharePoint or OneDrive link, copy a signed-in network request " +
        "as cURL and save that session to your ClipDesk account."
    ),
    h("label.field", h("span", "Copied cURL request"), paste),
    h(
      "div.row",
      h(
        "button.btn.btn-primary.btn-sm",
        {
          onclick: async () => {
            await savePastedSession();
            if (!pasteStatus.textContent.startsWith("Saved")) return;
            selectMode("link");
            if (url.value.trim()) inspect();
          },
        },
        "Save and return to URL"
      ),
      pasteStatus
    ),
    h(
      "div.faint.small",
      "The copied request normally contains its own URL. Paste the target URL on the URL " +
        "tab first only when you copied a bare Cookie header."
    )
  );
  const modes = [
    ["link", "URL", linkPane],
    ["upload", "Upload file", uploadPane],
    ["curl", "Paste cURL", curlPane],
  ];
  const modeBar = h(
    "div.tabs.asset-source-tabs",
    { role: "tablist", "aria-label": "Additional media source" },
    modes.map(([key, label]) =>
      h(
        "button",
        {
          role: "tab",
          "data-mode": key,
          "aria-selected": String(key === "link"),
          onclick: () => selectMode(key),
        },
        label
      )
    )
  );

  function selectMode(key) {
    for (const [mode, , pane] of modes) pane.style.display = mode === key ? "" : "none";
    for (const button of modeBar.children) {
      button.setAttribute("aria-selected", String(button.dataset.mode === key));
    }
  }

  api.signInCapability().then((capability) => {
    if (!capability.hosted) return;
    signIn.disabled = true;
    signInMessage.textContent = "Interactive sign-in is unavailable on a hosted server.";
  }).catch(() => {});

  return h(
    "div.subcard.asset-importer",
    modeBar,
    h("div", { style: { marginTop: "12px" } }, uploadPane, linkPane, curlPane)
  );
}
