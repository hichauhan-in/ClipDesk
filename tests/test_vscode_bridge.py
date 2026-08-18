"""Request-shape checks for the localhost VS Code bridge provider."""

from types import SimpleNamespace

import pytest

from clipdesk.config import VSCodeLLMConfig
from clipdesk.llm.base import ChatMessage
from clipdesk.llm.vscode_bridge import BRIDGE_VERSION, VSCodeBridgeProvider, _is_older


def test_model_effort_and_context_are_forwarded_to_the_bridge(monkeypatch):
    from clipdesk.llm import vscode_bridge as bridge_module

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs["json"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": "done"}}],
            },
        )

    monkeypatch.setattr(bridge_module.httpx, "post", fake_post)
    provider = VSCodeBridgeProvider(
        VSCodeLLMConfig(
            base_url="http://127.0.0.1:8761",
            token="test-token",
            model="gpt-test",
            reasoning_effort="high",
            context_window_tokens=128000,
        )
    )

    answer = provider.complete([ChatMessage("user", "hello")])

    assert answer.text == "done"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["context_window_tokens"] == 128000
@pytest.mark.parametrize(
    "running, older",
    [
        ("", True),  # predates the bridge reporting a version at all
        ("0.1.4", True),
        ("0.1.5", False),
        ("0.2.0", False),
        ("0.1.10", False),  # compared as numbers, not as text
    ],
)
def test_an_older_bridge_running_in_vs_code_is_spotted(running, older):
    """VS Code caches the extension module until the window is reloaded.

    Installing a new bridge changes the file without changing what executes, and
    the symptom is silent: token counts fall back to estimates with nothing said.
    """
    assert _is_older(running, "0.1.5") is older


def test_the_expected_bridge_version_matches_the_manifest():
    """A bump in one place and not the other would warn forever, or never."""
    import json
    from pathlib import Path

    manifest = Path(__file__).resolve().parent.parent / "vscode-bridge" / "package.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == BRIDGE_VERSION
