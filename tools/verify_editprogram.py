"""Render each kind of prompt edit for real, so a broken filter graph cannot hide."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipdesk.actions.editprogram import parse_edit_program, render_program
from clipdesk.config import RenderConfig

FFMPEG = str(Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe")
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe"))

PROMPTS = [
    ("five-second", "create an edit of five seconds", 5.0),
    ("middle", "create a 10 second video from the middle", 10.0),
    ("speed", "keep the first 20 seconds and speed it up 2x", 10.0),
    ("slow", "keep the first 10 seconds and slow it down by half", 20.0),
    ("vertical", "keep the first 6 seconds and make it vertical for reels", 6.0),
    ("compose", "cut the first 5 seconds, speed it up 1.5x and fade out over 2 seconds", None),
    ("window", "keep the first 12 seconds and make the first 4 seconds black and white", 12.0),
    ("text", 'keep the first 8 seconds and add text "Launch day" bottom right', 8.0),
    ("square", "keep the first 6 seconds, make it square with a cinematic look", 6.0),
    ("rotate", "keep the first 5 seconds, rotate 90 and denoise", 5.0),
    ("audio", "keep the first 6 seconds, mute the audio", 6.0),
    ("volume", "keep the first 6 seconds and set volume to 40%", 6.0),
    ("resize", "keep the first 6 seconds and resize to 720p", 6.0),
    ("mirror", "keep the first 5 seconds and mirror it", 5.0),
]


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True,
    )
    return float(out.strip())


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="editprogram-"))
    try:
        source = work / "source.mp4"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=40",
             "-f", "lavfi", "-i", "sine=frequency=220:duration=40",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(source)],
            check=True,
        )
        render = RenderConfig(width=640, height=360, fps=25, preset="ultrafast", crf=30)
        failures = 0
        for name, prompt, expected in PROMPTS:
            try:
                program = parse_edit_program(prompt, 40.0)
                destination = work / f"{name}.mp4"
                render_program(
                    source, destination, program,
                    has_audio=True, render=render, ffmpeg_bin=FFMPEG,
                )
                actual = probe_duration(destination)
                drift = "" if expected is None else f" (expected {expected:g}s)"
                ok = expected is None or abs(actual - expected) <= 0.6
                if not ok:
                    failures += 1
                print(f"  {'ok  ' if ok else 'FAIL'} {name:10} {actual:6.2f}s{drift}  {prompt}")
            except Exception as error:  # noqa: BLE001 - report every failure
                failures += 1
                print(f"  FAIL {name:10} {str(error)[:300]}")
        print(f"\n{len(PROMPTS) - failures}/{len(PROMPTS)} rendered")
        return 1 if failures else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
