"""Shared ffmpeg rendering primitives.

Every action that produces video goes through here, so encode settings, the
trim/concat filtergraph and the canvas-fitting logic exist in exactly one place.

All of it re-encodes rather than stream-copying. Stream copy can only cut on
keyframes, which would drift every cut by up to several seconds — unusable when
the whole point is that the cuts follow the transcript.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from clipdesk.config import RenderConfig
from clipdesk.media.ffmpeg import filter_complex_args, run_with_progress

ProgressFn = Callable[[float], None]

Span = tuple[float, float]


def encode_args(render: RenderConfig, *, has_audio: bool) -> list[str]:
    args = [
        "-c:v",
        render.video_codec,
        "-preset",
        render.preset,
        "-crf",
        str(render.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if render.fps:
        args += ["-r", str(render.fps)]
    if has_audio:
        args += ["-c:a", render.audio_codec, "-b:a", render.audio_bitrate, "-ac", "2"]
    else:
        args += ["-an"]
    return args


def _fraction(elapsed: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, elapsed / total))


def render_spans(
    source: Path,
    spans: Sequence[Span],
    dest: Path,
    *,
    has_audio: bool,
    render: RenderConfig,
    ffmpeg_bin: str,
    on_progress: ProgressFn | None = None,
    fade_seconds: float = 0.0,
    fade_min_gap: float = 4.0,
    video_filters: Sequence[str] = (),
) -> Path:
    """Keep only ``spans`` of ``source``, concatenated, in ``dest``.

    One ``filter_complex`` pass trims each span and concatenates the results, so
    the output has clean monotonic timestamps and correct A/V sync no matter how
    many joins there are or where the source keyframes fall.

    When ``fade_seconds`` is set, joins where a long stretch was removed get a
    cross-fade so a big jump reads as an intentional transition instead of a
    glitch. Natural continuations are left hard-cut.
    """
    spans = [(start, end) for start, end in spans if end > start]
    if not spans:
        raise ValueError("Nothing to render — the keep list is empty.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    total = sum(end - start for start, end in spans)
    count = len(spans)

    parts: list[str] = []
    concat_inputs: list[str] = []
    for index, (start, end) in enumerate(spans):
        duration = end - start
        gap_before = start - spans[index - 1][1] if index > 0 else None
        gap_after = spans[index + 1][0] - end if index < count - 1 else None
        fade_in = fade_seconds > 0 and (index == 0 or (gap_before or 0.0) >= fade_min_gap)
        fade_out = (
            fade_seconds > 0 and gap_after is not None and gap_after >= fade_min_gap
        )
        fade = min(fade_seconds, duration / 2)
        if fade <= 0.01:
            fade_in = fade_out = False

        video = f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        if video_filters:
            video += "," + ",".join(video_filters)
        if fade_in:
            video += f",fade=t=in:st=0:d={fade:.3f}"
        if fade_out:
            video += f",fade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
        parts.append(f"{video}[v{index}];")
        concat_inputs.append(f"[v{index}]")

        if has_audio:
            # aresample=async=1 pins audio to the reset timeline so many joins
            # do not accumulate drift against the video.
            audio = (
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
                "asetpts=PTS-STARTPTS,aresample=async=1"
            )
            if fade_in:
                audio += f",afade=t=in:st=0:d={fade:.3f}"
            if fade_out:
                audio += f",afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
            parts.append(f"{audio}[a{index}];")
            concat_inputs.append(f"[a{index}]")

    streams = "v=1:a=1[outv][outa]" if has_audio else "v=1:a=0[outv]"
    parts.append("".join(concat_inputs) + f"concat=n={count}:{streams}")
    graph = "\n".join(parts)

    scratch = dest.parent / f"{dest.stem}.filter.txt"
    fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)

    args = ["-i", str(source), *fc_args, "-map", "[outv]"]
    if has_audio:
        args += ["-map", "[outa]"]
    args += encode_args(render, has_audio=has_audio)
    args += ["-movflags", "+faststart", str(dest)]

    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda elapsed: on_progress(_fraction(elapsed, total)))
            if on_progress
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
    return dest


def fit_filter(width: int, height: int, *, blur_background: bool = False) -> str:
    """Fit the whole frame into a ``width``×``height`` canvas without cropping."""
    if blur_background:
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[outv]"
        )
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[outv]"
    )


def render_clip(
    source: Path,
    span: Span,
    dest: Path,
    *,
    has_audio: bool,
    render: RenderConfig,
    ffmpeg_bin: str,
    reframe: bool = False,
    blur_background: bool = False,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Cut one span, optionally refitting it to the configured canvas."""
    start, end = span
    duration = max(0.1, end - start)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # -ss before -i seeks fast; -accurate_seek keeps the cut frame-exact.
    args = ["-accurate_seek", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]

    if reframe:
        graph = fit_filter(render.width, render.height, blur_background=blur_background)
        scratch = dest.parent / f"{dest.stem}.filter.txt"
        fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)
        args += [*fc_args, "-map", "[outv]"]
        if has_audio:
            args += ["-map", "0:a?"]
    else:
        script = None
        args += ["-map", "0:v:0"]
        if has_audio:
            args += ["-map", "0:a?"]

    args += encode_args(render, has_audio=has_audio)
    args += ["-movflags", "+faststart", str(dest)]

    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda elapsed: on_progress(_fraction(elapsed, duration)))
            if on_progress
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
    return dest


