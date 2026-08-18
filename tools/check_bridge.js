// Checks the VS Code bridge keeps its handshake file when more than one window
// is open. Every window runs its own copy of the extension, and a window that
// never owned the bridge must not delete the handshake belonging to one that
// does — that failure leaves the bridge listening but unreachable.
//
//   node tools\check_bridge.js
//
// Takes about 25 seconds; most of that is waiting for the watchdog. It runs
// against a temporary home directory and an unused port, so it is safe to run
// while ClipDesk and VS Code are up.

const Module = require("node:module");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const PORT = 8799;
const HOME = fs.mkdtempSync(path.join(os.tmpdir(), "clipdesk-bridge-check-"));
const HANDSHAKE = path.join(HOME, ".clipdesk", "bridge.json");
const EXTENSION = path.resolve(__dirname, "..", "vscode-bridge", "extension.js");

const errors = [];

const vscodeStub = {
  version: "1.99.0-test",
  StatusBarAlignment: { Right: 2 },
  ThemeColor: class {
    constructor(id) {
      this.id = id;
    }
  },
  window: {
    createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => ({ show() {}, dispose() {} }),
    showErrorMessage: (message) => {
      errors.push(message);
      return Promise.resolve();
    },
    showInformationMessage: () => Promise.resolve(),
    showWarningMessage: () => Promise.resolve(),
  },
  workspace: { getConfiguration: () => ({ get: (key) => (key === "port" ? PORT : undefined) }) },
  commands: { registerCommand: () => ({ dispose() {} }), executeCommand: () => {} },
  lm: { selectChatModels: async () => [] },
};

// The extension reads os.homedir() to place the handshake. Redirecting it keeps
// this check away from the one the user's own VS Code is relying on.
const osStub = { ...os, homedir: () => HOME };

const load = Module._load;
Module._load = (request, ...rest) => {
  if (request === "vscode") return vscodeStub;
  if (request === "node:os" || request === "os") return osStub;
  return load.call(Module, request, ...rest);
};

function freshWindow() {
  delete require.cache[require.resolve(EXTENSION)];
  return require(EXTENSION);
}

const checks = [];
const check = (name, ok) => checks.push([name, ok]);
const handshake = () =>
  fs.existsSync(HANDSHAKE) ? JSON.parse(fs.readFileSync(HANDSHAKE, "utf8")) : null;

(async () => {
  const windowA = freshWindow();
  await windowA.activate({ subscriptions: [] });
  check("the first window writes the handshake", handshake() !== null);
  const owner = handshake()?.token;

  const windowB = freshWindow();
  await windowB.activate({ subscriptions: [] });
  check("a second window does not report an error", errors.length === 0);
  check("a second window leaves the handshake alone", handshake()?.token === owner);

  windowB.deactivate();
  check("closing the second window keeps the bridge up", handshake()?.token === owner);

  fs.rmSync(HANDSHAKE, { force: true });
  process.stdout.write("  waiting for the watchdog…\n");
  await new Promise((resolve) => setTimeout(resolve, 22000));
  check("the owner puts a deleted handshake back", handshake() !== null);

  windowA.deactivate();
  check("the owner removes its own handshake on shutdown", handshake() === null);

  // A socket VS Code kept alive across a reload holds the port but serves no
  // bridge. The bridge has to come up anyway, on whatever port is free.
  const squatter = net.createServer();
  await new Promise((resolve) => squatter.listen(PORT, "127.0.0.1", resolve));

  const windowC = freshWindow();
  await windowC.activate({ subscriptions: [] });
  const stranded = handshake();
  check("a blocked port does not stop the bridge", stranded !== null);
  check(
    "it says where it actually landed",
    stranded !== null && !stranded.base_url.endsWith(`:${PORT}`)
  );
  check("no error is shown for a blocked port", errors.length === 0);
  windowC.deactivate();
  await new Promise((resolve) => squatter.close(resolve));

  for (const [name, ok] of checks) process.stdout.write(`  ${ok ? "ok  " : "FAIL"} ${name}\n`);
  fs.rmSync(HOME, { recursive: true, force: true });
  process.exit(checks.every(([, ok]) => ok) ? 0 : 1);
})();
