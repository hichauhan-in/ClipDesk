// ClipDesk Bridge — exposes VS Code's Language Model API on localhost.
//
// This is the sanctioned route from local code to a GitHub Copilot seat: the
// request goes through `vscode.lm`, exactly like any other Copilot-powered
// extension, so quota, policy and telemetry all behave normally. Nothing here
// touches a private Copilot endpoint or reuses a Copilot token.
//
// Security model — the server is only as safe as its weakest caller, so:
//   * it binds to 127.0.0.1, never 0.0.0.0;
//   * every request must carry a bearer token that is regenerated each session
//     and written to a file only this user can read;
//   * requests carrying an Origin or Sec-Fetch-Site header are rejected, which
//     shuts the door on a malicious web page trying DNS rebinding;
//   * the request body is size-capped.

const http = require("node:http");
const net = require("node:net");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const vscode = require("vscode");

const MAX_BODY_BYTES = 8 * 1024 * 1024;
const HANDSHAKE_DIR = path.join(os.homedir(), ".clipdesk");
const HANDSHAKE_FILE = path.join(HANDSHAKE_DIR, "bridge.json");
// Read from the manifest so it cannot drift from the published version.
const BRIDGE_VERSION = require("./package.json").version;
//: How often to check the handshake is still there, and to retry the port.
const WATCHDOG_MS = 20000;

let server = null;
let token = "";
let statusBar = null;
let output = null;
let watchdog = null;
/** True when another window already serves the bridge and this one stood down. */
let standby = false;
/** Set once a request has actually reached the model, i.e. consent was given. */
let consented = false;

function log(message) {
  const stamp = new Date().toISOString().slice(11, 19);
  output?.appendLine(`[${stamp}] ${message}`);
}

function config() {
  return vscode.workspace.getConfiguration("clipdeskBridge");
}

// --- model selection ---------------------------------------------------------
async function listModels() {
  try {
    return await vscode.lm.selectChatModels({ vendor: "copilot" });
  } catch (error) {
    log(`Could not list models: ${error.message}`);
    return [];
  }
}

async function pickModel(requestedFamily) {
  const models = await listModels();
  if (models.length === 0) return null;
  const family = requestedFamily || config().get("modelFamily") || "";
  if (family) {
    const match = matchFamily(models, family);
    if (match) return match;
  }
  return defaultModel(models);
}

function matchFamily(models, wanted) {
  return models.find(
    (model) => model.family === wanted || model.id === wanted || model.name === wanted
  );
}

// VS Code returns models in no meaningful order, so taking models[0] lands on
// whatever happens to be first — often the most expensive one available.
// ClipDesk issues one call per transcript window plus one per notes section, so
// the default leans towards fast, inexpensive models that are still strong
// enough for classification and summarising. An explicit setting, or a model
// named in the request, always wins over this list.
const PREFERRED_FAMILIES = [
  "gpt-4o-mini",
  "gpt-5-mini",
  "gpt-5.4-mini",
  "gpt-4o",
  "gpt-5.4",
  "claude-haiku-4.5",
  "claude-sonnet-4.6",
];

function defaultModel(models) {
  for (const family of PREFERRED_FAMILIES) {
    const match = matchFamily(models, family);
    if (match) return match;
  }
  return models[0];
}

// --- request handling --------------------------------------------------------
function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  response.end(body);
}

function authorised(request) {
  const header = request.headers.authorization || "";
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!provided) return false;
  // Compare byte lengths, not string lengths: timingSafeEqual throws on a length
  // mismatch, and a multi-byte character makes those two differ.
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(token, "utf8");
  if (a.length !== b.length) return false;
  // Constant-time compare so the token cannot be guessed a byte at a time.
  return crypto.timingSafeEqual(a, b);
}

function fromBrowser(request) {
  return Boolean(request.headers.origin || request.headers["sec-fetch-site"]);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("Request body is too large."));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {});
      } catch (error) {
        reject(new Error("Request body is not valid JSON."));
      }
    });
    request.on("error", reject);
  });
}

/**
 * The Language Model API has no system-message channel, so a system message is
 * folded into the first user turn. That is the documented approach and matches
 * what the Copilot Chat extension itself does.
 */
