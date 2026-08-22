// Every call to the backend goes through here.

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload || {};
  }
}

async function request(path, { method = "GET", body, signal } = {}) {
  const init = { method, signal, headers: {}, cache: "no-store" };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Cannot reach the ClipDesk server. Is it still running?", 0);
  }

  const isJson = (response.headers.get("content-type") || "").includes("json");
  const payload = isJson ? await response.json().catch(() => ({})) : await response.text();

  if (!response.ok) {
    const detail =
      (payload && payload.detail) ||
      (typeof payload === "string" && payload.slice(0, 300)) ||
      `Request failed (${response.status})`;
    throw new ApiError(
      Array.isArray(detail) ? detail.map((d) => d.msg || d).join("; ") : detail,
      response.status,
      payload
    );
  }
  return payload;
}

export const api = {
  health: () => request("/api/health"),

  setup: () => request("/api/setup"),
  provision: (component) =>
    request("/api/setup/provision", { method: "POST", body: { component } }),

  getSettings: () => request("/api/settings"),
  putSettings: (body) => request("/api/settings", { method: "PUT", body }),
  shutdown: (force = false) =>
    request("/api/shutdown", { method: "POST", body: { force } }),

  listProjects: () => request("/api/projects"),  getProject: (id) => request(`/api/projects/${id}`),
  deleteProject: (id) => request(`/api/projects/${id}`, { method: "DELETE" }),
  renameProject: (id, title) =>
    request(`/api/projects/${id}/rename`, { method: "POST", body: { title } }),
  getAnalysis: (id) => request(`/api/projects/${id}/analysis`),
  transcriptCheckpoint: (id) => request(`/api/projects/${id}/transcript/checkpoint`),

  inspectLink: (url) => request("/api/links/inspect", { method: "POST", body: { url } }),
  browseLink: (url) => request("/api/links/browse", { method: "POST", body: { url } }),

  listSources: () => request("/api/sources"),
  browseSource: (rootId, path = "") =>
    request(`/api/sources/${encodeURIComponent(rootId)}/browse?path=${encodeURIComponent(path)}`),
  searchSource: (rootId, query) =>
    request(`/api/sources/${encodeURIComponent(rootId)}/search?q=${encodeURIComponent(query)}`),
  importLocal: (body) => request("/api/projects/from-local", { method: "POST", body }),
  importLocalBatch: (items) =>
    request("/api/projects/from-local/batch", { method: "POST", body: { items } }),
  importFromLink: (body) => request("/api/projects/from-link", { method: "POST", body }),
  importFromLinks: (items) =>
    request("/api/projects/from-links", { method: "POST", body: { items } }),

  analyze: (id, body) => request(`/api/projects/${id}/analyze`, { method: "POST", body }),
  notes: (id, body) => request(`/api/projects/${id}/notes`, { method: "POST", body }),
  articleOptions: () => request("/api/article/options"),
  llmPlan: (level) => request(`/api/llm/plan?level=${level}`),
  article: (id, body) => request(`/api/projects/${id}/article`, { method: "POST", body }),
  ask: (id, body) => request(`/api/projects/${id}/ask`, { method: "POST", body }),
  cleanupPlan: (id, body) =>
    request(`/api/projects/${id}/cleanup/plan`, { method: "POST", body }),
  cleanup: (id, body) => request(`/api/projects/${id}/cleanup`, { method: "POST", body }),
  findClips: (id, body) => request(`/api/projects/${id}/clips/find`, { method: "POST", body }),
  renderClips: (id, body) =>
    request(`/api/projects/${id}/clips/render`, { method: "POST", body }),
  bookend: (id, body) => request(`/api/projects/${id}/bookend`, { method: "POST", body }),
  intro: (id, body) => request(`/api/projects/${id}/intro`, { method: "POST", body }),
  outro: (id, body) => request(`/api/projects/${id}/outro`, { method: "POST", body }),
  introStyles: () => request("/api/intro/styles"),
  introAudio: () => request("/api/intro/audio"),
  refreshIntroVoices: () =>
    request("/api/intro/voices/refresh", { method: "POST" }),
  installIntroStyle: (styleId) =>
    request("/api/intro/styles/install", {
      method: "POST",
      body: { style_id: styleId },
    }),
  importIntroStyle: (definition) =>
    request("/api/intro/styles/import", { method: "POST", body: definition }),
  previewEdit: (id, prompt) =>
    request(`/api/projects/${id}/edit`, {
      method: "POST",
      body: { prompt, preview_only: true },
    }),
  renderEdit: (id, body) => request(`/api/projects/${id}/edit`, { method: "POST", body }),
  planPrompt: (id, prompt) =>
    request(`/api/projects/${id}/plan`, { method: "POST", body: { prompt } }),
  buildIntro: (id, body) => request(`/api/projects/${id}/intro`, { method: "POST", body }),
  exportOptions: () => request("/api/export/options"),
  exportOutput: (id, body) => request(`/api/projects/${id}/export`, { method: "POST", body }),
  exportTranscript: (id, format) =>
    request(`/api/projects/${id}/transcript`, { method: "POST", body: { format } }),
  exportSummary: (id) => request(`/api/projects/${id}/summary`, { method: "POST" }),

  listFlows: () => request("/api/flows"),
  saveFlow: (id, body) =>
    request(`/api/flows/${encodeURIComponent(id)}`, { method: "PUT", body }),
  deleteFlow: (id) =>
    request(`/api/flows/${encodeURIComponent(id)}`, { method: "DELETE" }),
  runFlow: (projectId, flowId) =>
    request(`/api/projects/${projectId}/flows/${encodeURIComponent(flowId)}/run`, {
      method: "POST",
    }),

  listOutputs: (id) => request(`/api/projects/${id}/outputs`),

  queue: (id) => request(`/api/projects/${id}/queue`),
  runQueue: (id) => request(`/api/projects/${id}/queue/run`, { method: "POST" }),
  clearQueue: (id) => request(`/api/projects/${id}/queue`, { method: "DELETE" }),
  removeQueueStep: (id, stepId) =>
    request(`/api/projects/${id}/queue/${encodeURIComponent(stepId)}`, { method: "DELETE" }),
  moveQueueStep: (id, stepId, offset) =>
    request(`/api/projects/${id}/queue/${encodeURIComponent(stepId)}/move`, {
      method: "POST",
      body: { offset },
    }),
  deleteSource: (id) => request(`/api/projects/${id}/source`, { method: "DELETE" }),
  deleteOutput: (id, filename) =>
    request(`/api/projects/${id}/outputs/${encodeURIComponent(filename)}`, {
      method: "DELETE",
    }),
  renameOutput: (id, filename, name) =>
    request(`/api/projects/${id}/outputs/${encodeURIComponent(filename)}/rename`, {
      method: "POST",
      body: { name },
    }),
  outputDocument: (id, filename) =>
    request(`/api/projects/${id}/outputs/${encodeURIComponent(filename)}/document`),
  bundleOutputs: (id, filenames, archiveName = "") =>
    request(`/api/projects/${id}/outputs/bundle`, {
      method: "POST",
      body: { filenames, archive_name: archiveName },
    }),
  revealOutputs: (id) =>
    request(`/api/projects/${id}/outputs/reveal`, { method: "POST" }),

  listMedia: (projectId) => request(`/api/projects/${projectId}/media`),
  mediaLibrary: (projectId) => request(`/api/projects/${projectId}/media-library`),
  adoptMedia: (projectId, name, sourceProjectId = "") =>
    request(`/api/projects/${projectId}/media/adopt`, {
      method: "POST",
      body: { name, source_project_id: sourceProjectId },
    }),
  importMediaFromLinks: (projectId, items) =>
    request(`/api/projects/${projectId}/media/from-link`, {
      method: "POST",
      body: { project_id: projectId, items },
    }),
  deleteMedia: (projectId, name) =>
    request(`/api/projects/${projectId}/media/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  getJob: (jobId) => request(`/api/jobs/${jobId}`),
  listJobs: (projectId = "") =>
    request(`/api/jobs${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  cancelJob: (jobId) => request(`/api/jobs/${jobId}/cancel`, { method: "POST" }),

  listSessions: () => request("/api/sessions"),
  signInCapability: () => request("/api/sessions/capability"),
  startSignIn: (url) => request("/api/sessions/sign-in", { method: "POST", body: { url } }),
  saveSession: (pasted, url = "") =>
    request("/api/sessions", { method: "POST", body: { pasted, url } }),
  deleteSession: (host) =>
    request(`/api/sessions/${encodeURIComponent(host)}`, { method: "DELETE" }),
};

/**
 * Upload with real progress. fetch() cannot report upload progress, so this is
 * the one place XMLHttpRequest is still the right tool.
 */
export function uploadProject({ video, transcript, title }, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("video", video);
    if (transcript) form.append("transcript", transcript);
    form.append("title", title || "");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/projects");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        /* fall through to the status check */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(payload.detail || `Upload failed (${xhr.status})`, xhr.status));
    });
    xhr.addEventListener("error", () =>
      reject(new ApiError("The upload was interrupted.", 0))
    );
    xhr.send(form);
  });
}

