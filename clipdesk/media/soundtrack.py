"""File-backed soundtracks for intros/outros: curated CC0 music or saved audio.

Source-recording audio is never used in an intro — sampled speech from unrelated
moments is incoherent — so every intro gets a bed from here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from clipdesk.media.ffmpeg import run

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

BUILT_IN_AUDIO = (
    {
        "id": "elevate",
        "name": "Elevate",
        "description": "Polished, optimistic lift for explainers and team updates.",
        "filename": "Elevate Inspirate.mp3",
    },
    {
        "id": "backbeat",
        "name": "Backbeat",
        "description": "Clean rhythmic drive that sits comfortably under titles.",
        "filename": "Backbeat.mp3",
    },
    {
        "id": "inspiration",
        "name": "Inspiration",
        "description": "Broad uplifting theme for recaps, thanks, and next steps.",
        "filename": "Inspiration.mp3",
    },
    {
        "id": "keep-moving",
        "name": "Keep moving",
        "description": "Friendly forward rhythm for team and project stories.",
        "filename": "Gotta Keep On Movin.mp3",
    },
    {
        "id": "martini-sunset",
        "name": "Martini sunset",
        "description": "Smooth, restrained lounge character for polished sign-offs.",
        "filename": "Martini Sunset.mp3",
    },
    {
        "id": "be-chillin",
        "name": "Be chillin",
        "description": "Relaxed, even background for a friendly professional tone.",
        "filename": "Be Chillin.mp3",
    },
    {
        "id": "soft-nothing",
        "name": "Soft and nothing",
        "description": "Quiet contemporary background for narration-led updates.",
        "filename": "Soft_and_Furious_-_02_-_Soft_and_Nothing(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "garden-party",
        "name": "Garden party",
        "description": "Light social warmth for team stories and friendly sign-offs.",
        "filename": "TRG_Banks_-_05_-_Garden_party(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "smooth-jacket",
        "name": "Smooth jacket",
        "description": "Reserved, even pacing for polished organizational video.",
        "filename": "TRG_Banks_-_10_-_Smooth_jacket(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "incredible-you",
        "name": "Incredible you",
        "description": "Positive but restrained background for recognition and recaps.",
        "filename": "Soft_and_Furious_-_03_-_Incredible_You(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "water-and-rain",
        "name": "Through water and rain",
        "description": "Gentle forward texture for reflective professional stories.",
        "filename": "Soft_and_Furious_-_05_-_Through_the_water_and_rain(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "evening-journey",
        "name": "Evening journey",
        "description": "Measured background motion for longer internal narratives.",
        "filename": "TRG_Banks_-_02_-_Evening_journey(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "first-stop",
        "name": "First stop",
        "description": "Light forward pacing for openings, chapters, and recaps.",
        "filename": "TRG_Banks_-_06_-_First_stop(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "back-on-road",
        "name": "Back on the road",
        "description": "Steady positive movement for progress and milestone videos.",
        "filename": "TRG_Banks_-_09_-_Back_on_the_road(chosic.com).mp3",
        "folder": "chosic.com",
    },
    {
        "id": "lucidity",
        "name": "Lucidity",
        "description": "Clear, restrained texture for professional summaries.",
        "filename": "Soft_and_Furious_-_03_-_Lucidity(chosic.com).mp3",
        "folder": "chosic.com",
    },
)

_SOURCE_ROOT = "https://raw.githubusercontent.com/SoundSafari/CC0-1.0-Music/main"
_MAX_BUILT_IN_BYTES = 12 * 1024 * 1024


def _public_track(item: dict[str, str]) -> dict[str, str]:
    folder = item.get("folder", "freepd.com")
    return {
        "id": item["id"],
        "name": item["name"],
        "description": item["description"],
        "license": "CC0-1.0",
        "source_url": f"{_SOURCE_ROOT}/{folder}/{item['filename'].replace(' ', '%20')}",
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
    return {"built_in": [_public_track(item) for item in BUILT_IN_AUDIO], "imported": imported}


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
    if audio_id.startswith("imported:"):
        name = audio_id.removeprefix("imported:")
        directory = audio_dir(state_dir).resolve()
        candidate = (directory / name).resolve()
        if (
            candidate.parent != directory
            or not candidate.is_file()
            or candidate.suffix.lower() not in AUDIO_SUFFIXES
        ):
            raise ValueError("That saved intro/outro audio does not exist.")
        return candidate

    item = next((track for track in BUILT_IN_AUDIO if track["id"] == audio_id), None)
    if item is None:
        raise ValueError(f"No intro/outro soundtrack named '{audio_id}'.")
    cache = audio_dir(state_dir) / ".built-in"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{audio_id}.mp3"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination

    temporary = destination.with_suffix(".download")
    request = Request(_public_track(item)["source_url"], headers={"User-Agent": "ClipDesk/1"})
    written = 0
    try:
        with urlopen(request, timeout=30) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > _MAX_BUILT_IN_BYTES:
                    raise ValueError("The built-in soundtrack download is unexpectedly large.")
                handle.write(chunk)
        if written == 0:
            raise ValueError("The built-in soundtrack download was empty.")
        temporary.replace(destination)
    except (HTTPError, URLError, OSError) as exc:
        raise ValueError(
            "The built-in soundtrack could not be downloaded. Choose a saved preset or try again online."
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return destination


PREVIEW_SECONDS = 7.0


def preview_audio(state_dir: Path, audio_id: str, ffmpeg_bin: str) -> Path:
    """A short sample of one soundtrack, rendered once and then reused.

    Built-in tracks are downloaded once and cached. Every track is trimmed to
    the same length so a large file never streams in full just to be auditioned.
    """
    cache = audio_dir(state_dir) / ".previews"
    cache.mkdir(parents=True, exist_ok=True)
    source = resolve_audio(state_dir, audio_id)
    if audio_id.startswith("imported:"):
        stamp = int(source.stat().st_mtime)
        destination = cache / f"imported-{source.stem}-{stamp}.mp3"
    else:
        destination = cache / f"{audio_id}.mp3"
    if destination.is_file():
        return destination

    scratch = destination.with_suffix(".tmp.wav")
    prepare_soundtrack(
        audio_id, PREVIEW_SECONDS, scratch, ffmpeg_bin, imported=source
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
    """Normalize one file-backed track to the exact requested duration."""
    duration = max(1.0, duration_seconds)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(0.0, duration - min(1.0, duration / 4))
    common = (
        f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
        "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "loudnorm=I=-20:TP=-2:LRA=7,"
        f"afade=t=in:st=0:d={min(.5, duration / 4):.3f},"
        f"afade=t=out:st={fade_out:.3f}:d={min(1.0, duration / 4):.3f}"
    )
    if audio_id == "none":
        args = [
            "-f", "lavfi", "-i",
            f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration:.3f}",
            "-af", common,
        ]
    elif imported is None:
        raise ValueError(f"No file was provided for soundtrack '{audio_id}'.")
    else:
        args = ["-stream_loop", "-1", "-i", str(imported), "-af", common]
    args += ["-y", "-c:a", "pcm_s16le", str(destination)]
    run(ffmpeg_bin, args, timeout=max(30.0, duration * 3))
    return destination