function toLmMessages(messages) {
  const out = [];
  const systemParts = [];
  for (const message of messages || []) {
    const content = String(message?.content ?? "").trim();
    if (!content) continue;
    if (message.role === "system") systemParts.push(content);
    else if (message.role === "assistant") out.push(vscode.LanguageModelChatMessage.Assistant(content));
    else out.push(vscode.LanguageModelChatMessage.User(content));
  }
  if (systemParts.length) {
    out.unshift(vscode.LanguageModelChatMessage.User(systemParts.join("\n\n")));
  }
  return out;
}

async function handleChat(request, response) {
  let payload;
  try {
    payload = await readBody(request);
  } catch (error) {
    send(response, 400, { error: error.message });
    return;
  }

  const messages = toLmMessages(payload.messages);
  if (messages.length === 0) {
    send(response, 400, { error: "No messages were supplied." });
    return;
  }

  const model = await pickModel(payload.model);
  if (!model) {
    send(response, 503, {
      error:
        "VS Code reports no Copilot chat models. Sign in to GitHub Copilot in this " +
        "VS Code window, then try again.",
    });
    return;
  }

  // Fail with a useful message instead of letting the model truncate silently.
  let promptTokens = 0;
  try {
    for (const message of messages) {
      // Counted as a message, not as its text: the chat format adds role and
      // delimiter tokens per message that are billed but are not in the string.
      promptTokens += await model.countTokens(message);
    }
    const configuredLimit = Number.isSafeInteger(payload.context_window_tokens)
      ? payload.context_window_tokens
      : 0;
    const inputLimit = configuredLimit && model.maxInputTokens
      ? Math.min(configuredLimit, model.maxInputTokens)
      : configuredLimit || model.maxInputTokens;
    if (inputLimit && promptTokens > inputLimit) {
      send(response, 413, {
        error:
          `The prompt is ${promptTokens} tokens but ${model.name} accepts ` +
          `${inputLimit} with the current context setting. Lower the context window ` +
          `size or analysis.window_chars in ClipDesk's settings.`,
      });
      return;
    }
  } catch {
    // Token counting is best-effort; a failure here should not block the call.
  }

  const cancellation = new vscode.CancellationTokenSource();
  request.on("close", () => cancellation.cancel());

  try {
    const modelOptions = {};
    if (payload.reasoning_effort) modelOptions.reasoningEffort = payload.reasoning_effort;
    if (payload.context_window_tokens) modelOptions.contextWindow = payload.context_window_tokens;
    const requestOptions = Object.keys(modelOptions).length ? { modelOptions } : {};
    const chatResponse = await model.sendRequest(messages, requestOptions, cancellation.token);
    let text = "";
    for await (const fragment of chatResponse.text) text += fragment;
    consented = true;
    updateStatus();

    // Counted with the model's own tokenizer, so ClipDesk reports what was
    // actually spent rather than a guess from character counts.
    let completionTokens = 0;
    try {
      completionTokens = await model.countTokens(text);
    } catch {
      /* best effort */
    }

    send(response, 200, {
      id: `clipdesk-${Date.now().toString(36)}`,
      model: model.id,
      choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
      usage: {
        prompt_tokens: promptTokens,
        completion_tokens: completionTokens,
        total_tokens: promptTokens + completionTokens,
      },
    });
  } catch (error) {
    const isLmError = error instanceof vscode.LanguageModelError;
    const code = isLmError ? error.code : "";
    log(`Request failed (${code || "unknown"}): ${error.message}`);

    if (code === "NoPermissions" || /consent/i.test(error.message || "")) {
      send(response, 403, {
        error:
          "Copilot access has not been authorised for this extension. Run " +
          "'ClipDesk Bridge: Authorise Copilot Access' from the VS Code command palette.",
      });
      return;
    }
    if (code === "Blocked" || /off_topic|content filter/i.test(error.message || "")) {
      send(response, 422, {
        error: "Copilot declined to answer this request (content filter).",
      });
      return;
    }
    send(response, 502, { error: error.message || "The model request failed." });
  } finally {
    cancellation.dispose();
  }
}

async function handleHealth(response) {
  const models = await listModels();
  const chosen = models.length ? defaultModel(models) : null;
  send(response, 200, {
    ok: true,
    copilot_available: models.length > 0,
    consented: consented || models.length > 0,
    models: models.map((model) => model.family || model.id),
    default_model: chosen ? chosen.family || chosen.id : "",
    vscode_version: vscode.version,
    // The version of the code actually executing, which is not necessarily the
    // one on disk: VS Code caches the module until the window is reloaded.
    bridge_version: BRIDGE_VERSION,
  });
}

