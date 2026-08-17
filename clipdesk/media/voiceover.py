"""Optional offline narration using the speech engine built into Windows."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

_SCRIPT = r"""param([string]$TextPath, [string]$OutputPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SetOutputToWaveFile($OutputPath)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
"""


def local_voiceover_available() -> bool:
    return sys.platform == "win32" and shutil.which("powershell.exe") is not None


@lru_cache(maxsize=1)
def available_voices() -> tuple[str, ...]:
    if not local_voiceover_available():
        return ()
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try {$s.GetInstalledVoices() | ForEach-Object {$_.VoiceInfo.Name}} finally {$s.Dispose()}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def synthesize_voiceover(text: str, destination: Path, voice_id: str = "") -> Path:
    """Write spoken ``text`` to a WAV without evaluating any user-provided script."""
    if not local_voiceover_available():
        raise RuntimeError("Local voiceover is not available on this machine.")
    clean = " ".join(text.split()).strip()
    if not clean:
        raise ValueError("Voiceover text is empty.")
    voices = available_voices()
    if voice_id and voice_id not in voices:
        raise ValueError("That Windows narration voice is not available.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipdesk-voiceover-") as name:
        root = Path(name)
        text_path = root / "narration.txt"
        script_path = root / "speak.ps1"
        text_path.write_text(clean, encoding="utf-8")
        script = _SCRIPT.replace(
            "$synth.SetOutputToWaveFile($OutputPath)",
            (f"$synth.SelectVoice('{voice_id.replace(chr(39), chr(39) * 2)}')\n    " if voice_id else "")
            + "$synth.SetOutputToWaveFile($OutputPath)",
        )
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [
                shutil.which("powershell.exe") or "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                str(text_path),
                str(destination),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    if result.returncode != 0 or not destination.is_file():
        detail = (result.stderr or result.stdout or "Voiceover generation failed.").strip()
        raise RuntimeError(detail[-500:])
    return destination
