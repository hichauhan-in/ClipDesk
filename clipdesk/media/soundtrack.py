"""Safe local soundtracks for intros: fixed generated themes or imported audio.

Source-recording audio is never used in an intro — sampled speech from unrelated
moments is incoherent — so every intro gets a bed from here.
"""

from __future__ import annotations

from pathlib import Path

from clipdesk.media.ffmpeg import run

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

BUILT_IN_AUDIO = (
    {"id": "bright", "name": "Bright pulse", "description": "Optimistic layered tones for concise recaps."},
    {"id": "calm", "name": "Calm bed", "description": "Soft sustained harmony for measured summaries."},
    {"id": "cinematic", "name": "Cinematic rise", "description": "Low foundation with a gradual sense of lift."},
    {"id": "digital", "name": "Digital signal", "description": "Clean technical pulses for product and demo reels."},
    {"id": "minimal", "name": "Minimal tone", "description": "Restrained neutral sound with little distraction."},
    {"id": "impact", "name": "Impact", "description": "Deep pulsing hits for a bold trailer-style opener."},
    {"id": "uplift", "name": "Uplift", "description": "Rising major intervals that build into the video."},
)

#: Commas are escaped because these become a single ffmpeg option value.
_THEME_EXPRESSIONS = {
    "bright": "0.055*sin(2*PI*220*t)+0.035*sin(2*PI*330*t)+0.025*sin(2*PI*440*t)",
    "calm": "0.045*sin(2*PI*174.61*t)+0.035*sin(2*PI*261.63*t)",
    "cinematic": "0.06*sin(2*PI*110*t)+0.03*sin(2*PI*(165+8*t)*t)",
    "digital": "0.045*sin(2*PI*196*t)+0.025*sin(2*PI*392*t)*if(lt(mod(t\\,1)\\,0.22)\\,1\\,0)",
    "minimal": "0.04*sin(2*PI*146.83*t)+0.018*sin(2*PI*220*t)",
    "impact": "0.07*sin(2*PI*82.41*t)*exp(-mod(t\\,1.5)*2.2)+0.03*sin(2*PI*164.81*t)",
    "uplift": "0.05*sin(2*PI*196*t)+0.04*sin(2*PI*246.94*t)+0.03*sin(2*PI*293.66*t)*min(1\\,t/3)",
}


def audio_dir(state_dir: Path) -> Path:
    directory = state_dir / "intro-audio"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_audio(state_dir: Path) -> dict[str, list[dict[str, object]]]:
    imported = []
    for path in sorted(audio_dir(state_dir).iterdir()):
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
            imported.append(
                {"id": f"imported:{path.name}", "name": path.name, "size_bytes": path.stat().st_size}
            )
    return {"built_in": [dict(item) for item in BUILT_IN_AUDIO], "imported": imported}


def store_audio(source: Path, state_dir: Path, name: str) -> Path:
    suffix = Path(name).suffix.lower()
    if not source.is_file() or suffix not in AUDIO_SUFFIXES:
        raise ValueError("Intro audio must be MP3, WAV, M4A, AAC, FLAC or OGG.")
    directory = audio_dir(state_dir)
    requested = Path(name).name
    destination = directory / requested
    counter = 2
    while destination.exists():
        destination = directory / f"{Path(requested).stem}-{counter}{suffix}"
        counter += 1
    source.replace(destination)
    return destination


def resolve_audio(state_dir: Path, audio_id: str) -> Path:
    if not audio_id.startswith("imported:"):
        raise ValueError("That imported intro audio does not exist.")
    name = audio_id.removeprefix("imported:")
    directory = audio_dir(state_dir).resolve()
    candidate = (directory / name).resolve()
    if (
        candidate.parent != directory
        or not candidate.is_file()
        or candidate.suffix.lower() not in AUDIO_SUFFIXES
    ):
        raise ValueError("That imported intro audio does not exist.")
    return candidate


PREVIEW_SECONDS = 7.0


def preview_audio(state_dir: Path, audio_id: str, ffmpeg_bin: str) -> Path:
    """A short sample of one soundtrack, rendered once and then reused.

    Themes are synthesized rather than stored, so a preview has to be produced
    the first time it is asked for. Imported tracks are trimmed to the same
    length so a large upload never streams in full just to be auditioned.
    """
    cache = audio_dir(state_dir) / ".previews"
    cache.mkdir(parents=True, exist_ok=True)
    imported = resolve_audio(state_dir, audio_id) if audio_id.startswith("imported:") else None
    if imported is not None:
        stamp = int(imported.stat().st_mtime)
        destination = cache / f"imported-{imported.stem}-{stamp}.mp3"
    else:
        if audio_id not in _THEME_EXPRESSIONS:
            raise ValueError(f"No intro audio theme named '{audio_id}'.")
        destination = cache / f"{audio_id}.mp3"
    if destination.is_file():
        return destination

    scratch = destination.with_suffix(".tmp.wav")
    prepare_soundtrack(
        audio_id, PREVIEW_SECONDS, scratch, ffmpeg_bin, imported=imported
    )
    try:
        run(ffmpeg_bin, ["-i", str(scratch), "-c:a", "libmp3lame", "-q:a", "5", "-y", str(destination)])
    finally:
        scratch.unlink(missing_ok=True)
    return destination


def prepare_soundtrack(
    audio_id: str,
    duration_seconds: float,
    destination: Path,
    ffmpeg_bin: str,
    *,
    imported: Path | None = None,
) -> Path:
    """Normalize an imported track or synthesize one fixed theme to exact duration."""
    duration = max(1.0, duration_seconds)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(0.0, duration - min(1.0, duration / 4))
    common = (
        f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
        "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"afade=t=in:st=0:d={min(.5, duration / 4):.3f},"
        f"afade=t=out:st={fade_out:.3f}:d={min(1.0, duration / 4):.3f}"
    )
    if imported is not None:
        args = ["-stream_loop", "-1", "-i", str(imported), "-af", common]
    else:
        expression = _THEME_EXPRESSIONS.get(audio_id)
        if expression is None:
            raise ValueError(f"No intro audio theme named '{audio_id}'.")
        args = [
            "-f", "lavfi", "-i", f"aevalsrc={expression}:s=48000:d={duration:.3f}",
            "-af", common,
        ]
    args += ["-y", "-c:a", "pcm_s16le", str(destination)]
    run(ffmpeg_bin, args, timeout=max(30.0, duration * 3))
    return destination
