"""Pipeline behavior around transcript acquisition and silent source videos."""

import pytest

from clipdesk.config import Settings
from clipdesk.events import EventBus
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
