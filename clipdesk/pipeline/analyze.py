"""The end-to-end analysis: upload in, ``analysis.json`` out.

Deliberately linear and readable — this is the flow a reader should be able to
follow top to bottom to understand what the app actually does.
"""

from __future__ import annotations

from pathlib import Path

from clipdesk.actions.exports import export_summary, export_transcript
from clipdesk.analysis.analyzer import TranscriptAnalyzer
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.llm import LLMClient
from clipdesk.media.audio import detect_silence, extract_audio
from clipdesk.media.ffmpeg import require_tools
from clipdesk.media.probe import probe
from clipdesk.models import AnalysisReport, Transcript
from clipdesk.store import Project
from clipdesk.transcription import SubtitleParseError, load_transcript
from clipdesk.transcription.base import create_transcriber


def analyze_project(
    project: Project,
    settings: Settings,
    bus: EventBus,
    *,
    llm: LLMClient | None = None,
    keep_audio: bool = False,
) -> AnalysisReport:
    tools = require_tools(settings.paths.vendor_dir)
    source = project.source_path
    if not source.is_file():
        raise FileNotFoundError(f"The uploaded video is missing: {source}")

    project.set_status("analyzing")

    warnings: list[str] = []

    # 1. What are we working with?
    bus.stage_start("probe", f"Reading {source.name}")
    media = probe(source, tools.ffprobe)
    project.record_media(media.duration_s, media.size_bytes)
    bus.stage_end(
        "probe",
        f"{media.duration_s / 60:.1f} minutes"
        + (
            f", {media.video.width}×{media.video.height}"
            if media.video and media.video.width
            else ""
        ),
    )

    # 2. Get a transcript — uploaded if we have one, otherwise speech-to-text.
    transcript = _get_transcript(
        project,
        settings,
        bus,
        media.duration_s,
        media.has_audio,
        tools.ffmpeg,
        warnings,
    )
    if not transcript.segments:
        raise ValueError(
            "No speech was found in this video, so there is nothing to analyse. "
            "If the recording has no narration, upload a transcript alongside it."
        )
    project.save_transcript_checkpoint(transcript)
    bus.stage_end(
        "checkpoint",
        "Transcript ready",
        transcript_ready=True,
        segments=len(transcript.segments),
    )

    # 3. Silence is cheap to detect and drives the cleanup cut.
    bus.stage_start("silence", "Finding silent stretches")
    try:
        silences = (
            detect_silence(
                source,
                ffmpeg_bin=tools.ffmpeg,
                noise_db=settings.media.noise_db,
                min_silence_seconds=settings.media.min_silence_seconds,
            )
            if media.has_audio
            else []
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Silence detection failed: {exc}")
        silences = []
    total_silence = sum(span.duration for span in silences)
    bus.stage_end(
        "silence", f"{len(silences)} silent stretches ({total_silence / 60:.1f} minutes)"
    )

    # 4. Understand it.
    if llm is not None:
        status = llm.status()
        if not status.available:
            warnings.append(f"{status.label} is unavailable: {status.detail}")
            llm = None

    analyzer = TranscriptAnalyzer(settings, llm, bus)
    report = analyzer.analyze(
        project_id=project.id,
        media=media,
        transcript=transcript,
        silences=silences,
        title=project.meta.title,
    )
    report.warnings = warnings + report.warnings

    # 5. Persist, and produce the artifacts that cost nothing to make.
    project.save_analysis(report)
    bus.stage_start("export", "Writing transcript and summary")
    for fmt in ("srt", "vtt", "md"):
        try:
            export_transcript(project, report, fmt)
        except Exception as exc:  # noqa: BLE001
            bus.warn(f"Could not write the {fmt.upper()} transcript: {exc}")
    try:
        export_summary(project, report)
    except Exception as exc:  # noqa: BLE001
        bus.warn(f"Could not write the summary: {exc}")
    bus.stage_end("export", "Transcript and summary ready")

    if not keep_audio:
        project.cleanup_scratch()

    return report


def _get_transcript(
    project: Project,
    settings: Settings,
    bus: EventBus,
    duration_s: float,
    has_audio: bool,
    ffmpeg_bin: str,
    warnings: list[str],
) -> Transcript:
    checkpoint = project.load_transcript_checkpoint()
    if checkpoint is not None and checkpoint.segments:
        bus.stage_start("transcript", "Reusing the saved transcript")
        bus.stage_end(
            "transcript", f"Loaded {len(checkpoint.segments)} segments from the checkpoint"
        )
        return checkpoint
    previous = project.load_analysis()
    if previous is not None and previous.transcript.segments:
        bus.stage_start("transcript", "Promoting the existing transcript checkpoint")
        bus.stage_end(
            "transcript",
            f"Loaded {len(previous.transcript.segments)} segments from the previous analysis",
        )
        return previous.transcript

    uploaded = project.transcript_upload_path
    if uploaded is not None and uploaded.is_file():
        bus.stage_start("transcript", f"Reading {uploaded.name}")
        try:
            transcript = load_transcript(uploaded, media_duration_s=duration_s)
        except SubtitleParseError as exc:
            warnings.append(f"{uploaded.name} could not be used ({exc}); transcribing instead.")
            bus.warn(str(exc))
        else:
            bus.stage_end(
                "transcript", f"Loaded {len(transcript.segments)} segments from the upload"
            )
            return transcript

    if not has_audio:
        raise ValueError(
            "This video has no audio track, so ClipDesk cannot create a transcript from it. "
            "Upload an SRT or VTT transcript alongside the video, or use a copy that "
            "contains narration."
        )

    bus.stage_start("audio", "Extracting audio")
    audio_path: Path = project.audio_dir / "audio.wav"
    extract_audio(
        project.source_path,
        audio_path,
        ffmpeg_bin=ffmpeg_bin,
        sample_rate=settings.media.audio_sample_rate,
        on_elapsed=lambda elapsed: bus.progress(
            "audio", min(1.0, elapsed / duration_s) if duration_s else None, "Extracting audio"
        ),
    )
    bus.stage_end("audio", f"Audio extracted ({audio_path.stat().st_size / 1e6:.0f} MB)")

    bus.stage_start("transcript", "Transcribing")
    transcriber = create_transcriber(settings, settings.paths.vendor_dir)
    transcript = transcriber.transcribe(
        audio_path,
        duration_s=duration_s,
        on_progress=lambda fraction, message: bus.progress("transcript", fraction, message),
    )
    bus.stage_end("transcript", f"{len(transcript.segments)} segments transcribed")
    return transcript
