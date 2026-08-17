"""Request-shape checks for the localhost VS Code bridge provider."""

from types import SimpleNamespace

from clipdesk.config import VSCodeLLMConfig
from clipdesk.llm.base import ChatMessage
from clipdesk.llm.vscode_bridge import VSCodeBridgeProvider


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

    assert answer == "done"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["context_window_tokens"] == 128000