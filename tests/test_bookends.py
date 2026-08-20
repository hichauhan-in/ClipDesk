"""Intro/outro joins keep each selected transition at its own boundary."""

from types import SimpleNamespace

from clipdesk.actions.bookends import apply_bookends
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.store import ProjectStore


def test_normalised_join_pads_video_to_the_probed_audio_duration(tmp_path, monkeypatch):
    from clipdesk.actions.render import ConcatInput, concat_normalised
    from clipdesk.config import RenderConfig

    captured = {}
    source = tmp_path / "footer.mp4"
    source.write_bytes(b"footer")

    def fake_filter_args(graph, _scratch, _ffmpeg):
        captured["graph"] = graph
        return ["-filter_complex", graph], None

    monkeypatch.setattr("clipdesk.actions.render.filter_complex_args", fake_filter_args)
    monkeypatch.setattr("clipdesk.actions.render.run_with_progress", lambda *_args, **_kwargs: None)

    concat_normalised(
        [ConcatInput(source, duration_s=8.0, has_audio=True)],
        tmp_path / "joined.mp4",
        render=RenderConfig(width=320, height=180, fps=30),
        ffmpeg_bin="ffmpeg",
    )

    assert "tpad=stop_mode=clone:stop_duration=8.000" in captured["graph"]
    assert "trim=duration=8.000" in captured["graph"]


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

def test_a_rendered_output_can_be_attached_as_an_intro(tmp_path):
    """Intro Studio writes to outputs, not to media.

    Restricting attachments to imported media left a project unable to attach
    the very intro it had just produced -- it appeared under resources but not
    in the intro/outro pickers.
    """
    from clipdesk.actions.bookends import media_dir, resolve_attachment

    project = ProjectStore(tmp_path / "workspace").create("meeting.mp4")
    media_dir(project).mkdir(parents=True, exist_ok=True)
    project.output_dir.mkdir(parents=True, exist_ok=True)

    imported = media_dir(project) / "sting.mp4"
    imported.write_bytes(b"x")
    rendered = project.output_dir / "intro.mp4"
    rendered.write_bytes(b"x")

    assert resolve_attachment(project, "sting.mp4") == imported.resolve()
    assert resolve_attachment(project, "intro.mp4") == rendered.resolve()


def test_an_attachment_cannot_escape_the_project(tmp_path):
    """Widening where a name may resolve must not widen how far it may reach."""
    import pytest

    from clipdesk.actions.bookends import resolve_attachment

    project = ProjectStore(tmp_path / "workspace").create("meeting.mp4")
    project.output_dir.mkdir(parents=True, exist_ok=True)
    outsider = tmp_path / "outsider.mp4"
    outsider.write_bytes(b"x")

    for name in ("../outsider.mp4", "..\\outsider.mp4", str(outsider)):
        with pytest.raises(ValueError, match="No such media"):
            resolve_attachment(project, name)


def test_a_non_video_output_is_refused(tmp_path):
    """Outputs hold articles and notes too, which cannot be joined to a video."""
    import pytest

    from clipdesk.actions.bookends import resolve_attachment

    project = ProjectStore(tmp_path / "workspace").create("meeting.mp4")
    project.output_dir.mkdir(parents=True, exist_ok=True)
    (project.output_dir / "article.docx").write_bytes(b"x")

    with pytest.raises(ValueError, match="not a video"):
        resolve_attachment(project, "article.docx")
