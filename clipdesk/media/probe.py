"""ffprobe → :class:`MediaInfo`."""

from __future__ import annotations

from pathlib import Path

from clipdesk.media.ffmpeg import probe_json
from clipdesk.models import AudioStreamInfo, MediaInfo, VideoStreamInfo


def _fraction(value: str | None) -> float | None:
    """Parse ffprobe's ``30000/1001`` style rational."""
    if not value:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denom = float(denominator)
            return round(float(numerator) / denom, 3) if denom else None
        return float(value)
    except ValueError:
        return None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def probe(source: str | Path, ffprobe_bin: str) -> MediaInfo:
    source = Path(source)
    data = probe_json(ffprobe_bin, source)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    video: VideoStreamInfo | None = None
    audios: list[AudioStreamInfo] = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and video is None:
            # Cover art is stored as a video stream; a real one has a frame rate.
            if stream.get("disposition", {}).get("attached_pic"):
                continue
            video = VideoStreamInfo(
                codec=stream.get("codec_name"),
                width=_int(stream.get("width")),
                height=_int(stream.get("height")),
                fps=_fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
            )
        elif kind == "audio":
            audios.append(
                AudioStreamInfo(
                    codec=stream.get("codec_name"),
                    sample_rate=_int(stream.get("sample_rate")),
                    channels=_int(stream.get("channels")),
                    language=(stream.get("tags") or {}).get("language"),
                )
            )

    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    return MediaInfo(
        path=str(source),
        filename=source.name,
        container=fmt.get("format_name"),
        duration_s=duration,
        size_bytes=_int(fmt.get("size")) or (source.stat().st_size if source.is_file() else 0),
        video=video,
        audios=audios,
    )
