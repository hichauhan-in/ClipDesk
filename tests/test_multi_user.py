"""Hosted-mode identity and cross-user isolation security checks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipdesk.actions.intro import BUILT_IN_STYLES
from clipdesk.config import load_settings
from clipdesk.ingest.browsersignin import recalled_resolution, remember_resolution
from clipdesk.server.app import create_app


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setenv("CLIPDESK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLIPDESK_PROXY_SECRET", "proxy-only-secret")
    settings = load_settings()
    settings.server.multi_user = True
    with TestClient(create_app(settings)) as client:
        yield client


def headers(user: str, secret: str = "proxy-only-secret") -> dict[str, str]:
    return {
        "x-clipdesk-user": user,
        "x-clipdesk-proxy-secret": secret,
    }


def create_project(client: TestClient, user: str) -> dict:
    response = client.post(
        "/api/projects",
        headers=headers(user),
        files={"video": ("private.mp4", b"private bytes", "video/mp4")},
    )
    assert response.status_code == 201
    return response.json()


def test_hosted_requests_fail_closed_without_proxy_context(hosted):
    assert hosted.get("/api/projects").status_code == 401
    assert hosted.get("/api/projects", headers={"x-clipdesk-user": "alice"}).status_code == 401
    assert hosted.get("/api/projects", headers=headers("alice", "wrong")).status_code == 401


def test_hosted_mode_requires_the_server_side_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.delenv("CLIPDESK_PROXY_SECRET", raising=False)
    settings = load_settings()
    settings.server.multi_user = True

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/projects", headers=headers("alice"))

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_non_loopback_servers_must_enable_multi_user(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    settings = load_settings()
    settings.server.host = "0.0.0.0"

    with pytest.raises(RuntimeError, match="multi_user=true"):
        create_app(settings)


def test_projects_are_invisible_across_users(hosted):
    project = create_project(hosted, "alice-subject")

    assert [item["id"] for item in hosted.get("/api/projects", headers=headers("alice-subject")).json()] == [project["id"]]
    assert hosted.get("/api/projects", headers=headers("bob-subject")).json() == []
    assert hosted.get(f"/api/projects/{project['id']}", headers=headers("bob-subject")).status_code == 404
    assert hosted.get(f"/api/projects/{project['id']}/preview", headers=headers("bob-subject")).status_code == 404


def test_user_identifiers_never_become_directory_names(hosted):
    create_project(hosted, "Alice Example <alice@example.com>")
    registry = hosted.app.state.clipdesk
    user = registry.for_identity("Alice Example <alice@example.com>")

    assert user.store.workspace.parent.name == "users"
    assert len(user.store.workspace.name) == 64
    assert all(character in "0123456789abcdef" for character in user.store.workspace.name)
    assert "alice" not in str(user.store.workspace).lower()


def test_settings_are_isolated(hosted):
    response = hosted.put(
        "/api/settings",
        headers=headers("alice"),
        json={"llm_model": "alice-model"},
    )

    assert response.status_code == 200
    assert hosted.get("/api/settings", headers=headers("alice")).json()["llm_model"] == "alice-model"
    assert hosted.get("/api/settings", headers=headers("bob")).json()["llm_model"] == ""


def test_hosted_users_cannot_redirect_provider_secrets(hosted):
    response = hosted.put(
        "/api/settings",
        headers=headers("alice"),
        json={
            "openai_base_url": "https://attacker.example",
            "openai_api_key_env": "CLIPDESK_PROXY_SECRET",
        },
    )

    assert response.status_code == 403
    settings = hosted.get("/api/settings", headers=headers("alice")).json()
    assert settings["openai_base_url"] != "https://attacker.example"
    assert settings["openai_api_key_env"] != "CLIPDESK_PROXY_SECRET"


def test_hosted_users_cannot_provision_shared_dependencies(hosted):
    response = hosted.post(
        "/api/setup/provision",
        headers=headers("alice"),
        json={"component": "whisper"},
    )

    assert response.status_code == 403


def test_saved_sessions_are_isolated(hosted):
    response = hosted.post(
        "/api/sessions",
        headers=headers("alice"),
        json={
            "url": "https://tenant.sharepoint.com/video.mp4",
            "pasted": "Cookie: FedAuth=alice-secret; rtFa=alice-refresh",
        },
    )

    assert response.status_code == 200
    assert len(hosted.get("/api/sessions", headers=headers("alice")).json()["sessions"]) == 1
    assert hosted.get("/api/sessions", headers=headers("bob")).json()["sessions"] == []


def test_opaque_link_resolutions_are_isolated(hosted):
    registry = hosted.app.state.clipdesk
    alice = registry.for_identity("alice")
    bob = registry.for_identity("bob")
    original = "https://onedrive.cloud.microsoft/:f:/a@tenant/S/opaque"
    resolved = "https://onedrive.cloud.microsoft/shared?id=alice-folder"

    remember_resolution(alice.state_dir, original, resolved)

    assert recalled_resolution(alice.state_dir, original) == resolved
    assert recalled_resolution(bob.state_dir, original) is None


def test_custom_intro_styles_are_isolated(hosted):
    definition = {
        "id": "alice-open",
        "name": "Alice open",
        "description": "A custom warm opener for Alice.",
        "accent": "#f0a13c",
        "backdrop": "source-blur",
        "title_animation": "band-reveal",
        "grade": "warm",
    }

    response = hosted.post(
        "/api/intro/styles/import", headers=headers("alice"), json=definition
    )

    assert response.status_code == 201
    alice = hosted.get("/api/intro/styles", headers=headers("alice")).json()
    bob = hosted.get("/api/intro/styles", headers=headers("bob")).json()
    assert "alice-open" in {item["id"] for item in alice["installed"]}
    assert "alice-open" not in {item["id"] for item in bob["installed"]}


def test_intro_catalog_installs_on_demand(hosted):
    before = hosted.get("/api/intro/styles", headers=headers("alice")).json()

    installed = hosted.post(
        "/api/intro/styles/install",
        headers=headers("alice"),
        json={"style_id": "cinema-bars"},
    )
    after = hosted.get("/api/intro/styles", headers=headers("alice")).json()

    assert len(before["installed"]) == len(BUILT_IN_STYLES)
    assert installed.status_code == 201
    assert "cinema-bars" in {item["id"] for item in after["installed"]}
    assert len(after["installed"]) == len(before["installed"]) + 1


def test_project_media_is_isolated_between_hosted_users(hosted):
    def make_project(user: str) -> str:
        return hosted.post(
            "/api/projects",
            headers=headers(user),
            files={"video": ("talk.mp4", b"video", "video/mp4")},
        ).json()["id"]

    alice_project = make_project("alice")
    bob_project = make_project("bob")

    response = hosted.post(
        f"/api/projects/{alice_project}/media",
        headers=headers("alice"),
        files={"file": ("intro.mp4", b"intro", "video/mp4")},
    )

    assert response.status_code == 201
    alice_media = hosted.get(
        f"/api/projects/{alice_project}/media", headers=headers("alice")
    ).json()
    assert [item["name"] for item in alice_media] == ["intro.mp4"]
    # Bob cannot see Alice's project at all, let alone its media.
    assert hosted.get(
        f"/api/projects/{alice_project}/media", headers=headers("bob")
    ).status_code == 404
    assert hosted.get(
        f"/api/projects/{bob_project}/media", headers=headers("bob")
    ).json() == []
    assert hosted.get(
        f"/api/projects/{bob_project}/media-library", headers=headers("bob")
    ).json()["sources"] == []


def test_intro_audio_is_isolated_between_hosted_users(hosted, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *_args, **_kwargs: SimpleNamespace(has_audio=True, duration_s=2.0),
    )
    state = hosted.app.state.clipdesk.authenticate(headers("alice"))
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    response = hosted.post(
        "/api/intro/audio",
        headers=headers("alice"),
        files={"file": ("theme.wav", b"audio", "audio/wav")},
    )

    assert response.status_code == 201
    alice = hosted.get("/api/intro/audio", headers=headers("alice")).json()
    bob = hosted.get("/api/intro/audio", headers=headers("bob")).json()
    assert [item["name"] for item in alice["imported"]] == ["theme.wav"]
    assert bob["imported"] == []


def test_intro_audio_rejects_non_audio_extensions(hosted):
    response = hosted.post(
        "/api/intro/audio",
        headers=headers("alice"),
        files={"file": ("payload.exe", b"not audio", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_jobs_and_job_websockets_are_isolated(hosted):
    registry = hosted.app.state.clipdesk
    alice = registry.for_identity("alice")
    job = alice.jobs.start("test", "-", lambda _bus: {"private": "alice"})

    assert hosted.get(f"/api/jobs/{job.id}", headers=headers("bob")).status_code == 404
    with hosted.websocket_connect(f"/ws/jobs/{job.id}", headers=headers("bob")) as socket:
        assert socket.receive_json()["type"] == "error"


def test_hosted_mode_never_exposes_server_local_onedrive_or_browser(hosted):
    sources = hosted.get("/api/sources", headers=headers("alice"))
    capability = hosted.get("/api/sessions/capability", headers=headers("alice"))
    sign_in = hosted.post(
        "/api/sessions/sign-in",
        headers=headers("alice"),
        json={"url": "https://onedrive.cloud.microsoft/:f:/a@tenant/S/token"},
    )

    assert sources.json() == {"roots": [], "available": False, "hosted": True}
    assert capability.json() == {"available": False, "browser": "", "hosted": True}
    assert sign_in.status_code == 403
    assert hosted.get("/api/sources/anything/browse", headers=headers("alice")).status_code == 403


def test_invalid_identity_values_are_rejected(hosted):
    assert hosted.get("/api/projects", headers=headers("")).status_code == 401
    assert hosted.get("/api/projects", headers=headers("x" * 257)).status_code == 401


def test_hosted_links_are_denied_without_an_administrator_allowlist(hosted):
    response = hosted.post(
        "/api/links/inspect",
        headers=headers("alice"),
        json={"url": "https://tenant.sharepoint.com/sites/Team/video.mp4"},
    )

    assert response.status_code == 403
    assert "allowed_link_hosts" in response.json()["detail"]


def test_hosted_link_wildcards_match_only_real_subdomains(tmp_path, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setattr(
        app_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("20.40.60.80", 443))],
    )
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setenv("CLIPDESK_PROXY_SECRET", "proxy-only-secret")
    settings = load_settings()
    settings.server.multi_user = True
    settings.server.allowed_link_hosts = ["*.sharepoint.com"]

    with TestClient(create_app(settings)) as client:
        allowed = client.post(
            "/api/links/inspect",
            headers=headers("alice"),
            json={"url": "https://tenant.sharepoint.com/sites/Team/video.mp4"},
        )
        lookalike = client.post(
            "/api/links/inspect",
            headers=headers("alice"),
            json={"url": "https://tenant.sharepoint.com.evil.invalid/video.mp4"},
        )

    assert allowed.status_code == 200
    assert lookalike.status_code == 403


def test_hosted_links_cannot_target_private_addresses(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setenv("CLIPDESK_PROXY_SECRET", "proxy-only-secret")
    settings = load_settings()
    settings.server.multi_user = True
    settings.server.allowed_link_hosts = ["127.0.0.1"]

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/links/inspect",
            headers=headers("alice"),
            json={"url": "http://127.0.0.1/admin"},
        )

    assert response.status_code == 403
    assert "private addresses" in response.json()["detail"]


def test_hosted_allowlisted_dns_name_cannot_resolve_private(tmp_path, monkeypatch):
    from clipdesk.server import app as app_module

    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    monkeypatch.setenv("CLIPDESK_PROXY_SECRET", "proxy-only-secret")
    monkeypatch.setattr(
        app_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("169.254.169.254", 443))],
    )
    settings = load_settings()
    settings.server.multi_user = True
    settings.server.allowed_link_hosts = ["media.example.com"]

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/links/inspect",
            headers=headers("alice"),
            json={"url": "https://media.example.com/video.mp4"},
        )

    assert response.status_code == 403
    assert "non-global" in response.json()["detail"]
