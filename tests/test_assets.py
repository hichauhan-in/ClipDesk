"""Imported media belongs to one project, and can be copied between them."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipdesk.actions.bookends import store_asset, store_media
from clipdesk.config import load_settings
from clipdesk.ingest.fetch import FetchResult
from clipdesk.server.app import create_app
from clipdesk.store import ProjectStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    with TestClient(create_app(load_settings())) as test_client:
        yield test_client


def make_project(client, name="talk.mp4") -> str:
    response = client.post(
        "/api/projects",
        files={"video": (name, b"video bytes", "video/mp4")},
        data={"title": Path(name).stem},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_store_asset_does_not_overwrite_an_existing_name(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    stored_first = store_asset(first, tmp_path / "workspace", "intro.mp4")
    stored_second = store_asset(second, tmp_path / "workspace", "intro.mp4")

    assert stored_first.name == "intro.mp4"
    assert stored_second.name == "intro-2.mp4"
    assert stored_first.read_bytes() == b"first"
    assert stored_second.read_bytes() == b"second"


def test_store_media_keeps_both_files_inside_the_project(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    project = store.create("talk.mp4", title="Talk")
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    stored_first = store_media(first, project, "intro.mp4")
    stored_second = store_media(second, project, "intro.mp4")

    assert stored_first.name == "intro.mp4"
    assert stored_second.name == "intro-2.mp4"
    assert stored_first.parent == project.root / "media"


def test_uploading_the_same_media_twice_keeps_both(client):
    project_id = make_project(client)
    for content in (b"first", b"second"):
        response = client.post(
            f"/api/projects/{project_id}/media",
            files={"file": ("intro.mp4", content, "video/mp4")},
        )
        assert response.status_code == 201

    names = [item["name"] for item in client.get(f"/api/projects/{project_id}/media").json()]
    assert names == ["intro-2.mp4", "intro.mp4"]


def test_media_upload_obeys_the_server_size_limit(client):
    project_id = make_project(client)
    client.app.state.clipdesk.settings.server.max_upload_mb = 0

    response = client.post(
        f"/api/projects/{project_id}/media",
        files={"file": ("intro.mp4", b"not empty", "video/mp4")},
    )

    assert response.status_code == 413
    assert client.get(f"/api/projects/{project_id}/media").json() == []


def test_media_is_not_shared_between_projects(client):
    first = make_project(client, "first.mp4")
    second = make_project(client, "second.mp4")
    client.post(
        f"/api/projects/{first}/media",
        files={"file": ("shared.mp4", b"bytes", "video/mp4")},
    )

    assert [item["name"] for item in client.get(f"/api/projects/{first}/media").json()] == [
        "shared.mp4"
    ]
    assert client.get(f"/api/projects/{second}/media").json() == []


def test_project_media_preview_supports_range_requests(client):
    project_id = make_project(client)
    client.post(
        f"/api/projects/{project_id}/media",
        files={"file": ("preview.mp4", b"0123456789", "video/mp4")},
    )

    response = client.get(
        f"/api/projects/{project_id}/media/preview.mp4",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["accept-ranges"] == "bytes"


def test_media_from_another_project_is_offered_and_copied_on_request(client):
    first = make_project(client, "first.mp4")
    second = make_project(client, "second.mp4")
    client.post(
        f"/api/projects/{first}/media",
        files={"file": ("opener.mp4", b"bytes", "video/mp4")},
    )

    library = client.get(f"/api/projects/{second}/media-library").json()
    offered = {item["name"] for source in library["sources"] for item in source["items"]}
    assert offered == {"opener.mp4"}

    adopted = client.post(
        f"/api/projects/{second}/media/adopt",
        json={"name": "opener.mp4", "source_project_id": first},
    )

    assert adopted.status_code == 201
    assert [item["name"] for item in client.get(f"/api/projects/{second}/media").json()] == [
        "opener.mp4"
    ]
    # Copying leaves the original where it was.
    assert [item["name"] for item in client.get(f"/api/projects/{first}/media").json()] == [
        "opener.mp4"
    ]


def test_a_folder_must_be_resolved_to_files_before_media_import(client):
    project_id = make_project(client)
    response = client.post(
        f"/api/projects/{project_id}/media/from-link",
        json={
            "project_id": project_id,
            "items": [
                {
                    "url": "https://contoso.sharepoint.com/:f:/t/Team/token?e=abc",
                    "name": "",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "Choose one or more videos" in response.json()["detail"]


def test_a_linked_media_job_stores_the_download_in_the_project(client, monkeypatch):
    from clipdesk.server import app as app_module

    def fake_fetch(url, destination_dir, on_progress, **kwargs):
        path = Path(destination_dir) / "remote.mp4"
        path.write_bytes(b"video bytes")
        on_progress(1.0, "Downloaded")
        return FetchResult(path, url, "direct", path.stat().st_size)

    monkeypatch.setattr(app_module, "fetch_video", fake_fetch)
    project_id = make_project(client)
    response = client.post(
        f"/api/projects/{project_id}/media/from-link",
        json={
            "project_id": project_id,
            "items": [
                {
                    "url": "https://media.example.com/remote.mp4",
                    "name": "branded-intro.mp4",
                }
            ],
        },
    )

    assert response.status_code == 202
    job = client.app.state.clipdesk.jobs.get(response.json()["job_id"])
    assert job is not None
    assert job.finished.wait(2)
    assert job.status == "done", job.error
    assert job.result["assets"][0]["name"] == "branded-intro.mp4"
    assert [item["name"] for item in client.get(f"/api/projects/{project_id}/media").json()] == [
        "branded-intro.mp4"
    ]
