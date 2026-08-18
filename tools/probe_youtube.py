"""Work out which yt-dlp settings can actually fetch YouTube media right now.

YouTube changes what it demands often enough that "it broke" is rarely
actionable on its own: the extractor may need a JavaScript runtime, a different
player client, or a signed-in session, and the failure looks the same from the
outside. This tries the combinations and reports which of them got bytes.

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
DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

CLIENTS = ["", "tv", "web", "web_safari", "mweb", "ios", "android", "tv_simply", "web_embedded"]


def attempt(url: str, client: str, runtime: str, target: Path) -> tuple[bool, str]:
    command = [
        sys.executable,
        str(EXTRACTOR),
        "--no-playlist",
        "--no-part",
        "--no-warnings",
        "-f",
        "140/worstaudio/worst",
        "-o",
        str(target / "%(id)s.%(ext)s"),
    ]
    if runtime:
        command += ["--js-runtimes", runtime]
    if client:
        command += ["--extractor-args", f"youtube:player_client={client}"]
    command.append(url)

    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode == 0 and any(target.iterdir()):
        return True, ""
    lines = [line for line in (done.stdout + done.stderr).splitlines() if "ERROR" in line]
    return False, (lines[0] if lines else "no output").strip()[:110]


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if not EXTRACTOR.is_file():
        print(f"Extractor not found at {EXTRACTOR}")
        return 1

    runtime = "node" if shutil.which("node") else ""
    print(f"URL      : {url}")
    print(f"Runtime  : {runtime or 'none available'}\n")

    for client in CLIENTS:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw)
            ok, why = attempt(url, client, runtime, target)
        label = client or "(default)"
        print(f"  {label:<14} {'OK' if ok else 'FAIL  ' + why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
