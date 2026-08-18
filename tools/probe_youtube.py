"""Work out which yt-dlp settings can actually fetch YouTube media right now.

YouTube changes what it demands often enough that "it broke" is rarely
actionable on its own: the extractor may need a JavaScript runtime, a different
player client, or a signed-in session, and the failure looks the same from the
outside.

This reports the resolution as well as success, because a client list can
succeed and still be wrong -- some clients only offer 360p for a given video, so
"it downloaded" is not the same as "it downloaded what you asked for".

    .\\.venv\\Scripts\\python.exe tools\\probe_youtube.py [URL]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR = ROOT / "vendor" / "ytdlp" / "yt-dlp.pyz"
FFMPEG = ROOT / "vendor" / "ffmpeg" / "bin"
DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# What ClipDesk itself asks for, so the probe measures the real thing.
FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"

CANDIDATES = [
    "",
    "mweb",
    "tv_simply",
    "web_embedded",
    "android",
    "web",
    "web_safari",
    "ios",
    "tv",
    "mweb,tv_simply,web_embedded,android",
]


def attempt(url: str, client: str, runtime: str) -> tuple[bool, str]:
    """Fetch the opening seconds and report the resolution, or why not."""
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw)
        command = [
            sys.executable,
            str(EXTRACTOR),
            "--no-playlist",
            "--no-part",
            "--no-warnings",
            "-f",
            FORMAT,
            "--merge-output-format",
            "mp4",
            # Only the opening seconds: enough to prove the media URL serves
            # bytes without pulling a whole recording for every candidate.
            "--download-sections",
            "*0-4",
            "-o",
            str(target / "%(id)s.%(ext)s"),
            "--print",
            "after_move:GOT:%(width)sx%(height)s",
        ]
        if FFMPEG.is_dir():
            command += ["--ffmpeg-location", str(FFMPEG)]
        if runtime:
            command += ["--js-runtimes", f"{runtime}:{shutil.which(runtime)}"]
        if client:
            command += ["--extractor-args", f"youtube:player_client={client}"]
        command.append(url)

        done = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

    for line in done.stdout.splitlines():
        if line.startswith("GOT:"):
            return True, line[4:].strip()
    errors = [line for line in (done.stdout + done.stderr).splitlines() if "ERROR" in line]
    return False, (errors[0] if errors else "no output").strip()[:70]


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if not EXTRACTOR.is_file():
        print(f"Extractor not found at {EXTRACTOR}")
        return 1

    runtime = next((name for name in ("deno", "node", "bun", "quickjs") if shutil.which(name)), "")
    print(f"URL     : {url}")
    print(f"Runtime : {runtime or 'NONE -- install Node.js, downloads will 403 without one'}\n")

    for client in CANDIDATES:
        ok, detail = attempt(url, client, runtime)
        label = client or "(extractor default)"
        print(f"  {label:<38} {'OK    ' + detail if ok else 'FAIL  ' + detail}")

    print("\nPick a list that reaches the resolution you expect, then set it in")
    print("config/local.yaml under ingest.youtube_player_clients.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
