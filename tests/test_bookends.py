"""Intro/outro joins keep each selected transition at its own boundary."""

from types import SimpleNamespace

from clipdesk.actions.bookends import apply_bookends
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.store import ProjectStore


def test_intro_and_outro_transitions_are_independent(tmp_path, monkeypatch):
    project = ProjectStore(tmp_path / "workspace").create("meeting.mp4")
    project.source_path.parent.mkdir(parents=True, exist_ok=True)
    project.source_path.write_bytes(b"body")
    intro = tmp_path / "intro.mp4"
    outro = tmp_path / "outro.mp4"
    intro.write_bytes(b"intro")
    outro.write_bytes(b"outro")
    captured = {}

    monkeypatch.setattr(
        "clipdesk.actions.bookends.probe",
        lambda path, _ffprobe: SimpleNamespace(
            duration_s=10.0 if path == project.source_path else 2.0,
            has_audio=True,
        ),
    )

    def fake_concat(inputs, destination, **kwargs):
        captured["parts"] = [item.path.name for item in inputs]
        captured["transitions"] = kwargs["transitions"]
        destination.write_bytes(b"joined")

    monkeypatch.setattr("clipdesk.actions.render.concat_normalised", fake_concat)

    apply_bookends(
        project,
        Settings(),
        "ffprobe",
        "ffmpeg",
        EventBus(),
        body_filename="meeting.mp4",
        header=intro,
        footer=outro,
        intro_transition="dissolve",
        outro_transition="wipe-left",
    )

    assert captured["parts"] == ["intro.mp4", "meeting.mp4", "outro.mp4"]
    assert captured["transitions"] == ["dissolve", "wipe-left"]
    artifact = project.meta.artifacts[-1]
    assert artifact["meta"]["intro_transition"] == "dissolve"
    assert artifact["meta"]["outro_transition"] == "wipe-left"