"""Multi-file ingestion creates ordered, isolated analysis jobs."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipdesk.config import load_settings
from clipdesk.ingest.fetch import FetchResult
from clipdesk.ingest.localsource import Root
from clipdesk.models import AnalysisReport, MediaInfo, Transcript, TranscriptSegment
from clipdesk.server.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "user_state_dir", lambda: tmp_path / "state")
    with TestClient(create_app(load_settings())) as test_client:
        yield test_client


def install_fake_analysis(client, monkeypatch, order):
    from clipdesk.server import app as app_module

    state = client.app.state.clipdesk.authenticate({})
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )

    class Meter:
        def to_dict(self):
            return {}

    monkeypatch.setattr(state, "llm", lambda *_args, **_kwargs: SimpleNamespace(meter=Meter()))

    def fake_analyze(project, _settings, _bus, **_kwargs):
        order.append(f"analyse:{project.meta.source_filename}")
        report = AnalysisReport(
            project_id=project.id,
            title=project.meta.title,
            media=MediaInfo(
                path=str(project.source_path),
                duration_s=10.0,
                size_bytes=project.source_path.stat().st_size,
                has_audio=True,
            ),
            transcript=Transcript(
                duration_s=10.0,
                segments=[TranscriptSegment(id=0, start=0, end=5, text="Transcript")],
            ),
        )
        project.save_transcript_checkpoint(report.transcript)
        project.save_analysis(report)
        return report

    monkeypatch.setattr(app_module, "analyze_project", fake_analyze)
    return state


def wait_for_jobs(state, ids):
    jobs = [state.jobs.get(job_id) for job_id in ids]
    for job in jobs:
        assert job is not None
        assert job.finished.wait(3)
        assert job.status == "done", job.error
    return jobs


def test_multiple_uploads_queue_analysis_in_selection_order(client, monkeypatch):
    order = []
    state = install_fake_analysis(client, monkeypatch, order)

    response = client.post(
        "/api/projects/batch",
        files=[
            ("videos", ("first.mp4", b"first", "video/mp4")),
            ("videos", ("second.mp4", b"second", "video/mp4")),
        ],
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    jobs = wait_for_jobs(state, [item["id"] for item in payload["jobs"]])
    assert payload["count"] == 2
    assert [item["source_filename"] for item in payload["projects"]] == [
        "first.mp4",
        "second.mp4",
    ]
    assert [job.kind for job in jobs] == ["analyze", "analyze"]
    assert order == ["analyse:first.mp4", "analyse:second.mp4"]


def test_single_upload_endpoint_keeps_its_existing_contract(client):
    response = client.post(
        "/api/projects",
        files={"video": ("single.mp4", b"single", "video/mp4")},
    )

    assert response.status_code == 201
    assert response.json()["source_filename"] == "single.mp4"
    assert "jobs" not in response.json()


def test_synced_drive_batch_runs_copy_then_analysis_for_each_file(client, monkeypatch, tmp_path):
    from clipdesk.server import app as app_module

    order = []
    state = install_fake_analysis(client, monkeypatch, order)
    base = tmp_path / "OneDrive - Contoso"
    base.mkdir()
    (base / "first.mp4").write_bytes(b"first")
    (base / "second.mp4").write_bytes(b"second")
    root = Root("onedrive-contoso", "OneDrive - Contoso", base)
    monkeypatch.setattr(app_module, "cloud_roots", lambda: [root])

    real_copy = app_module.copy_into

    def tracked_copy(source, destination, progress):
        order.append(f"copy:{Path(source).name}")
        return real_copy(source, destination, progress)

    monkeypatch.setattr(app_module, "copy_into", tracked_copy)
    response = client.post(
        "/api/projects/from-local/batch",
        json={
            "items": [
                {"root": root.id, "path": "first.mp4"},
                {"root": root.id, "path": "second.mp4"},
            ]
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    jobs = wait_for_jobs(state, [item["id"] for item in payload["jobs"]])
    assert [job.kind for job in jobs] == ["batch-import", "analyze", "batch-import", "analyze"]
    assert order == [
        "copy:first.mp4",
        "analyse:first.mp4",
        "copy:second.mp4",
        "analyse:second.mp4",
    ]


def test_single_synced_drive_import_remains_one_download_job(client, monkeypatch, tmp_path):
    from clipdesk.server import app as app_module

    base = tmp_path / "OneDrive - Contoso"
    base.mkdir()
    (base / "single.mp4").write_bytes(b"single")
    root = Root("onedrive-contoso", "OneDrive - Contoso", base)
    monkeypatch.setattr(app_module, "cloud_roots", lambda: [root])

    response = client.post(
        "/api/projects/from-local",
        json={"root": root.id, "path": "single.mp4"},
    )

    assert response.status_code == 202
    state = client.app.state.clipdesk.authenticate({})
    job = state.jobs.get(response.json()["job_id"])
    assert job.kind == "download"
    assert len(state.jobs.list(response.json()["project_id"])) == 1


def test_link_folder_batch_runs_download_then_analysis_for_each_file(client, monkeypatch):
    from clipdesk.server import app as app_module

    order = []
    state = install_fake_analysis(client, monkeypatch, order)
    monkeypatch.setattr(
        app_module,
        "describe_link",
        lambda url, _vendor: {
            "kind": "direct",
            "suggested_name": Path(url).name,
            "is_folder": False,
        },
    )

    def fake_fetch(url, destination_dir, on_progress, **_kwargs):
        filename = Path(url).name
        order.append(f"download:{filename}")
        path = Path(destination_dir) / filename
        path.write_bytes(filename.encode())
        on_progress(1.0, "Downloaded")
        return FetchResult(path, url, "direct", path.stat().st_size)

    monkeypatch.setattr(app_module, "fetch_video", fake_fetch)
    response = client.post(
        "/api/projects/from-links",
        json={
            "items": [
                {"url": "https://media.example.com/first.mp4"},
                {"url": "https://media.example.com/second.mp4"},
            ]
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    jobs = wait_for_jobs(state, [item["id"] for item in payload["jobs"]])
    assert [job.kind for job in jobs] == ["batch-import", "analyze", "batch-import", "analyze"]
    assert order == [
        "download:first.mp4",
        "analyse:first.mp4",
        "download:second.mp4",
        "analyse:second.mp4",
    ]


def test_single_link_import_remains_one_download_job(client, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setattr(
        app_module,
        "describe_link",
        lambda url, _vendor: {
            "kind": "direct",
            "suggested_name": Path(url).name,
            "is_folder": False,
        },
    )
    monkeypatch.setattr(
        app_module,
        "fetch_video",
        lambda url, destination_dir, _progress, **_kwargs: FetchResult(
            Path(destination_dir) / Path(url).name,
            url,
            "direct",
            0,
        ),
    )

    response = client.post(
        "/api/projects/from-link",
        json={"url": "https://media.example.com/single.mp4"},
    )

    assert response.status_code == 202
    state = client.app.state.clipdesk.authenticate({})
    job = state.jobs.get(response.json()["job_id"])
    assert job.kind == "download"
    assert len(state.jobs.list(response.json()["project_id"])) == 1