// --- server lifecycle --------------------------------------------------------
function writeHandshake(port) {
  fs.mkdirSync(HANDSHAKE_DIR, { recursive: true });
  const payload = {
    base_url: `http://127.0.0.1:${port}`,
    token,
    pid: process.pid,
    started_at: new Date().toISOString(),
    bridge_version: BRIDGE_VERSION,
  };
  // 0o600: readable only by this user, so another account cannot spend the seat.
  // Written to a sibling then renamed, because rename is atomic: ClipDesk polls
  // this file and would otherwise sometimes read it half-written and conclude
  // the bridge was down.
  const staging = `${HANDSHAKE_FILE}.${process.pid}.tmp`;
  fs.writeFileSync(staging, JSON.stringify(payload, null, 2), { mode: 0o600 });
  fs.renameSync(staging, HANDSHAKE_FILE);
  try {
    fs.chmodSync(HANDSHAKE_FILE, 0o600);
  } catch {
    // Windows ignores POSIX modes; ACL inheritance from the user profile applies.
  }
  log(`Handshake written to ${HANDSHAKE_FILE}`);
}

function removeHandshake() {
  // Every VS Code window runs its own copy of this extension. Deleting a
  // handshake another window wrote would take the bridge down underneath it,
  // and nothing would put it back — so only ever remove our own.
  const existing = readHandshake();
  if (!existing) return;
  if (!token || existing.token !== token) {
    log("Handshake belongs to another window; left in place.");
    return;
  }
  try {
    fs.unlinkSync(HANDSHAKE_FILE);
    log("Handshake removed.");
  } catch {
    /* already gone */
  }
}

function readHandshake() {
  try {
    return JSON.parse(fs.readFileSync(HANDSHAKE_FILE, "utf8"));
  } catch {
    return null;
  }
}

/** Is something accepting connections at this address right now? */
function isListening(baseUrl) {
  return new Promise((resolve) => {
    let port;
    try {
      port = Number(new URL(baseUrl).port);
    } catch {
      resolve(false);
      return;
    }
    const probe = net.connect({ port, host: "127.0.0.1" });
    const done = (answer) => {
      probe.destroy();
      resolve(answer);
    };
    probe.setTimeout(400);
    probe.on("connect", () => done(true));
    probe.on("timeout", () => done(false));
    probe.on("error", () => done(false));
  });
}

function startServer() {
  const preferred = Number(config().get("port") ?? 8761);
  return listen(preferred).catch(async (error) => {
    if (error.code !== "EADDRINUSE") throw error;

    // Someone holds the port. If it is another window's bridge, this window has
    // nothing to add and stands down. If not — a stale socket VS Code kept
    // alive across a reload, or an unrelated program — the bridge must not be
    // held hostage by a port number. The handshake carries the address, so any
    // free port works just as well.
    const existing = readHandshake();
    if (existing?.base_url && (await isListening(existing.base_url))) {
      standby = true;
      log(`Port ${preferred} is serving another window's bridge; standing down.`);
      return null;
    }
    log(`Port ${preferred} is held by something that is not the bridge; using a free port.`);
    return listen(0);
  });
}

function listen(port) {
  return new Promise((resolve, reject) => {
    const attempt = crypto.randomBytes(32).toString("hex");

    const candidate = http.createServer(async (request, response) => {
      if (fromBrowser(request)) {
        send(response, 403, { error: "Browser-originated requests are not accepted." });
        return;
      }
      if (!authorised(request)) {
        send(response, 401, { error: "Missing or invalid bearer token." });
        return;
      }

      const url = (request.url || "").split("?")[0];
      if (request.method === "GET" && url === "/health") {
        await handleHealth(response);
      } else if (request.method === "POST" && url === "/v1/chat/completions") {
        await handleChat(request, response);
      } else {
        send(response, 404, { error: `No route for ${request.method} ${url}` });
      }
    });

    candidate.on("error", reject);
    candidate.listen(port, "127.0.0.1", () => {
      server = candidate;
      token = attempt;
      standby = false;
      const actual = candidate.address().port;
      writeHandshake(actual);
      log(`Listening on http://127.0.0.1:${actual}`);
      resolve(actual);
    });
  });
}