export function uploadProjects({ videos, transcripts = [], title = "" }, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const video of videos) form.append("videos", video);
    for (const transcript of transcripts) form.append("transcripts", transcript);
    form.append("title", title);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/projects/batch");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText || "{}");
      } catch {
        /* status handling below */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(payload.detail || `Batch upload failed (${xhr.status})`, xhr.status));
    });
    xhr.addEventListener("error", () =>
      reject(new ApiError("The batch upload was interrupted.", 0))
    );
    xhr.send(form);
  });
}

export function uploadAsset(projectId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects/${projectId}/media`);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      const payload = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(payload.detail || "Upload failed", xhr.status));
    });
    xhr.addEventListener("error", () => reject(new ApiError("Upload failed", 0)));
    xhr.send(form);
  });
}

export function uploadIntroAudio(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/intro/audio");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText || "{}");
      } catch {
        /* status handling below */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(payload.detail || "Audio upload failed", xhr.status));
    });
    xhr.addEventListener("error", () => reject(new ApiError("Audio upload failed", 0)));
    xhr.send(form);
  });
}

/**
 * Follow a job to completion, streaming events over a WebSocket and falling
 * back to polling if the socket cannot be opened (some corporate proxies).
 */
export function followJob(jobId, { onEvent, onDone, onError, onCancelled } = {}) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  let socket;
  let closed = false;

  const finish = (status, result, error) => {
    if (closed) return;
    closed = true;
    try {
      socket?.close();
    } catch {
      /* already closing */
    }
    if (status === "cancelled") onCancelled?.();
    else if (status === "failed" || error) onError?.(error || "The job failed.");
    else onDone?.(result || {});
  };

  try {
    socket = new WebSocket(`${protocol}//${location.host}/ws/jobs/${jobId}`);
  } catch {
    poll();
    return () => {
      closed = true;
    };
  }

  socket.addEventListener("message", (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (payload.type === "closed") {
      finish(payload.status, payload.result, payload.error);
      return;
    }
    onEvent?.(payload);
    if (payload.type === "error") finish("failed", null, payload.message);
    if (payload.type === "done") finish("done", payload.data?.result);
  });

  socket.addEventListener("error", () => {
    if (!closed) poll();
  });
  socket.addEventListener("close", () => {
    if (!closed) poll();
  });

  let polling = false;
  async function poll() {
    if (polling || closed) return;
    polling = true;
    let seen = 0;
    while (!closed) {
      let job;
      try {
        job = await api.getJob(jobId);
      } catch {
        finish("failed", null, "Lost contact with the server.");
        return;
      }
      for (const event of job.events.slice(seen)) onEvent?.(event);
      seen = job.events.length;
      if (["done", "failed", "cancelled"].includes(job.status)) {
        finish(job.status, job.result, job.error);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }

  return () => {
    closed = true;
    try {
      socket?.close();
    } catch {
      /* ignore */
    }
  };
}

export { ApiError };
