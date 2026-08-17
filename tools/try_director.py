"""Exercise the prompt director across every intent it can route to."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipdesk.actions.director import PromptContext, plan_prompt

CONTEXT = PromptContext(
    duration_s=3600,
    media_names=("Pre.mp4", "Post.mp4"),
    output_names=("cleaned.mp4", "final.mp4"),
    style_ids=("prestige", "newsroom", "neon-pulse", "noir-cut", "keynote"),
    title="Comms review",
    has_analysis=True,
)

PROMPTS = [
    'create a cinematic intro that runs 12 seconds titled "Team Sync"',
    "build a social intro with 6 moments",
    'make an outro saying "Thanks for watching"',
    "clean up the recording and remove the pauses",
    "clip from 04:10 to 06:00",
    "find 3 highlights",
    'find the parts about "retry policy"',
    "export cleaned.mp4 as a small mp4",
    "convert final.mp4 to a gif",
    "just the audio as mp3",
    "attach intro Pre.mp4 and outro Post.mp4",
    "trim the first 30 seconds",
    "drop the last 2 minutes",
    "make it sepia and add a vignette",
    'add text "Confidential" bottom right from 00:10 to 00:25',
    "mute audio and make it black and white",
    # Regressions worth keeping an eye on.
    "clean up the recording and remove the intro",
    "convert to mov",
    "compress and remove the noise",
    "clip after 10:00",
]


def main() -> int:
    failures = 0
    for prompt in PROMPTS:
        try:
            plan = plan_prompt(prompt, CONTEXT)
            print(f"  {plan.intent:9} | {prompt}")
            print(f"             -> {plan.summary}")
            for step in plan.steps:
                print(f"                - {step}")
        except ValueError as error:
            failures += 1
            print(f"  FAILED    | {prompt}: {error}")
    print(f"\n{len(PROMPTS) - failures}/{len(PROMPTS)} prompts planned")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
