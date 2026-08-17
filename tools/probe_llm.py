"""Dump exactly what the configured model returns for a real analysis window.

Not a test — a debugging aid for when analysis produces empty chapters and the
warning says the model did not return JSON. Run:

    .venv\\Scripts\\python.exe tools\\probe_llm.py <project_id> [provider]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipdesk.analysis.prompts import ANALYST_SYSTEM, WINDOW_USER_TEMPLATE
from clipdesk.analysis.windows import build_windows, format_timestamp
from clipdesk.config import load_settings
from clipdesk.llm import build_provider
from clipdesk.llm.base import JSON_INSTRUCTION, ChatMessage, extract_json
from clipdesk.store import ProjectStore


def main() -> int:
    settings = load_settings()
    project_id = sys.argv[1] if len(sys.argv) > 1 else ""
    provider_key = sys.argv[2] if len(sys.argv) > 2 else settings.llm.provider

    store = ProjectStore(settings.paths.workspace_dir)
    if not project_id:
        projects = store.list()
        if not projects:
            print("No projects in the workspace.")
            return 1
        project_id = projects[0].id

    report = store.require(project_id).load_analysis()
    if report is None:
        print(f"{project_id} has no analysis.json yet.")
        return 1

    windows = build_windows(
        report.transcript.segments,
        window_chars=settings.analysis.window_chars,
        overlap_chars=settings.analysis.window_overlap_chars,
    )
    window = windows[0]
    user = WINDOW_USER_TEMPLATE.format(
        title=report.title,
        window_index=1,
        window_count=len(windows),
        start=format_timestamp(window.start),
        end=format_timestamp(window.end),
        context="",
        transcript=window.render(),
    )

    provider = build_provider(settings.llm, provider_key)
    print(f"provider     : {provider.label}")
    print(f"prompt chars : {len(ANALYST_SYSTEM) + len(user)}")
    print("-" * 70)

    raw = provider.complete(
        [ChatMessage("system", f"{ANALYST_SYSTEM}\n\n{JSON_INSTRUCTION}"), ChatMessage("user", user)],
        temperature=0.1,
        expect_json=True,
    )
    print("RAW RESPONSE")
    print("-" * 70)
    print(raw[:4000])
    print("-" * 70)
    try:
        payload = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"PARSE FAILED: {exc}")
        return 1
    print("PARSED KEYS:", list(payload) if isinstance(payload, dict) else type(payload).__name__)
    for key in ("segments", "chapters", "clips", "action_items", "decisions"):
        value = payload.get(key) if isinstance(payload, dict) else None
        print(f"  {key:<14} {len(value) if isinstance(value, list) else value!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