@dataclass(slots=True)
class ConcatInput:
    path: Path
    duration_s: float
    has_audio: bool = True
    start: float = 0.0
    end: float | None = None

    @property
    def effective_duration(self) -> float:
        stop = self.duration_s if self.end is None else min(self.end, self.duration_s)
        return max(0.05, stop - max(0.0, self.start))

    @property
    def trimmed(self) -> bool:
        return self.start > 0.0 or self.end is not None


def concat_normalised(
    inputs: Sequence[ConcatInput],
    dest: Path,
    *,
    render: RenderConfig,
    ffmpeg_bin: str,
    transitions: Sequence[str] | None = None,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Join clips that may differ in resolution, frame rate or audio layout.

    Every input is scaled onto the same canvas and resampled to the same audio
    format before ``concat``, because the demuxer-level concat that avoids
    re-encoding requires the inputs to already match exactly — which an intro
    supplied by a different team never does.

    An input with no audio track borrows a silent stereo source trimmed to its
    own length, so the joined timeline keeps A/V alignment instead of collapsing.
    """
    if not inputs:
        raise ValueError("Nothing to join.")
    dest.parent.mkdir(parents=True, exist_ok=True)

    width, height = render.width, render.height
    fps = render.fps or 30
    silence_index = len(inputs)
    total_duration = sum(item.effective_duration for item in inputs)

    transition_map = {
        "cut": ("fade", 0.001),
        "fade": ("fade", 0.45),
        "dissolve": ("dissolve", 0.55),
        "wipe-left": ("wipeleft", 0.5),
        "slide-left": ("slideleft", 0.5),
    }
    boundary_transitions = list(transitions or [])
    if boundary_transitions and len(boundary_transitions) != len(inputs) - 1:
        raise ValueError("A transition is required for every join.")
    if any(effect not in transition_map for effect in boundary_transitions):
        raise ValueError("Unsupported video transition.")

    parts: list[str] = []
    labels: list[str] = []
    for index, item in enumerate(inputs):
        stop = "" if item.end is None else f":end={item.end:.3f}"
        video_trim = (
            f"trim=start={item.start:.3f}{stop},setpts=PTS-STARTPTS," if item.trimmed else ""
        )
        parts.append(
            f"[{index}:v]{video_trim}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p[v{index}];"
        )
        if item.has_audio:
            source_label = f"[{index}:a]"
            trim = (
                f"atrim=start={item.start:.3f}{stop},asetpts=PTS-STARTPTS,"
                if item.trimmed
                else ""
            )
        else:
            source_label = f"[{silence_index}:a]"
            trim = (
                f"atrim=duration={item.effective_duration:.3f},asetpts=PTS-STARTPTS,"
            )
        parts.append(
            f"{source_label}{trim}aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{index}];"
        )
        labels.append(f"[v{index}][a{index}]")

    if not boundary_transitions:
        parts.append("".join(labels) + f"concat=n={len(inputs)}:v=1:a=1[outv][outa]")
    else:
        video_label = "v0"
        audio_label = "a0"
        elapsed = inputs[0].effective_duration
        for boundary, effect in enumerate(boundary_transitions, start=1):
            ffmpeg_effect, requested_duration = transition_map[effect]
            transition_duration = min(
                requested_duration,
                max(0.001, inputs[boundary - 1].effective_duration / 3),
                max(0.001, inputs[boundary].effective_duration / 3),
            )
            offset = max(0.0, elapsed - transition_duration)
            next_video = "outv" if boundary == len(inputs) - 1 else f"vx{boundary}"
            next_audio = "outa" if boundary == len(inputs) - 1 else f"ax{boundary}"
            parts.append(
                f"[{video_label}][v{boundary}]xfade=transition={ffmpeg_effect}:"
                f"duration={transition_duration:.3f}:offset={offset:.3f}[{next_video}];"
            )
            parts.append(
                f"[{audio_label}][a{boundary}]acrossfade=d={transition_duration:.3f}:"
                f"c1=tri:c2=tri[{next_audio}];"
            )
            elapsed += inputs[boundary].effective_duration - transition_duration
            video_label = next_video
            audio_label = next_audio
    graph = "\n".join(parts)

    scratch = dest.parent / f"{dest.stem}.filter.txt"
    fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)

    args: list[str] = []
    for item in inputs:
        args += ["-i", str(item.path)]
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    args += [*fc_args, "-map", "[outv]", "-map", "[outa]"]
    args += encode_args(render, has_audio=True)
    args += ["-movflags", "+faststart", str(dest)]

    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda elapsed: on_progress(_fraction(elapsed, total_duration)))
            if on_progress and total_duration
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
    return dest
