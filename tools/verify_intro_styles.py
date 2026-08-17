"""Render every bundled intro style at low resolution to prove each graph builds."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipdesk.actions.intro import BUILT_IN_STYLES, CATALOG_STYLES, plan_intro, render_intro
from clipdesk.config import RenderConfig
from clipdesk.media.soundtrack import prepare_soundtrack

FFMPEG = str(Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe")
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe"))


def main() -> int:
    flags = {word for word in sys.argv[1:] if word.startswith("--")}
    only = {word for word in sys.argv[1:] if not word.startswith("--")}
    styles = [
        style
        for style in (*BUILT_IN_STYLES, *CATALOG_STYLES)
        if not only or style.id in only
    ]
    found = {style.id for style in styles}
    unknown = sorted(only - found)
    if unknown:
        print(f"Unknown style id(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    hd = "--hd" in flags
    render = (
        RenderConfig(width=1920, height=1080, fps=30)
        if hd
        else RenderConfig(width=480, height=270, fps=15, preset="ultrafast", crf=32)
    )
    keep = Path(__file__).resolve().parents[1] / "workspace" / ".intro-verify" if hd else None
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="intro-verify-"))
    try:
        source = work / "source.mp4"
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15:duration=40",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=40",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(source),
            ],
            check=True,
        )
        failures: list[str] = []
        for style in styles:
            plan = plan_intro(
                style,
                total_seconds=8.0,
                shot_count=min(style.default_shots, 4),
                source_duration=40.0,
                subtitle="A short overview line for the recording",
            )
            bed = prepare_soundtrack("calm", plan.total_seconds + 1, work / "bed.wav", FFMPEG)
            destination = (keep or work) / f"{style.id}.mp4"
            try:
                render_intro(
                    source,
                    destination,
                    plan,
                    style=style,
                    render=render,
                    ffmpeg_bin=FFMPEG,
                    soundtrack_path=bed,
                    title="Quarterly platform review: 100% rollout plan",
                    subtitle="What changed, what shipped and what's next: %{literal}",
                    end_card_text="Watch the full session",
                )
                encoded_seconds = float(
                    subprocess.check_output(
                        [
                            FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(destination),
                        ],
                        text=True,
                    ).strip()
                )
                if abs(encoded_seconds - plan.total_seconds) > 0.12:
                    raise RuntimeError(
                        f"encoded duration {encoded_seconds:.3f}s != plan {plan.total_seconds:.3f}s"
                    )
                print(f"  ok   {style.id}")
            except Exception as error:  # noqa: BLE001 - report every failure, not the first
                failures.append(style.id)
                print(f"  FAIL {style.id}: {str(error)[:600]}")
        print(f"\n{len(styles) - len(failures)}/{len(styles)} styles rendered")
        return 1 if failures else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
