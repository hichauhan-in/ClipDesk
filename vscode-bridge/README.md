# ClipDesk Bridge

Lets the local ClipDesk app use your GitHub Copilot seat, through VS Code's
[Language Model API](https://code.visualstudio.com/api/extension-guides/ai/language-model).

## Why this exists

A Copilot subscription is licensed for use through approved clients, not as a
general-purpose API key, and GitHub Models — the old PAT-based inference API —
was retired in July 2026. `vscode.lm` is the sanctioned route: it is the same API
every Copilot-powered extension uses, so quota, policy and telemetry all behave
normally.

This extension does **not** copy or reuse a Copilot token, and does not call any
private endpoint. It forwards a chat request to `vscode.lm` and returns the
answer.

## Install

From the repository root:

```powershell
.\scripts\install-bridge.ps1
```

This copies the folder into VS Code's extensions directory. There is no build
step — it is plain JavaScript, so packaging it into a `.vsix` (which is what
`code --install-extension` requires for a local extension) would mean adding Node
tooling for no benefit. For development, open this folder in VS Code and press
<kbd>F5</kbd> instead.

Restart VS Code, then run **ClipDesk Bridge: Authorise Copilot Access** from the
command palette once. VS Code shows a consent dialog; Copilot models cannot be
used by an extension until you accept it, and that dialog only appears in
response to a command you invoked.

The status bar shows `⟨broadcast⟩ ClipDesk` while the bridge is listening.

To remove it: `.\scripts\install-bridge.ps1 -Uninstall`.

## How ClipDesk finds it

On activation the extension writes `~/.clipdesk/bridge.json`:

```json
{ "base_url": "http://127.0.0.1:8761", "token": "<64 hex chars>", "pid": 1234 }
```

The token is regenerated every session and the file is written with mode `0600`.

## Security

The server is a local HTTP listener, so it is treated as an attack surface:

- **Binds to `127.0.0.1` only.** Never `0.0.0.0`, so it is not reachable from the
  network.
- **Requires the bearer token** on every request, compared in constant time. Any
  other process on the machine that cannot read your home directory cannot spend
  your Copilot quota.
- **Rejects browser-originated requests** — anything carrying `Origin` or
  `Sec-Fetch-Site` is refused, which closes off DNS rebinding from a malicious
  web page.
- **Caps the request body** at 8 MB.

## Endpoints

Both require `Authorization: Bearer <token>`.

### `GET /health`

```json
{
  "ok": true,
  "copilot_available": true,
  "consented": true,
  "models": ["gpt-4o", "gpt-4.1", "claude-3.5-sonnet"],
  "default_model": "gpt-4o",
  "vscode_version": "1.99.0"
}
```

### `POST /v1/chat/completions`

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "..." },
    { "role": "user", "content": "..." }
  ]
}
```

Responds in the OpenAI shape:

```json
{ "choices": [{ "message": { "role": "assistant", "content": "..." } }] }
```

The Language Model API has no system-message channel, so a system message is
folded into the first user turn — the documented approach.

Before sending, the prompt is counted with `model.countTokens` and rejected with
`413` and a useful message if it exceeds `model.maxInputTokens`, rather than
letting the model silently truncate it.

| Status | Meaning |
| --- | --- |
| `401` | Missing or wrong bearer token |
| `403` | Consent not granted — run the Authorise command |
| `413` | Prompt exceeds the model's context window |
| `422` | Copilot declined (content filter) |
| `502` | The model request failed |
| `503` | No Copilot models — sign in to Copilot in VS Code |

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `clipdeskBridge.port` | `8761` | Listen port. `0` picks a free one. |
| `clipdeskBridge.modelFamily` | `""` | Preferred model family, e.g. `gpt-4o`. Empty uses the first available. |
| `clipdeskBridge.autoStart` | `true` | Start with VS Code. |

## Commands

- **ClipDesk Bridge: Authorise Copilot Access** — triggers the one-time consent dialog
- **ClipDesk Bridge: Show Status** — port, model count, log
- **ClipDesk Bridge: Restart Server** — after changing the port

## Limitations

- A VS Code window must be open. Closing VS Code stops the bridge and removes the
  handshake file.
- Requests count against your normal Copilot quota.
- Model availability follows whatever your Copilot plan exposes to extensions;
  handle an empty model list gracefully rather than assuming a specific model.