function stopServer() {
  removeHandshake();
  token = "";
  if (server) {
    // close() alone waits for keep-alive clients, which can hold the port past
    // a window reload and lock the next activation out of it.
    server.closeAllConnections?.();
    server.close();
    server = null;
  }
}

/**
 * Keeps the bridge reachable without the user having to notice it is not.
 *
 * Two things it repairs: a handshake removed by another window shutting down,
 * and this window standing down for a port that has since been given up.
 */
function startWatchdog(start) {
  if (watchdog) return;
  watchdog = setInterval(() => {
    try {
      if (server) {
        if (!fs.existsSync(HANDSHAKE_FILE)) {
          log("Handshake had gone; writing it again.");
          writeHandshake(server.address().port);
          updateStatus();
        }
        return;
      }
      if (standby) start();
    } catch (error) {
      log(`Watchdog: ${error.message}`);
    }
  }, WATCHDOG_MS);
}

function stopWatchdog() {
  if (watchdog) clearInterval(watchdog);
  watchdog = null;
}

function updateStatus() {
  if (!statusBar) return;
  const port = server?.address()?.port;
  if (port) {
    statusBar.text = "$(broadcast) ClipDesk";
    statusBar.tooltip = `ClipDesk Bridge is listening on 127.0.0.1:${port}`;
    statusBar.backgroundColor = undefined;
  } else if (standby) {
    statusBar.text = "$(broadcast) ClipDesk";
    statusBar.tooltip =
      "Another VS Code window is serving the ClipDesk Bridge. This window will " +
      "take over if that one closes.";
    statusBar.backgroundColor = undefined;
  } else {
    statusBar.text = "$(circle-slash) ClipDesk";
    statusBar.tooltip = "ClipDesk Bridge is stopped.";
    statusBar.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
  }
  statusBar.show();
}

// --- activation --------------------------------------------------------------
async function activate(context) {
  output = vscode.window.createOutputChannel("ClipDesk Bridge");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = "clipdeskBridge.status";
  context.subscriptions.push(output, statusBar, { dispose: shutDown });

  const start = async () => {
    try {
      const port = await startServer();
      updateStatus();
      return port;
    } catch (error) {
      updateStatus();
      vscode.window.showErrorMessage(
        `ClipDesk Bridge could not start: ${error.message}. Change clipdeskBridge.port ` +
          "in settings, or set it to 0 to pick a free port."
      );
      return null;
    }
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("clipdeskBridge.authorize", async () => {
      // Consent is granted through a dialog that only appears on a user-initiated
      // action, which is why this is a command rather than something automatic.
      const model = await pickModel();
      if (!model) {
        vscode.window.showWarningMessage(
          "No Copilot chat models are available. Sign in to GitHub Copilot first."
        );
        return;
      }
      try {
        const response = await model.sendRequest(
          [vscode.LanguageModelChatMessage.User("Reply with the single word: ready")],
          {},
          new vscode.CancellationTokenSource().token
        );
        for await (const _ of response.text) break;
        consented = true;
        updateStatus();
        vscode.window.showInformationMessage(
          `ClipDesk Bridge is authorised to use ${model.name}.`
        );
      } catch (error) {
        vscode.window.showErrorMessage(`Authorisation failed: ${error.message}`);
      }
    }),

    vscode.commands.registerCommand("clipdeskBridge.status", async () => {
      const models = await listModels();
      const port = server?.address()?.port;
      const choice = await vscode.window.showInformationMessage(
        port
          ? `ClipDesk Bridge is listening on 127.0.0.1:${port}. ` +
              `${models.length} Copilot model(s) available.`
          : "ClipDesk Bridge is not running.",
        "Show log",
        "Restart"
      );
      if (choice === "Show log") output.show();
      if (choice === "Restart") vscode.commands.executeCommand("clipdeskBridge.restart");
    }),

    vscode.commands.registerCommand("clipdeskBridge.restart", async () => {
      stopServer();
      await start();
      vscode.window.showInformationMessage("ClipDesk Bridge restarted.");
    })
  );

  if (config().get("autoStart") !== false) await start();
  else updateStatus();
  startWatchdog(start);
}

function shutDown() {
  stopWatchdog();
  stopServer();
}

function deactivate() {
  shutDown();
}

module.exports = { activate, deactivate };
