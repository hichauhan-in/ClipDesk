"""Pipeline behavior around transcript acquisition and silent source videos."""

import pytest

from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.models import AnalysisReport, MediaInfo, Transcript, TranscriptSegment
from clipdesk.pipeline.analyze import _get_transcript
from clipdesk.store import ProjectStore


def test_a_video_without_audio_fails_before_ffmpeg_when_no_transcript_exists(tmp_path):
    project = ProjectStore(tmp_path / "workspace").create("silent.mp4")

    with pytest.raises(ValueError, match="no audio track") as failure:
        _get_transcript(
            project,
            Settings(),
            EventBus(),
            10.0,
            False,
            "ffmpeg-must-not-run",
            [],
        )

    assert "SRT or VTT" in str(failure.value)


def test_an_uploaded_transcript_allows_a_video_without_audio(tmp_path):
    project = ProjectStore(tmp_path / "workspace").create("silent.mp4")
    project.meta.transcript_filename = "captions.srt"
    project.meta.has_uploaded_transcript = True
    project.save()
    project.transcript_upload_path.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\nThe screen demonstrates the workflow.\n",
        encoding="utf-8",
    )

    transcript = _get_transcript(
        project,
        Settings(),
        EventBus(),
        10.0,
        False,
        "ffmpeg-must-not-run",
        [],
    )

    assert len(transcript.segments) == 1
    assert "demonstrates" in transcript.segments[0].text


def test_a_saved_checkpoint_skips_transcription_on_rerun(tmp_path):
    project = ProjectStore(tmp_path / "workspace").create("silent.mp4")
    project.save_transcript_checkpoint(
        Transcript(
            duration_s=10.0,
            segments=[
                TranscriptSegment(id=0, start=0.0, end=5.0, text="Already transcribed.")
            ],
        )
    )

    transcript = _get_transcript(
        project,
        Settings(),
        EventBus(),
        10.0,
        False,
        "ffmpeg-must-not-run",
        [],
    )

    assert transcript.segments[0].text == "Already transcribed."


def test_an_existing_analysis_is_promoted_without_retranscribing(tmp_path):
    project = ProjectStore(tmp_path / "workspace").create("legacy.mp4")
    project.save_analysis(
        AnalysisReport(
            project_id=project.id,
            media=MediaInfo(path=str(project.source_path), duration_s=10.0),
            transcript=Transcript(
                duration_s=10.0,
                segments=[
                    TranscriptSegment(id=0, start=0.0, end=4.0, text="Legacy transcript.")
                ],
            ),
        )
    )

    transcript = _get_transcript(
        project,
        Settings(),
        EventBus(),
        10.0,
        False,
        "ffmpeg-must-not-run",
        [],
    )

    assert transcript.segments[0].text == "Legacy transcript."
