"""End-to-end checks against the HTTP API.

These exist because of a real miss: the whole suite passed while `server/app.py`
contained a syntax error, since nothing imported it. Anything that only breaks
when the app is constructed or a route is called was invisible. Building the app
is most of the value here; the route checks are the guards worth pinning down.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipdesk.config import load_settings
from clipdesk.models import AnalysisReport, ArtifactKind, MediaInfo
from clipdesk.server.app import create_app, safe_filename


@pytest.fixture
def client(tmp_path, monkeypatch):
    # CLIPDESK_HOME is the supported way to relocate everything, so the paths
    # follow from it rather than being poked in individually.
    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    settings = load_settings()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# --- the app builds at all ---------------------------------------------------
def test_health_responds(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert "version" in response.json()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_settings_round_trip(client):
    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["version"]


def test_copilot_model_settings_are_persisted(client):
    response = client.put(
        "/api/settings",
        json={
            "llm_model": "gpt-test",
            "vscode_reasoning_effort": "high",
            "vscode_context_window_tokens": 128000,
            "copilot_cli_model": "claude-test",
            "copilot_cli_reasoning_effort": "xhigh",
            "copilot_cli_context_window": "long_context",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["llm_model"] == "gpt-test"
    assert payload["vscode_reasoning_effort"] == "high"
    assert payload["vscode_context_window_tokens"] == 128000
    assert payload["copilot_cli_model"] == "claude-test"
    assert payload["copilot_cli_reasoning_effort"] == "xhigh"
    assert payload["copilot_cli_context_window"] == "long_context"


def test_a_tier_model_choice_can_be_undone(client):
    """One tier at a time, and "choose for me" has to actually stick.

    Two bugs made this worth pinning: settings files are deep-merged, so a key
    left out is a key kept, and the handler once read the settings the process
    started with instead of the current ones -- which quietly resurrected a
    cleared tier the next time any other tier was touched.
    """
    client.put("/api/settings", json={"llm_tier_models": {"small": "a-small-model"}})

    after_second_tier = client.put(
        "/api/settings", json={"llm_tier_models": {"strong": "a-strong-model"}}
    )
    assert after_second_tier.json()["llm_tier_models"] == {
        "small": "a-small-model",
        "balanced": "",
        "strong": "a-strong-model",
    }

    cleared = client.put("/api/settings", json={"llm_tier_models": {"small": ""}})
    assert cleared.json()["llm_tier_models"] == {
        "small": "",
        "balanced": "",
        "strong": "a-strong-model",
    }


def test_the_auto_plan_describes_every_pass(client):
    """The settings screen reads this to name the model behind each pass.

    Called without a level it has to fall back to the saved one -- the reason
    for the test, since the screen always sends one and the fallback was broken
    without anything noticing.
    """
    client.put("/api/settings", json={"llm_budget_level": 3})

    response = client.get("/api/llm/plan")

    assert response.status_code == 200
    plan = response.json()
    assert plan["level"] == 3
    assert [task["task"] for task in plan["tasks"]] == [
        "analyse",
        "notes",
        "article",
        "clips",
        "ask",
    ]
    for task in plan["tasks"]:
        assert task["tier"] in {"small", "balanced", "strong"}
        # Blank means nothing is pinned; the automatic pick is reported either
        # way so the control can name the default even while it is overridden.
        assert task["chosen"] == ""
        assert set(task) >= {"label", "tier", "model", "automatic", "chosen", "options"}


def test_the_auto_plan_follows_the_requested_level(client):
    lean = client.get("/api/llm/plan?level=1").json()
    best = client.get("/api/llm/plan?level=4").json()

    assert lean["level"] == 1 and best["level"] == 4
    # Bigger windows are the cheap end: fewer of them, so less repeated prompt.
    assert lean["window_chars"] > best["window_chars"]
    assert client.get("/api/llm/plan?level=99").json()["level"] == 4


def test_an_unknown_reasoning_effort_is_refused(client):
    response = client.put(
        "/api/settings", json={"copilot_cli_reasoning_effort": "unlimited"}
    )

    assert response.status_code == 422


def test_shutdown_stops_the_launched_server(client):
    calls = []
    client.app.state.clipdesk.shutdown_callback = lambda: calls.append("stopped")

    response = client.post("/api/shutdown", json={})

    assert response.json() == {"shutting_down": True, "terminated_jobs": 0}
    assert calls == ["stopped"]


def test_shutdown_requires_confirmation_while_jobs_are_active(client, monkeypatch):
    calls = []
    state = client.app.state.clipdesk
    state.shutdown_callback = lambda: calls.append("stopped")
    monkeypatch.setattr(state.jobs, "active_count", lambda project_id="": 2)

    response = client.post("/api/shutdown", json={})

    assert response.status_code == 409
    assert "2 background jobs" in response.json()["detail"]
    assert calls == []

    confirmed = client.post("/api/shutdown", json={"force": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["terminated_jobs"] == 2
    assert calls == ["stopped"]


def test_the_project_list_starts_empty(client):
    assert client.get("/api/projects").json() == []


# --- ids from the URL --------------------------------------------------------
# A malformed id is a normal thing to receive, not a server error.
@pytest.mark.parametrize(
    "project_id",
    # "." is absent on purpose: the HTTP client normalises it away before the
    # request is sent, so it tests the client rather than the app.
    ["../../etc", "..", "not a real id", "a/b", "%2e%2e"],
)
def test_a_malformed_project_id_is_a_404_not_a_500(client, project_id):
    response = client.get(f"/api/projects/{project_id}")

    assert response.status_code == 404


def test_an_unknown_project_is_a_404(client):
    assert client.get("/api/projects/missing-00000000").status_code == 404


def test_project_with_an_active_job_cannot_be_deleted(client, monkeypatch):
    project = client.post(
        "/api/projects",
        files={"video": ("meeting.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    monkeypatch.setattr(state.jobs, "active_count", lambda project_id="": 1)

    response = client.delete(f"/api/projects/{project['id']}")

    assert response.status_code == 409
    assert client.get(f"/api/projects/{project['id']}").status_code == 200


# --- upload guards -----------------------------------------------------------
def test_a_non_media_upload_is_refused(client):
    response = client.post(
        "/api/projects",
        files={"video": ("notes.txt", b"not a video", "text/plain")},
    )

    assert response.status_code == 400


def test_a_refused_upload_leaves_no_project_behind(client):
    client.post("/api/projects", files={"video": ("notes.txt", b"x", "text/plain")})

    assert client.get("/api/projects").json() == []


def test_an_analysis_failure_updates_the_project_status(client, monkeypatch):
    from clipdesk.server import app as app_module

    project = client.post(
        "/api/projects",
        files={"video": ("silent.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    monkeypatch.setattr(state, "ffmpeg", lambda: object())
    monkeypatch.setattr(
        app_module,
        "analyze_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("no audio track")),
    )

    response = client.post(
        f"/api/projects/{project['id']}/analyze",
        json={"skip_llm": True},
    )
    job = state.jobs.get(response.json()["job_id"])
    assert job is not None
    assert job.finished.wait(2)

    failed = client.get(f"/api/projects/{project['id']}").json()
    assert failed["status"] == "failed"
    assert failed["error"] == "no audio track"


def test_prompt_edit_preview_can_reference_an_imported_intro(client, monkeypatch):
    from clipdesk.server import app as app_module

    project = client.post(
        "/api/projects",
        files={"video": ("silent.mp4", b"video", "video/mp4")},
    ).json()
    client.post(
        f"/api/projects/{project['id']}/media",
        files={"file": ("Post.mp4", b"intro", "video/mp4")},
    )
    state = client.app.state.clipdesk.authenticate({})
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *args, **kwargs: SimpleNamespace(duration_s=60.0, has_audio=False),
    )
    response = client.post(
        f"/api/projects/{project['id']}/edit",
        json={"prompt": "add intro Post.mp4", "preview_only": True},
    )

    assert response.status_code == 200
    assert response.json()["plan"]["operations"] == ["Add Post.mp4"]


def test_intro_job_uses_the_selected_style_and_words(client, monkeypatch):
    from clipdesk.server import app as app_module

    project_payload = client.post(
        "/api/projects",
        files={"video": ("silent.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda *args, **kwargs: SimpleNamespace(duration_s=60.0, has_audio=False),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_soundtrack",
        lambda _audio, _duration, destination, _ffmpeg, **_kwargs: (
            destination.write_bytes(b"audio"), destination
        )[1],
    )
    captured = {}

    def fake_render(source, destination, plan, **kwargs):
        captured["style"] = kwargs["style"].id
        captured["title"] = kwargs["title"]
        captured["subtitle"] = kwargs["subtitle"]
        captured["end_card_text"] = kwargs["end_card_text"]
        captured["plan"] = plan
        destination.write_bytes(b"intro")
        return destination

    monkeypatch.setattr(app_module, "render_intro", fake_render)

    response = client.post(
        f"/api/projects/{project_payload['id']}/intro",
        json={
            "duration_seconds": 14,
            "shot_count": 4,
            "style_id": "momentum",
            "title": "Design review",
            "subtitle": "How the rollout stalled",
            "end_card_text": "Watch the session",
            "output_name": "opener.mp4",
        },
    )
    job = state.jobs.get(response.json()["job_id"])
    assert job is not None
    assert job.finished.wait(2)

    assert job.status == "done"
    assert captured["style"] == "momentum"
    assert captured["title"] == "Design review"
    assert captured["subtitle"] == "How the rollout stalled"
    assert captured["end_card_text"] == "Watch the session"
    assert captured["plan"].total_seconds == pytest.approx(14.0, abs=0.05)
    project = client.get(f"/api/projects/{project_payload['id']}").json()
    artifact = next(item for item in project["artifacts"] if item["filename"] == "opener.mp4")
    assert artifact["meta"]["style"] == "momentum"
    assert artifact["meta"]["title"] == "Design review"

    unknown = client.post(
        f"/api/projects/{project_payload['id']}/intro",
        json={"style_id": "not-a-style"},
    )
    assert unknown.status_code == 400


def test_short_intro_fits_overview_voiceover_to_total_duration(client, monkeypatch):
    from clipdesk.server import app as app_module

    project_payload = client.post(
        "/api/projects",
        files={"video": ("silent.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    project = state.store.get(project_payload["id"])
    project.save_analysis(
        AnalysisReport(
            project_id=project.id,
            media=MediaInfo(path=str(project.source_path), duration_s=60.0, has_audio=False),
            abstract=" ".join(f"overview{index}" for index in range(80)),
        )
    )
    monkeypatch.setattr(
        state,
        "ffmpeg",
        lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg"),
    )
    monkeypatch.setattr(
        app_module,
        "probe",
        lambda path, *_args, **_kwargs: SimpleNamespace(
            duration_s=4.0 if str(path).endswith("voiceover.wav") else 60.0,
            has_audio=False,
        ),
    )
    monkeypatch.setattr(app_module, "local_voiceover_available", lambda: True)

    def fake_voiceover(_text, destination, _voice_id=""):
        destination.write_bytes(b"voice")
        return destination

    monkeypatch.setattr(app_module, "synthesize_voiceover", fake_voiceover)
    monkeypatch.setattr(
        app_module,
        "prepare_soundtrack",
        lambda _audio, _duration, destination, _ffmpeg, **_kwargs: (
            destination.write_bytes(b"audio"), destination
        )[1],
    )
    monkeypatch.setattr(
        app_module,
        "render_intro",
        lambda _source, destination, _plan, **_kwargs: destination.write_bytes(b"video"),
    )

    response = client.post(
        f"/api/projects/{project.id}/intro",
        json={
            "duration_seconds": 6,
            "shot_count": 2,
            "style_id": "neon-pulse",
            "voiceover": True,
        },
    )
    job = state.jobs.get(response.json()["job_id"])
    assert job is not None
    assert job.finished.wait(2)
    assert job.status == "done"

    artifact = state.store.get(project.id).meta.artifacts[-1]
    assert artifact["duration_s"] == pytest.approx(6.0, abs=0.05)
    assert len(artifact["meta"]["narration"].split()) <= 12


def test_output_can_be_renamed_through_the_api(client):
    project_payload = client.post(
        "/api/projects",
        files={"video": ("silent.mp4", b"video", "video/mp4")},
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    project = state.store.get(project_payload["id"])
    output = project.output_path("transcript.md")
    output.write_text("# Transcript", encoding="utf-8")
    project.add_artifact(ArtifactKind.TRANSCRIPT, "Transcript", output)

    response = client.post(
        f"/api/projects/{project.id}/outputs/transcript.md/rename",
        json={"name": "meeting-transcript"},
    )

    assert response.status_code == 200
    assert response.json()["file"] == "meeting-transcript.md"
    assert project.output_path("meeting-transcript.md").is_file()
    refreshed = client.get(f"/api/projects/{project.id}").json()
    assert any(
        item["filename"] == "meeting-transcript.md" for item in refreshed["artifacts"]
    )


# --- the queue ---------------------------------------------------------------
@pytest.fixture
def analysed(client, monkeypatch):
    """A project with an analysis, so the queueable actions are all reachable."""
    payload = client.post(
        "/api/projects", files={"video": ("talk.mp4", b"video", "video/mp4")}
    ).json()
    state = client.app.state.clipdesk.authenticate({})
    project = state.store.get(payload["id"])
    project.save_analysis(
        AnalysisReport(
            project_id=project.id,
            media=MediaInfo(path=str(project.source_path), duration_s=120.0, has_audio=True),
        )
    )
    monkeypatch.setattr(
        state, "ffmpeg", lambda: SimpleNamespace(ffprobe="ffprobe", ffmpeg="ffmpeg")
    )
    return SimpleNamespace(id=payload["id"], state=state, project=project)


def test_the_cleanup_preview_ignores_the_queue_flag(client, analysed):
    # The flag is about when the work runs, not what the cut contains, so it
    # must not reach the options the plan is built from.
    response = client.post(
        f"/api/projects/{analysed.id}/cleanup/plan",
        json={"remove_silence": True, "queue": True},
    )

    assert response.status_code == 200
    assert "kept_seconds" in response.json()


def test_an_action_can_be_queued_instead_of_run(client, analysed):
    response = client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is True
    assert body["step"]["produces"] == "clean.mp4"
    assert client.app.state.clipdesk.authenticate({}).jobs.list(analysed.id) == []


def test_the_queue_lists_what_it_will_write(client, analysed):
    client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    )

    payload = client.get(f"/api/projects/{analysed.id}/queue").json()

    assert payload["count"] == 1
    assert payload["produces"] == ["clean.mp4"]


def test_a_queued_step_can_be_removed(client, analysed):
    step = client.post(
        f"/api/projects/{analysed.id}/cleanup", json={"queue": True}
    ).json()["step"]

    response = client.delete(f"/api/projects/{analysed.id}/queue/{step['id']}")

    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_removing_a_step_twice_says_so(client, analysed):
    step = client.post(
        f"/api/projects/{analysed.id}/cleanup", json={"queue": True}
    ).json()["step"]
    client.delete(f"/api/projects/{analysed.id}/queue/{step['id']}")

    assert client.delete(f"/api/projects/{analysed.id}/queue/{step['id']}").status_code == 404


def test_a_step_can_be_reordered(client, analysed):
    client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    )
    second = client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "tighter.mp4", "queue": True},
    ).json()["step"]

    response = client.post(
        f"/api/projects/{analysed.id}/queue/{second['id']}/move", json={"offset": -1}
    )

    assert response.json()["produces"] == ["tighter.mp4", "clean.mp4"]


def test_a_step_cannot_be_moved_past_the_end(client, analysed):
    step = client.post(
        f"/api/projects/{analysed.id}/cleanup", json={"queue": True}
    ).json()["step"]

    response = client.post(
        f"/api/projects/{analysed.id}/queue/{step['id']}/move", json={"offset": -1}
    )

    assert response.status_code == 409


def test_running_the_queue_chains_the_steps(client, analysed):
    client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    )
    client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "tighter.mp4", "queue": True},
    )

    payload = client.post(f"/api/projects/{analysed.id}/queue/run").json()
    jobs = {job["id"]: job for job in payload["jobs"]}

    assert len(payload["job_ids"]) == 2
    first, second = payload["job_ids"]
    assert jobs[first]["depends_on"] == []
    assert jobs[second]["depends_on"] == [first]
    assert client.get(f"/api/projects/{analysed.id}/queue").json()["count"] == 0


def test_running_an_empty_queue_says_so(client, analysed):
    assert client.post(f"/api/projects/{analysed.id}/queue/run").status_code == 409


def test_running_something_directly_puts_the_queue_first(client, analysed):
    # The whole point: an edit of a file the queue has not written yet only
    # works if the queued step happens first.
    queued = client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    ).json()["step"]

    direct = client.post(
        f"/api/projects/{analysed.id}/cleanup", json={"output_name": "again.mp4"}
    ).json()

    assert queued["produces"] == "clean.mp4"
    assert len(direct["after"]) == 1
    assert client.get(f"/api/projects/{analysed.id}/queue").json()["count"] == 0

    jobs = {job["id"]: job for job in client.get(f"/api/jobs?project_id={analysed.id}").json()["jobs"]}
    assert jobs[direct["job_id"]]["depends_on"] == direct["after"]


def test_the_queue_can_be_emptied(client, analysed):
    client.post(f"/api/projects/{analysed.id}/cleanup", json={"queue": True})

    response = client.delete(f"/api/projects/{analysed.id}/queue")

    assert response.json() == {"removed": 1, "steps": [], "count": 0, "produces": []}


def test_an_export_may_name_a_file_the_queue_will_write(client, analysed):
    client.post(
        f"/api/projects/{analysed.id}/cleanup",
        json={"output_name": "clean.mp4", "queue": True},
    )

    response = client.post(
        f"/api/projects/{analysed.id}/export",
        json={"source": "clean.mp4", "format": "mp4", "quality": "small", "queue": True},
    )

    assert response.status_code == 200
    assert response.json()["step"]["produces"] == "clean-small.mp4"


def test_an_export_of_a_file_nobody_will_write_is_refused(client, analysed):
    response = client.post(
        f"/api/projects/{analysed.id}/export",
        json={"source": "imaginary.mp4", "format": "mp4", "quality": "small"},
    )

    assert response.status_code == 404


# --- articles ----------------------------------------------------------------
def test_article_options_describe_the_template(client):
    payload = client.get("/api/article/options").json()

    assert [item["id"] for item in payload["formats"]] == ["docx", "md"]
    assert "Symptoms/Summary" in payload["template_sections"]


def test_an_article_needs_an_analysis(client):
    payload = client.post(
        "/api/projects", files={"video": ("talk.mp4", b"video", "video/mp4")}
    ).json()

    response = client.post(f"/api/projects/{payload['id']}/article", json={"format": "md"})

    assert response.status_code == 409


def test_an_article_can_be_queued(client, analysed):
    response = client.post(
        f"/api/projects/{analysed.id}/article",
        json={"format": "md", "shape": "how-to", "queue": True},
    )

    assert response.status_code == 200
    assert response.json()["step"]["produces"] == "article.md"


def test_an_unknown_article_type_is_refused(client, analysed):
    response = client.post(
        f"/api/projects/{analysed.id}/article", json={"format": "md", "shape": "essay"}
    )

    assert response.status_code == 422


def test_a_word_output_can_be_read_back_for_previewing(client, analysed, monkeypatch):
    from clipdesk.actions import docxtemplate
    from clipdesk.actions.article import Article, render_template_docx

    # The client fixture relocates CLIPDESK_HOME, so point at the real template.
    real = Path(__file__).resolve().parent.parent / docxtemplate.TEMPLATE_RELATIVE
    if not real.is_file():
        pytest.skip("the Word template is not present")
    monkeypatch.setattr(docxtemplate, "template_path", lambda: real)

    render_template_docx(
        analysed.project.output_path("article.docx"),
        Article(title="How TCP works", symptoms=["It is connection-oriented."]),
    )

    payload = client.get(f"/api/projects/{analysed.id}/outputs/article.docx/document").json()

    assert "How TCP works" in payload["markdown"]
    assert "It is connection-oriented." in payload["markdown"]


def test_only_word_outputs_can_be_read_back(client, analysed):
    analysed.project.output_path("notes.md").write_text("# Notes", encoding="utf-8")

    response = client.get(f"/api/projects/{analysed.id}/outputs/notes.md/document")

    assert response.status_code == 400


def test_reading_back_a_file_that_is_not_there_says_so(client, analysed):
    response = client.get(f"/api/projects/{analysed.id}/outputs/missing.docx/document")

    assert response.status_code == 404


def test_a_word_file_that_is_not_a_word_file_is_refused(client, analysed):
    analysed.project.output_path("broken.docx").write_bytes(b"not a zip")

    response = client.get(f"/api/projects/{analysed.id}/outputs/broken.docx/document")

    assert response.status_code == 422


# --- filename sanitising -----------------------------------------------------
@pytest.mark.parametrize(
    ("given", "banned"),
    [
        ("../../evil.mp4", ".."),
        ("..\\..\\evil.mp4", ".."),
        ("sub/dir/clip.mp4", "/"),
        ("C:\\Windows\\evil.mp4", "\\"),
    ],
)
def test_upload_names_cannot_carry_a_path(given, banned):
    assert banned not in safe_filename(given)


def test_a_name_that_sanitises_to_nothing_falls_back():
    assert safe_filename("...", fallback="video.mp4") == "video.mp4"


# --- link import -------------------------------------------------------------
def test_a_non_url_is_refused(client):
    response = client.post("/api/projects/from-link", json={"url": "not a link"})

    assert response.status_code == 400


def test_an_unsupported_cookie_browser_is_refused(client):
    response = client.post(
        "/api/projects/from-link",
        json={"url": "https://example.invalid/a.mp4", "browser_cookies": "netscape"},
    )

    assert response.status_code == 400


# --- health probe caching ----------------------------------------------------
# The UI polls health every few seconds and a probe is not free, so it must be
# reused rather than re-run per request.
def test_the_provider_probe_is_reused_between_requests(client, monkeypatch):
    from clipdesk.server import app as app_module

    calls = []
    state = client.app.state.clipdesk
    monkeypatch.setattr(state.settings.llm, "provider", "copilot_cli")
    state.invalidate_probes()

    original = app_module.LLMClient.from_settings

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module.LLMClient, "from_settings", counting)

    client.get("/api/health")
    client.get("/api/health")

    assert len(calls) == 1


def test_the_vscode_probe_is_not_cached(client, monkeypatch):
    # The bridge alert watches this to confirm the moment a bridge comes up, so
    # caching it would make that feedback look broken.
    state = client.app.state.clipdesk
    monkeypatch.setattr(state.settings.llm, "provider", "vscode")

    assert state.provider_probe_ttl() == 0.0


# --- websocket origin --------------------------------------------------------
# WebSocket handshakes are not covered by CORS, so a page on any other origin
# could otherwise open this socket and read job progress.
def test_the_job_socket_rejects_a_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        "/ws/jobs/whatever", headers={"origin": "https://evil.invalid"}
    ) as socket:
        socket.receive_text()


def test_the_job_socket_accepts_its_own_origin(client):
    port = client.app.state.clipdesk.settings.server.port

    with client.websocket_connect(
        "/ws/jobs/missing", headers={"origin": f"http://127.0.0.1:{port}"}
    ) as socket:
        assert socket.receive_json()["type"] == "error"


def test_the_job_socket_accepts_a_client_that_sends_no_origin(client):
    with client.websocket_connect("/ws/jobs/missing") as socket:
        assert socket.receive_json()["type"] == "error"


# --- the port actually in use ------------------------------------------------
# The allow-lists are built from the configured port, so an overridden one has to
# be written back into settings or the UI serves from an unrecognised origin.
def test_an_overridden_port_reaches_the_settings(monkeypatch, tmp_path):
    import uvicorn

    from clipdesk import __main__ as cli

    monkeypatch.setenv("CLIPDESK_HOME", str(tmp_path))
    captured = {}

    def fake_config(app, **kwargs):
        captured["port"] = kwargs["port"]
        captured["origins"] = app.state.clipdesk.settings.server.port
        return SimpleNamespace(app=app)

    class FakeServer:
        should_exit = False

        def __init__(self, config):
            self.config = config

        def run(self):
            self.config.app.state.clipdesk.shutdown_callback()
            captured["should_exit"] = self.should_exit

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    cli._serve(
        SimpleNamespace(
            config=None, host=None, port=9911, bootstrap=False, no_whisper=True, no_browser=True
        )
    )

    assert captured["port"] == 9911
    # The app has to agree, or its CORS and WebSocket allow-lists name a port
    # the browser is not using.
    assert captured["origins"] == 9911
    assert captured["should_exit"] is True


def test_one_blip_from_the_bridge_does_not_report_no_model(client, monkeypatch):
    """The bridge is probed live on every poll, so a slow answer is normal.

    Reporting it as down flipped the whole UI to "no model" and raised the setup
    alert, only for the next poll to clear it. It has to repeat to be believed.
    """
    from types import SimpleNamespace

    from clipdesk.server import app as app_module

    state = client.app.state.clipdesk.authenticate({})
    good = SimpleNamespace(available=True, to_dict=lambda: {"available": True})
    bad = SimpleNamespace(available=False, to_dict=lambda: {"available": False})

    answers = [good, bad, bad]
    monkeypatch.setattr(state, "probe", lambda *a, **k: answers.pop(0))

    assert state.provider_status() is good
    assert state.provider_status() is good, "one failure should be ignored"
    assert state.provider_status() is bad, "a repeat failure is believed"
