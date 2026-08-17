"""Measure how reliably a provider returns usable JSON for a real analysis window.

    .venv\\Scripts\\python.exe tools\\bench_llm.py [provider] [runs]

Not a unit test — it makes real model calls and costs quota. Useful when tuning
prompts or comparing providers.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipdesk.analysis.prompts import ANALYST_SYSTEM, WINDOW_USER_TEMPLATE
from clipdesk.analysis.windows import build_windows, format_timestamp
from clipdesk.config import load_settings
from clipdesk.llm import LLMClient, build_provider
from clipdesk.store import ProjectStore


def main() -> int:
    settings = load_settings()
    provider_key = sys.argv[1] if len(sys.argv) > 1 else settings.llm.provider
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    store = ProjectStore(settings.paths.workspace_dir)
    projects = store.list()
    if not projects:
        print("No analysed project in the workspace to benchmark against.")
        return 1

    report = store.require(projects[0].id).load_analysis()
    if report is None or not report.transcript.segments:
        print("That project has no transcript yet.")
        return 1

    window = build_windows(
        report.transcript.segments,
        window_chars=settings.analysis.window_chars,
        overlap_chars=settings.analysis.window_overlap_chars,
    )[0]
    user = WINDOW_USER_TEMPLATE.format(
        title=report.title,
        window_index=1,
        window_count=1,
        start=format_timestamp(window.start),
        end=format_timestamp(window.end),
        context="",
        transcript=window.render(),
    )

    # json_retries=0 measures the first-attempt hit rate, which is what matters
    # for cost — retries multiply quota use.
    client = LLMClient(build_provider(settings.llm, provider_key), json_retries=0)
    print(f"provider : {client.provider.label}")
    print(f"runs     : {runs}\n")

    successes = 0
    durations: list[float] = []
    for index in range(1, runs + 1):
        started = time.time()
        try:
            payload = client.complete_json(ANALYST_SYSTEM, user, temperature=0.1)
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            durations.append(elapsed)
            print(f"  run {index}: FAIL  {elapsed:5.1f}s  {exc}")
            continue
        elapsed = time.time() - started
        durations.append(elapsed)
        successes += 1
        counts = " ".join(
            f"{key}={len(payload.get(key) or [])}"
            for key in ("segments", "chapters", "clips", "action_items", "decisions")
        )
        print(f"  run {index}: OK    {elapsed:5.1f}s  {counts}")

    print(
        f"\n  {successes}/{runs} first-attempt successes · "
        f"median {statistics.median(durations):.1f}s"
    )
    return 0 if successes == runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
