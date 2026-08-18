"""Talk to Copilot through the ClipDesk Bridge VS Code extension.

This is the sanctioned way to use a Copilot seat from your own code: the
extension calls VS Code's Language Model API (``vscode.lm``), which is the same
API every Copilot-powered extension uses. Nothing here impersonates a Copilot
client or touches a private endpoint.

The extension writes a handshake file containing the port and a per-session
bearer token. Requiring that token means another process on the machine cannot
quietly spend your Copilot quota.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from clipdesk.config import VSCodeLLMConfig
from clipdesk.llm.base import (
    ChatMessage,
    Completion,
    LLMError,
    LLMUnavailableError,
    ProviderStatus,
    estimate_usage,
    usage_from_payload,
)
from clipdesk.paths import user_state_dir

SETUP_HINT = (
    "Run .\\install-bridge.ps1, restart VS Code, then run 'ClipDesk Bridge: "
    "Authorise Copilot Access' from the command palette. A VS Code window signed "
    "in to GitHub Copilot must stay open."
)

EXTENSION_ID = "clipdesk.clipdesk-bridge"

#: The bridge this build of ClipDesk expects VS Code to be running. Kept in step
#: with vscode-bridge/package.json.
BRIDGE_VERSION = "0.1.5"


def _version_parts(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_older(running: str, expected: str) -> bool:
    """True when VS Code is executing an older bridge than the installed one.

    A blank version means a build from before the bridge reported one at all,
    which is by definition older.
    """
    if not running:
        return True
    return _version_parts(running) < _version_parts(expected)


def extension_state(handshake_file: str | None = None) -> dict[str, object]:
    """Where the bridge has got to, so the UI can give the right instruction.

    "Not installed", "installed but VS Code has not loaded it" and "was running,
    now gone" need three different fixes, and guessing between them wastes the
    user's time.
    """
    installed: list[str] = []
    for root in (".vscode", ".vscode-insiders"):
        extensions = Path.home() / root / "extensions"
        if not extensions.is_dir():
            continue
        installed += [
            entry.name.rsplit("-", 1)[-1]
            for entry in extensions.iterdir()
            if entry.is_dir() and entry.name.startswith(EXTENSION_ID)
        ]

    path = (
        Path(handshake_file).expanduser()
        if handshake_file
        else user_state_dir() / "bridge.json"
    )
    return {
        "extension_installed": bool(installed),
        "extension_version": installed[0] if installed else "",
        "handshake_present": path.is_file(),
        "handshake_path": str(path),
    }


class VSCodeBridgeProvider:
    key = "vscode"
    label = "GitHub Copilot (via VS Code bridge)"

    def __init__(self, config: VSCodeLLMConfig) -> None:
        self.config = config

    # --- handshake ---------------------------------------------------------
    def _handshake_path(self) -> Path:
        if self.config.handshake_file:
            return Path(self.config.handshake_file).expanduser()
        return user_state_dir() / "bridge.json"

    def _endpoint(self) -> tuple[str, str]:
        """Return ``(base_url, token)``, preferring explicit config."""
        if self.config.base_url:
            return self.config.base_url.rstrip("/"), self.config.token or ""

        path = self._handshake_path()
        if not path.is_file():
            raise LLMUnavailableError(
                f"The VS Code bridge is not running (no handshake file at {path}). {SETUP_HINT}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMUnavailableError(f"Bridge handshake file is unreadable: {exc}") from exc

        base_url = str(data.get("base_url") or "").rstrip("/")
        token = str(data.get("token") or "")
        if not base_url:
            raise LLMUnavailableError("Bridge handshake file does not contain a base_url.")
        return base_url, token

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    # --- provider interface ------------------------------------------------
    def status(self) -> ProviderStatus:
        try:
            base_url, token = self._endpoint()
        except LLMUnavailableError as exc:
            return ProviderStatus(self.key, self.label, False, str(exc), setup_hint=SETUP_HINT)

        try:
            response = httpx.get(
                f"{base_url}/health", headers=self._headers(token), timeout=5.0
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(
                self.key,
                self.label,
                False,
                f"Bridge did not respond at {base_url}: {exc}",
                setup_hint=SETUP_HINT,
            )

        models = [str(m) for m in payload.get("models") or []]
        active = self.config.model or str(payload.get("default_model") or "")
        running = str(payload.get("bridge_version") or "")
        if _is_older(running, BRIDGE_VERSION):
            # VS Code caches the extension module, so installing a new bridge
            # changes the file without changing what is executing. The visible
            # symptom is silent: token counts quietly fall back to estimates.
            return ProviderStatus(
                self.key,
                self.label,
                True,
                f"Connected to VS Code at {base_url}, but it is running bridge "
                f"{running or 'from before versions were reported'} while "
                f"{BRIDGE_VERSION} is installed. Reload the VS Code window "
                "(Developer: Reload Window) to pick it up — until then token "
                "counts are estimated rather than measured.",
                models=models,
                active_model=active,
            )
        if not payload.get("copilot_available", True):
            return ProviderStatus(
                self.key,
                self.label,
                False,
                "The bridge is running but VS Code reports no Copilot models. Sign in "
                "to GitHub Copilot in VS Code and try again.",
                models=models,
                setup_hint=SETUP_HINT,
            )
        if not payload.get("consented", True):
            return ProviderStatus(
                self.key,
                self.label,
                False,
                "Waiting for consent — run 'ClipDesk Bridge: Authorise Copilot Access' "
                "from the VS Code command palette once.",
                models=models,
                setup_hint=SETUP_HINT,
            )
        return ProviderStatus(
            self.key,
            self.label,
            True,
            f"Connected to VS Code at {base_url}",
            models=models,
            active_model=active,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        expect_json: bool = False,
        model: str | None = None,
    ) -> Completion:
        base_url, token = self._endpoint()
        body: dict[str, Any] = {
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
        }
        chosen = model or self.config.model
        if chosen:
            body["model"] = chosen
        if self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        if self.config.context_window_tokens:
            body["context_window_tokens"] = self.config.context_window_tokens
        if max_tokens:
            body["max_tokens"] = max_tokens

        try:
            response = httpx.post(
                f"{base_url}/v1/chat/completions",
                headers=self._headers(token),
                json=body,
                timeout=httpx.Timeout(15.0, read=self.config.request_timeout_s),
            )
        except httpx.RequestError as exc:
            raise LLMUnavailableError(
                f"Lost contact with the VS Code bridge: {exc}. Is the VS Code window still open?"
            ) from exc

        if response.status_code == 401:
            raise LLMUnavailableError(
                "The bridge rejected the token. Restart VS Code so a fresh handshake "
                "file is written, then try again."
            )
        if response.status_code >= 400:
            raise LLMError(f"Bridge error {response.status_code}: {response.text[:400]}")

        payload = response.json()
        if payload.get("error"):
            raise LLMError(str(payload["error"]))
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not content:
            raise LLMError("The bridge returned an empty response.")
        text = str(content)
        # The extension counts with the model's own tokenizer, so prefer that.
        return Completion(text, usage_from_payload(payload.get("usage")) or estimate_usage(messages, text))
