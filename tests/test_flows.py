"""Saved Flows compile into the existing typed job sequence."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipdesk.config import load_settings
from clipdesk.flows import FlowDefinition, delete_flow, load_flows, save_flow
from clipdesk.models import AnalysisReport, MediaInfo
from clipdesk.server.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "user_state_dir", lambda: tmp_path / "state")
    with TestClient(create_app(load_settings())) as test_client:
        yield test_client


def analysed_project(client) -> tuple[str, object]:
    payload = client.post(
        "/api/projects",
        files={"video": ("meeting.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    project = state.store.get(payload["id"])
    project.save_analysis(
        AnalysisReport(
            project_id=project.id,
            title="Team review",
            media=MediaInfo(path=str(project.source_path), duration_s=60.0, has_audio=False),
        )
    )
    return project.id, project


def test_flows_are_saved_reloaded_and_deleted_atomically(tmp_path):
    flow = FlowDefinition(
        id="weekly-video",
        name="Weekly video",
        steps=[{"type": "notes", "enrichment": 2}],
    )

    save_flow(tmp_path, flow)

    assert load_flows(tmp_path) == [flow]
    assert delete_flow(tmp_path, flow.id) is True
    assert load_flows(tmp_path) == []


def test_flow_refuses_assemble_before_a_bookend(client):
    project_id, _project = analysed_project(client)
    flow = {
        "id": "bad-order",
        "name": "Bad order",
        "steps": [{"type": "assemble", "output_name": "final.mp4"}],
    }
    assert client.put("/api/flows/bad-order", json=flow).status_code == 200

    response = client.post(f"/api/projects/{project_id}/flows/bad-order/run")

    assert response.status_code == 400
    assert "before Assemble" in response.json()["detail"]


def test_flow_runs_existing_actions_in_dependency_order(client, monkeypatch):
    from clipdesk.server import app as app_module

    project_id, project = analysed_project(client)
    state = client.app.state.clipdesk.authenticate({})
    rendered_sources = []
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *args, **kwargs: SimpleNamespace(duration_s=8.0, has_audio=True),
    )

    def fake_cleanup(project_arg, _report, _settings, _options, _ffmpeg, _bus, output_name):
        path = project_arg.output_path(output_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clean")
        return path

    def fake_soundtrack(_audio, _duration, destination, _ffmpeg, **_kwargs):
        destination.write_bytes(b"audio")
        return destination

    def fake_intro(source, destination, _plan, **_kwargs):
        rendered_sources.append(source)
        destination.write_bytes(b"bookend")
        return destination

    def fake_bookends(project_arg, _settings, _ffprobe, _ffmpeg, _bus, **kwargs):
        path = project_arg.output_path(kwargs["output_name"])
        path.write_bytes(b"final")
        return path

    monkeypatch.setattr(app_module, "render_cleanup", fake_cleanup)
    monkeypatch.setattr(app_module, "prepare_soundtrack", fake_soundtrack)
    monkeypatch.setattr(app_module, "render_intro", fake_intro)
    monkeypatch.setattr(app_module, "apply_bookends", fake_bookends)
    monkeypatch.setattr(
        app_module,
        "resolve_audio",
        lambda *_args: Path(__file__),
    )

    flow = {
        "id": "publish-ready",
        "name": "Publish ready",
        "description": "Clean and brand every recording.",
        "steps": [
            {"type": "cleanup", "output_name": "flow-clean.mp4"},
            {
                "type": "intro",
                "input_from": "flow-clean.mp4",
                "style_id": "keynote",
                "audio_id": "none",
                "output_name": "flow-intro.mp4",
            },
            {
                "type": "outro",
                "style_id": "minimal-frame",
                "audio_id": "none",
                "output_name": "flow-outro.mp4",
            },
            {
                "type": "assemble",
                "input_from": "flow-clean.mp4",
                "output_name": "flow-final.mp4",
            },
        ],
    }
    assert client.put("/api/flows/publish-ready", json=flow).status_code == 200

    response = client.post(f"/api/projects/{project_id}/flows/publish-ready/run")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [step["produces"] for step in payload["queued_steps"]] == [
        "flow-clean.mp4",
        "flow-intro.mp4",
        "flow-outro.mp4",
        "flow-final.mp4",
    ]
    assert payload["final_output"] == "flow-final.mp4"
    jobs = [state.jobs.get(job_id) for job_id in payload["job_ids"]]
    assert jobs[0].depends_on == []
    assert [job.depends_on for job in jobs[1:]] == [[jobs[0].id], [jobs[1].id], [jobs[2].id]]
    for job in jobs:
        assert job.finished.wait(2)
        assert job.status == "done", job.error
    assert project.output_path("flow-final.mp4").read_bytes() == b"final"
    assert rendered_sources[0] == project.output_path("flow-clean.mp4")


def test_notes_flow_runs_with_saved_verbosity(client, monkeypatch):
    from clipdesk.server import app as app_module

    project_id, project = analysed_project(client)
    state = client.app.state.clipdesk.authenticate({})
    captured = {}

    class FakeMeter:
        def to_dict(self):
            return {}

    class FakeLlm:
        meter = FakeMeter()

        def for_task(self, _task):
            return self

    monkeypatch.setattr(state, "llm", lambda **_kwargs: FakeLlm())

    def fake_notes(project_arg, _report, settings, _llm, _bus):
        captured["enrichment"] = settings.notes.enrichment
        path = project_arg.output_path("notes.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("notes", encoding="utf-8")
        return [path]

    monkeypatch.setattr(app_module, "generate_notes", fake_notes)
    flow = {
        "id": "notes-flow",
        "name": "Notes flow",
        "steps": [{"type": "notes", "enrichment": 4}],
    }
    client.put("/api/flows/notes-flow", json=flow)

    response = client.post(f"/api/projects/{project_id}/flows/notes-flow/run")

    assert response.status_code == 200
    job = state.jobs.get(response.json()["job_ids"][0])
    assert job.finished.wait(2)
    assert job.status == "done", job.error
    assert captured["enrichment"] == 4
    assert project.output_path("notes.md").read_text(encoding="utf-8") == "notes"


def test_flow_copies_a_fixed_local_bookend_before_assembly(client, monkeypatch, tmp_path):
    from clipdesk.server import app as app_module

    project_id, project = analysed_project(client)
    state = client.app.state.clipdesk.authenticate({})
    fixed = tmp_path / "brand-intro.mp4"
    fixed.write_bytes(b"brand")
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *args, **kwargs: SimpleNamespace(duration_s=8.0, has_audio=True),
    )

    def fake_bookends(project_arg, _settings, _ffprobe, _ffmpeg, _bus, **kwargs):
        assert kwargs["header"].read_bytes() == b"brand"
        path = project_arg.output_path(kwargs["output_name"])
        path.write_bytes(b"assembled")
        return path

    monkeypatch.setattr(app_module, "apply_bookends", fake_bookends)
    flow = {
        "id": "fixed-brand",
        "name": "Fixed brand",
        "steps": [
            {"type": "intro", "source": "local", "local_path": str(fixed)},
            {"type": "assemble", "output_name": "branded.mp4"},
        ],
    }
    client.put("/api/flows/fixed-brand", json=flow)

    response = client.post(f"/api/projects/{project_id}/flows/fixed-brand/run")

    assert response.status_code == 200, response.text
    job = state.jobs.get(response.json()["job_ids"][0])
    assert job.finished.wait(2)
    assert job.status == "done", job.error
    assert fixed.read_bytes() == b"brand"
    assert (project.root / "media" / fixed.name).read_bytes() == b"brand"
    assert project.output_path("branded.mp4").read_bytes() == b"assembled"


@pytest.mark.parametrize(
    "steps, message",
    [
        (
            [
                {"type": "clip", "input_from": "later.mp4", "output_name": "clip.mp4"},
                {"type": "cleanup", "output_name": "later.mp4"},
            ],
            "must be produced by an earlier step",
        ),
        (
            [
                {"type": "cleanup", "output_name": "same.mp4"},
                {"type": "clip", "input_from": "same.mp4", "output_name": "same.mp4"},
            ],
            "already produced",
        ),
    ],
)
def test_flow_rejects_invalid_source_graphs(client, steps, message):
    project_id, _project = analysed_project(client)
    flow = {"id": "invalid-graph", "name": "Invalid graph", "steps": steps}
    assert client.put("/api/flows/invalid-graph", json=flow).status_code == 200

    response = client.post(f"/api/projects/{project_id}/flows/invalid-graph/run")

    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_named_cleanup_output_feeds_prompt_and_assembly(client, monkeypatch, tmp_path):
    from clipdesk.server import app as app_module

    project_id, project = analysed_project(client)
    state = client.app.state.clipdesk.authenticate({})
    fixed = tmp_path / "brand-intro.mp4"
    fixed.write_bytes(b"brand")
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *args, **kwargs: SimpleNamespace(duration_s=8.0, has_audio=True),
    )

    def fake_cleanup(project_arg, _report, _settings, _options, _ffmpeg, _bus, output_name):
        path = project_arg.output_path(output_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clean")
        return path

    def fake_program(source, destination, _program, **_kwargs):
        assert source == project.output_path("named-clean.mp4")
        assert source.read_bytes() == b"clean"
        destination.write_bytes(b"edited")

    def fake_bookends(project_arg, _settings, _ffprobe, _ffmpeg, _bus, **kwargs):
        assert kwargs["body_filename"] == "named-edit.mp4"
        assert project_arg.output_path(kwargs["body_filename"]).read_bytes() == b"edited"
        path = project_arg.output_path(kwargs["output_name"])
        path.write_bytes(b"final")
        return path

    monkeypatch.setattr(app_module, "render_cleanup", fake_cleanup)
    monkeypatch.setattr(app_module, "render_program", fake_program)
    monkeypatch.setattr(app_module, "apply_bookends", fake_bookends)
    flow = {
        "id": "named-chain",
        "name": "Named chain",
        "steps": [
            {"type": "cleanup", "output_name": "named-clean.mp4"},
            {
                "type": "prompt",
                "input_from": "named-clean.mp4",
                "prompt": "trim the first 1 seconds",
                "output_name": "named-edit.mp4",
            },
            {"type": "intro", "source": "local", "local_path": str(fixed)},
            {
                "type": "assemble",
                "input_from": "named-edit.mp4",
                "output_name": "named-final.mp4",
            },
        ],
    }
    client.put("/api/flows/named-chain", json=flow)

    response = client.post(f"/api/projects/{project_id}/flows/named-chain/run")

    assert response.status_code == 200, response.text
    jobs = [state.jobs.get(job_id) for job_id in response.json()["job_ids"]]
    for job in jobs:
        assert job.finished.wait(2)
        assert job.status == "done", job.error
    assert project.output_path("named-final.mp4").read_bytes() == b"final"


def test_best_highlight_step_selects_and_renders_top_candidate(client, monkeypatch):
    from clipdesk.actions.clips import CandidateResult
    from clipdesk.models import ClipCandidate
    from clipdesk.server import app as app_module

    project_id, project = analysed_project(client)
    state = client.app.state.clipdesk.authenticate({})

    class FakeMeter:
        def to_dict(self):
            return {}

    class FakeLlm:
        meter = FakeMeter()

        def for_task(self, _task):
            return self

    monkeypatch.setattr(state, "llm", lambda **_kwargs: FakeLlm())
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "find_candidates",
        lambda *_args, **_kwargs: CandidateResult(
            candidates=[ClipCandidate(title="Best moment", start=5.0, end=25.0, score=0.95)]
        ),
    )

    def fake_render(project_arg, _report, clips, _settings, _ffmpeg, _bus, **kwargs):
        assert clips[0].title == "Best moment"
        path = project_arg.output_path(kwargs["output_name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"highlight")
        return [path]

    monkeypatch.setattr(app_module, "render_selection", fake_render)
    flow = {
        "id": "best-highlight",
        "name": "Best highlight",
        "steps": [{"type": "highlight", "mode": "best", "output_name": "best.mp4"}],
    }
    client.put("/api/flows/best-highlight", json=flow)

    response = client.post(f"/api/projects/{project_id}/flows/best-highlight/run")

    assert response.status_code == 200
    job = state.jobs.get(response.json()["job_ids"][0])
    assert job.finished.wait(2)
    assert job.status == "done", job.error
    assert project.output_path("best.mp4").read_bytes() == b"highlight"
