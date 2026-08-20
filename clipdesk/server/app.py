"""The HTTP + WebSocket API, and the static UI.

Everything the browser can do is here. The rules this file follows:

* Long work never blocks the event loop — it becomes a job and streams progress.
* Every path that comes from the client is resolved through the project store,
  which refuses anything that escapes the project folder.
* Errors come back as a message a person can act on, not a stack trace.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from clipdesk import __version__
from clipdesk.actions import (
    ASSET_SUFFIXES,
    CATALOG_STYLES,
    CleanupOptions,
    IntroPlan,
    IntroScene,
    OutputError,
    PromptContext,
    apply_bookends,
    ask as ask_report,
    bundle_outputs,
    copy_into_project,
    delete_output,
    delete_source,
    explain_cleanup,
    export_options,
    export_summary,
    export_transcript,
    find_candidates,
    fit_narration,
    generate_article,
    generate_notes,    list_assets,
    list_media,
    media_dir,
    available_styles,
    import_custom_style,
    install_catalog_style,
    parse_edit_prompt,
    parse_edit_program,
    plan_cleanup,
    plan_export,
    plan_intro,
    plan_outro,
    plan_prompt,
    render_cleanup,
    render_edit,
    render_export,
    render_intro,
    render_program,
    resolve_style,
    render_selection,
    resolve_asset,
    resolve_attachment,
    resolve_media,
    reveal,
    rename_output,
    shot_labels,
    source_info,
    store_media,
)
from clipdesk.actions.article import ARTICLE_FORMATS, ARTICLE_SHAPES, SHAPE_LABEL
from clipdesk.actions.docxtemplate import TemplateError, read_markdown, template_available
from clipdesk.actions.exports import TRANSCRIPT_FORMATS
from clipdesk.analysis.heuristics import format_duration
from clipdesk.bootstrap import (
    ProvisionError,
    component_statuses,
    provision,
    provision_all,
)
from clipdesk.config import Settings, load_settings, save_local_overrides
from clipdesk.events import EventBus
from clipdesk.ingest import (
    SUPPORTED_BROWSERS,
    FetchError,
    ytdlp_available,
)
from clipdesk.ingest import (
    describe as describe_link,
)
from clipdesk.ingest import (
    fetch as fetch_video,
)
from clipdesk.ingest.browsersignin import (
    SignInError,
    find_browser,
    recalled_resolution,
    remember_resolution,
)
from clipdesk.ingest.browsersignin import (
    sign_in as browser_sign_in,
)
from clipdesk.ingest.cookies import (
    CookieBundle,
    CookieError,
    find_for_url,
    parse_pasted,
    save_for_host,
    saved_hosts,
)
from clipdesk.ingest.cookies import (
    forget as forget_cookies,
)
from clipdesk.ingest.fetch import cookie_header_from_jar
from clipdesk.ingest.localsource import (
    BrowseError,
    cloud_roots,
    copy_into,
    find_root,
    is_cloud_only,
    resolve_within,
)
from clipdesk.ingest.localsource import (
    browse as browse_folder,
)
from clipdesk.ingest.localsource import (
    search as search_folder,
)
from clipdesk.ingest.sharepoint import (
    SharePointError,
    file_url,
    folder_path,
    list_folder,
)
from clipdesk.ingest.sharepoint import (
    resolve as resolve_share,
)
from clipdesk.llm import PRESETS, LLMClient, all_statuses, extension_state
from clipdesk.llm.budget import LEVELS as BUDGET_LEVELS
from clipdesk.llm.budget import TASKS as BUDGET_TASKS
from clipdesk.llm.budget import TASK_LABELS, budget_for, pick_model, rank_models
from clipdesk.llm.credits import credits_for_tokens
from clipdesk.llm.presets import get as get_preset
from clipdesk.llm.registry import build_provider
from clipdesk.flows import (
    FlowAssembleStep,
    FlowBookendStep,
    FlowCleanupStep,
    FlowClipStep,
    FlowDefinition,
    FlowHighlightStep,
    FlowNotesStep,
    FlowPromptStep,
    delete_flow,
    load_flows,
    save_flow,
)
from clipdesk.media.ffmpeg import find_tools
from clipdesk.media.soundtrack import (
    AUDIO_SUFFIXES,
    audio_dir,
    list_audio,
    prepare_soundtrack,
    preview_audio,
    resolve_audio,
    store_audio,
)
from clipdesk.media.probe import probe
from clipdesk.media.voiceover import available_voices, local_voiceover_available, synthesize_voiceover
from clipdesk.models import AnalysisReport, ArtifactKind, ClipCandidate
from clipdesk.paths import isolated_user_dir, user_state_dir
from clipdesk.pipeline import analyze_project
from clipdesk.server.jobs import KIND_LABEL, KIND_TAB, JobManager
from clipdesk.server.schemas import (
    AnalyzeRequest,
    ArticleRequest,
    AskRequest,
    AssetLinksRequest,
    BookendRequest,
    CleanupRequest,
    ClipFindRequest,
    ClipRenderRequest,
    ExportRequest,
    ImportLinkRequest,
    InspectLinkRequest,
    IntroRequest,
    IntroStyleImportRequest,
    IntroStyleInstallRequest,
    LocalImportRequest,
    MediaAdoptRequest,
    NotesRequest,
    OutputRenameRequest,
    OutputSelection,
    PromptEditRequest,
    ProvisionRequest,
    QueueMoveRequest,
    SessionSaveRequest,
    SettingsUpdate,
    ShutdownRequest,
    TranscriptExportRequest,
)
from clipdesk.server.sequence import Sequence
from clipdesk.store import Project, ProjectStore
from clipdesk.transcription import SUPPORTED_SUFFIXES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
}

_UPLOAD_CHUNK = 1024 * 1024 * 4
#: A ten-hour subtitle file is a few megabytes; 64 MB is generous by a wide margin.
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def safe_filename(name: str, *, fallback: str = "upload") -> str:
    """Strip anything that could steer a write outside the intended folder."""
    cleaned = _SAFE_NAME_RE.sub("_", Path(name).name).strip(". ")
    return cleaned or fallback


def video_output_name(name: str, *, fallback: str) -> str:
    cleaned = safe_filename(name or fallback, fallback=fallback)
    if not Path(cleaned).suffix:
        cleaned += ".mp4"
    if Path(cleaned).suffix.lower() != ".mp4":
        raise HTTPException(status_code=400, detail="Video output names must end in .mp4.")
    return cleaned


def _first_sentence(text: str, limit: int = 150) -> str:
    """The opening line of the overview, for use as an intro subtitle."""
    clean = " ".join(text.split())
    if not clean:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", clean)[0]
    return sentence[:limit].rstrip(" .") if len(sentence) > limit else sentence


def _is_local_only(settings: Settings) -> bool:
    """True when the server is bound to the loopback interface."""
    return settings.server.host in {"127.0.0.1", "localhost", "::1"}


def _require_link_allowed(app_state: UserState, url: str) -> None:
    """Fail closed on hosted outbound links unless the administrator trusts the host."""
    if not app_state.hosted:
        return
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="That hosted link is not a safe HTTP URL.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise HTTPException(status_code=403, detail="Hosted links cannot target private addresses.")

    patterns = app_state.settings.server.allowed_link_hosts
    allowed = any(
        host == pattern.lower().rstrip(".")
        or (
            pattern.startswith("*.")
            and host.endswith(pattern[1:].lower().rstrip("."))
            and host != pattern[2:].lower().rstrip(".")
        )
        for pattern in patterns
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Hosted outbound access to '{host}' is not allowed. An administrator "
                "must add it to server.allowed_link_hosts."
            ),
        )

    try:
        resolved = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Hosted outbound host '{host}' could not be resolved safely.",
        ) from exc
    if not resolved or any(not address.is_global for address in resolved):
        raise HTTPException(
            status_code=403,
            detail="Hosted links cannot resolve to private or non-global addresses.",
        )


class UserState:
    #: How long a health probe is reused. The UI polls health every few seconds,
    #: and a probe is not free: the CLI provider spawns a process, a hosted
    #: provider makes a billable network call, and the component check walks the
    #: model directory. None of that changes second to second.
    PROBE_TTL_S = 10.0

    def __init__(
        self,
        settings: Settings,
        *,
        identity: str,
        state_dir: Path,
        settings_path: Path | None = None,
        workspace_dir: Path | None = None,
        hosted: bool = False,
    ) -> None:
        self.identity = identity
        self.state_dir = state_dir
        self.settings_path = settings_path
        self.hosted = hosted
        self._base_settings = settings.model_copy(deep=True)
        if settings_path is not None:
            settings = self._isolated_settings(workspace_dir)
        self.settings = settings
        self.store = ProjectStore(settings.paths.workspace_dir)
        self.jobs = JobManager()
        self.sequence = Sequence()
        self.shutdown_callback: Callable[[], None] | None = None
        self._probe_lock = threading.Lock()
        self._probe_cache: dict[str, tuple[float, Any]] = {}
        self._last_good_provider: Any = None
        self._provider_failures = 0

    def reload_settings(self) -> None:
        if self.settings_path is None:
            self.settings = load_settings()
        else:
            self.settings = self._isolated_settings(self.settings.paths.workspace_dir)
        self.store = ProjectStore(self.settings.paths.workspace_dir)
        self.invalidate_probes()

    def _isolated_settings(self, workspace_dir: Path | None) -> Settings:
        settings = load_settings(self.settings_path)
        settings.server = self._base_settings.server.model_copy(deep=True)
        # Hosted users may choose models, but provider destinations and secret
        # names are operator-owned. Otherwise an override can forward a process
        # environment secret to an attacker-controlled endpoint.
        settings.llm.openai_compat.base_url = self._base_settings.llm.openai_compat.base_url
        settings.llm.openai_compat.auth_style = self._base_settings.llm.openai_compat.auth_style
        settings.llm.openai_compat.api_key_env = self._base_settings.llm.openai_compat.api_key_env
        settings.llm.anthropic.base_url = self._base_settings.llm.anthropic.base_url
        settings.llm.anthropic.api_key_env = self._base_settings.llm.anthropic.api_key_env
        settings.paths.vendor = str(self._base_settings.paths.vendor_dir)
        settings.paths.workspace = str(
            workspace_dir or self._base_settings.paths.workspace_dir
        )
        return settings

    def invalidate_probes(self) -> None:
        with self._probe_lock:
            self._probe_cache.clear()

    def probe(self, key: str, produce: Callable[[], Any], *, ttl: float | None = None) -> Any:
        """Run ``produce`` at most once per TTL.

        The lock guards the dictionary, not the probe itself: two requests
        arriving together may both probe, which wastes one call but never blocks
        a request behind someone else's network timeout.
        """
        window = self.PROBE_TTL_S if ttl is None else ttl
        if window <= 0:
            return produce()

        now = time.monotonic()
        with self._probe_lock:
            cached = self._probe_cache.get(key)
            if cached is not None and now - cached[0] < window:
                return cached[1]

        value = produce()
        with self._probe_lock:
            self._probe_cache[key] = (time.monotonic(), value)
        return value

    def provider_probe_ttl(self) -> float:
        """The VS Code bridge is probed over loopback and costs nothing, and the
        UI watches it to confirm the moment a bridge comes up. Caching it would
        make that feedback feel broken. Every other provider is expensive: the
        CLI spawns a process, and a hosted provider makes a billable call.
        """
        return 0.0 if self.settings.llm.provider == "vscode" else self.PROBE_TTL_S

    def provider_status(self) -> Any:
        """The provider's status, ignoring a single blip.

        The bridge is probed live on every poll, so one slow answer -- VS Code
        busy, or the handshake being rewritten -- used to flip the whole UI to
        "no model" and raise the setup alert, only for the next poll to clear
        it. A failure has to repeat before it is believed.
        """
        status = self.probe(
            "provider", lambda: self.llm().status(), ttl=self.provider_probe_ttl()
        )
        if getattr(status, "available", False):
            self._last_good_provider = status
            self._provider_failures = 0
            return status

        self._provider_failures += 1
        if self._provider_failures < 2 and self._last_good_provider is not None:
            return self._last_good_provider
        return status

    def llm(self, provider_key: str | None = None, *, duration_s: float = 0.0) -> LLMClient:
        return LLMClient.from_settings(self.settings, provider_key, duration_s=duration_s)

    def ffmpeg(self):
        tools = find_tools(self.settings.paths.vendor_dir)
        if tools is None:
            raise HTTPException(
                status_code=503,
                detail="ffmpeg is not installed yet. Open Setup and install the "
                "bundled copy, then try again.",
            )
        return tools


class AppState:
    """Registry of strictly isolated user contexts.

    Localhost keeps the historical single context. Hosted mode trusts an
    identity only when an authenticating reverse proxy also supplies the shared
    upstream secret.
    """

    def __init__(self, settings: Settings) -> None:
        self.base_settings = settings
        self.multi_user = settings.server.multi_user
        if not _is_local_only(settings) and not self.multi_user:
            raise RuntimeError(
                "A non-loopback ClipDesk server must set server.multi_user=true and "
                "run behind an authenticating reverse proxy."
            )
        self._lock = threading.Lock()
        self._users: dict[str, UserState] = {}
        self._local = UserState(
            settings,
            identity="local",
            state_dir=user_state_dir(),
            hosted=False,
        )

    @property
    def settings(self) -> Settings:
        return self.base_settings

    @property
    def store(self) -> ProjectStore:
        return self._local.store

    @property
    def jobs(self) -> JobManager:
        return self._local.jobs

    @property
    def shutdown_callback(self) -> Callable[[], None] | None:
        return self._local.shutdown_callback

    @shutdown_callback.setter
    def shutdown_callback(self, callback: Callable[[], None] | None) -> None:
        self._local.shutdown_callback = callback

    def invalidate_probes(self) -> None:
        self._local.invalidate_probes()

    def provider_probe_ttl(self) -> float:
        return self._local.provider_probe_ttl()

    def authenticate(self, headers: Mapping[str, str]) -> UserState:
        if not self.multi_user:
            return self._local

        config = self.base_settings.server
        expected = os.environ.get(config.proxy_secret_env, "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail=f"Hosted authentication is not configured: {config.proxy_secret_env} is empty.",
            )
        supplied = headers.get(config.proxy_secret_header, "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="The authenticating proxy context is missing.")

        identity = headers.get(config.identity_header, "").strip()
        if not identity or len(identity) > 256 or any(ord(char) < 32 for char in identity):
            raise HTTPException(status_code=401, detail="The authenticated user identity is invalid.")
        return self.for_identity(identity)

    def for_identity(self, identity: str) -> UserState:
        with self._lock:
            existing = self._users.get(identity)
            if existing is not None:
                return existing

            state_root = isolated_user_dir(user_state_dir(), identity)
            workspace = isolated_user_dir(
                self.base_settings.paths.workspace_dir, identity
            )
            created = UserState(
                self.base_settings,
                identity=identity,
                state_dir=state_root,
                settings_path=state_root / "settings.yaml",
                workspace_dir=workspace,
                hosted=True,
            )
            self._users[identity] = created
            return created


class RevalidatingStatics(StaticFiles):
    """Serve the UI with `no-cache`.

    The default headers let a browser hold on to `app.js` across an update, which
    silently serves a stale UI against a new API — and the symptom (a screen that
    is subtly out of date) is far harder to diagnose than a cache miss. ETags are
    still sent, so revalidation costs a 304 rather than a re-download.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return super().is_not_modified(response_headers, request_headers)

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache, must-revalidate"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    state = AppState(settings or load_settings())
    app = FastAPI(title="ClipDesk", version=__version__, docs_url="/api/docs")
    app.state.clipdesk = state

    origins = [
        f"http://127.0.0.1:{state.settings.server.port}",
        f"http://localhost:{state.settings.server.port}",
        *state.settings.server.allowed_origins,
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["x-frame-options"] = "DENY"
        response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path not in {"/api/docs", "/api/openapi.json"}:
            response.headers["content-security-policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; "
                "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'"
            )
        return response

    def current(request: Request) -> UserState:
        return state.authenticate(request.headers)

    # --- helpers -----------------------------------------------------------
    def require_project(project_id: str, app_state: UserState) -> Project:
        project = app_state.store.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'.")
        return project

    def require_report(project: Project) -> AnalysisReport:
        report = project.load_analysis()
        if report is None:
            raise HTTPException(
                status_code=409,
                detail="This video has not been analysed yet. Run Analyse first.",
            )
        return report

    def start_job(
        app_state: UserState,
        kind: str,
        project: Project,
        work: Callable[[EventBus], dict[str, Any]],
        *,
        label: str = "",
        tab: str = "",
        meta: dict[str, Any] | None = None,
        queue: bool = False,
        produces: str = "",
    ):
        """Run this work, or line it up behind whatever is already queued.

        Running directly while steps are waiting does not jump the queue: the
        pending steps go in first and this job waits for them. Asking for a
        clean cut and then editing its output only works if the clean cut
        happens first, and the user should not have to remember that.
        """
        label = label or KIND_LABEL.get(kind, kind)
        tab = tab or KIND_TAB.get(kind, "")
        if queue:
            step = app_state.sequence.add(
                project.id,
                kind,
                work,
                label=label,
                produces=produces,
                tab=tab,
                meta=meta or {},
            )
            return {
                "queued": True,
                "step": step.summary(),
                "project_id": project.id,
                "kind": kind,
                "tab": tab,
                "queue_length": len(app_state.sequence.list(project.id)),
            }

        after = flush_sequence(app_state, project)
        job = app_state.jobs.start(
            kind,
            project.id,
            work,
            label=label,
            tab=tab,
            meta=meta,
            depends_on=after[-1:],
        )
        return {
            "job_id": job.id,
            "project_id": project.id,
            "kind": kind,
            "status": job.status,
            "tab": job.tab,
            "queued": False,
            "after": after,
        }

    def flush_sequence(app_state: UserState, project: Project) -> list[str]:
        """Hand every pending step to the job manager, each waiting on the last."""
        started: list[str] = []
        for step in app_state.sequence.take(project.id):
            job = app_state.jobs.start(
                step.kind,
                project.id,
                step.work,
                label=step.label,
                tab=step.tab,
                meta=step.meta,
                depends_on=started[-1:],
            )
            started.append(job.id)
        return started

    def pending_outputs(app_state: UserState, project: Project) -> list[str]:
        return app_state.sequence.outputs(project.id)

    def track_tokens(project: Project, llm: LLMClient | None) -> None:
        """Fold one action's usage into the project's running total."""
        if llm is not None:
            project.record_tokens(llm.meter.to_dict())

    def artifact_payload(project: Project) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for entry in project.meta.artifacts:
            filename = str(entry.get("filename", ""))
            path = project.output_dir / filename
            item = dict(entry)
            item["exists"] = path.is_file()
            item["url"] = f"/api/projects/{project.id}/outputs/{filename}"
            payload.append(item)
        return payload

    def with_credits(payload: dict[str, Any]) -> dict[str, Any]:
        """Price the recorded tokens on read, so a rate change reprices history."""
        tokens = payload.get("tokens")
        if isinstance(tokens, dict) and tokens.get("total_tokens"):
            payload["tokens"] = {**tokens, **credits_for_tokens(tokens)}
        return payload

    def project_payload(project: Project) -> dict[str, Any]:
        return with_credits(
            {
                **project.meta.to_dict(),
                "artifacts": artifact_payload(project),
                "source_exists": project.source_path.is_file(),
            }
        )

    # --- health & setup ----------------------------------------------------
    @app.get("/api/health")
    def health(app_state: UserState = Depends(current)) -> dict[str, Any]:
        tools = find_tools(app_state.settings.paths.vendor_dir)
        components = app_state.probe(
            "components",
            lambda: component_statuses(
                app_state.settings.paths.vendor_dir, app_state.settings.transcription.model
            ),
        )
        provider = app_state.provider_status()
        ready = tools is not None
        return {
            "version": __version__,
            "hosted": app_state.hosted,
            "ready": ready,
            "ffmpeg": {
                "available": tools is not None,
                "source": tools.source if tools else "",
                "path": tools.ffmpeg if tools else "",
            },
            "components": [component.to_dict() for component in components],
            "llm": provider.to_dict(),
            "bridge": extension_state(app_state.settings.llm.vscode.handshake_file),
            "downloader": {
                "available": ytdlp_available(app_state.settings.paths.vendor_dir)
            },
            "workspace": str(app_state.settings.paths.workspace_dir),
        }

    @app.get("/api/setup")
    def setup(app_state: UserState = Depends(current)) -> dict[str, Any]:
        return {
            "components": [
                component.to_dict()
                for component in component_statuses(
                    app_state.settings.paths.vendor_dir,
                    app_state.settings.transcription.model,
                )
            ],
            "providers": [status.to_dict() for status in all_statuses(app_state.settings.llm)],
            "active_provider": app_state.settings.llm.provider,
            "whisper_model": app_state.settings.transcription.model,
            "vendor_dir": str(app_state.settings.paths.vendor_dir),
        }

    @app.post("/api/setup/provision")
    def setup_provision(
        request: ProvisionRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        if app_state.hosted:
            raise HTTPException(
                status_code=403,
                detail="Dependency provisioning is controlled by the hosted server administrator.",
            )
        vendor = app_state.settings.paths.vendor_dir
        model = app_state.settings.transcription.model
        component = request.component

        def work(bus: EventBus) -> dict[str, Any]:
            stage = f"provision:{component}"
            bus.stage_start(stage, f"Installing {component}")

            def report(fraction: float | None, message: str) -> None:
                bus.progress(stage, fraction, message)

            try:
                if component == "all":
                    provision_all(vendor, model, report)
                else:
                    provision(component, vendor, model, report)
            except ProvisionError as exc:
                raise RuntimeError(str(exc)) from exc
            # Something just appeared on disk, so the cached health probe is
            # wrong rather than merely stale.
            app_state.invalidate_probes()
            bus.stage_end(stage, "Installed")
            return {
                "components": [
                    status.to_dict() for status in component_statuses(vendor, model)
                ]
            }

        job = app_state.jobs.start(
            "provision",
            "-",
            work,
            label=f"Installing {component}",
        )
        return {"job_id": job.id, "kind": "provision", "project_id": "-"}

    @app.get("/api/llm/plan")
    def llm_plan(
        level: int | None = None, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """What auto mode would use at a level, for the settings screen to show.

        Worked out here rather than in the browser so the ranking that actually
        routes the calls is the same one the user is shown.
        """
        settings = app_state.settings
        chosen = settings.llm.budget_level if level is None else max(0, min(4, level))
        budget = budget_for(chosen)
        try:
            models = list(build_provider(settings.llm).status().models)
        except Exception:  # noqa: BLE001
            models = []
        return {
            "level": budget.level,
            "label": budget.label,
            "note": budget.note,
            "window_chars": budget.window_chars,
            "notes_word_target": budget.notes_word_target,
            "include_diagrams": budget.include_diagrams,
            "max_enrichment": budget.max_enrichment,
            "tasks": [
                {
                    "task": task,
                    "label": TASK_LABELS[task],
                    "tier": budget.tier_for(task),
                    "model": pick_model(
                        models,
                        budget.tier_for(task),
                        settings.llm.tier_models.get(budget.tier_for(task), ""),
                    ),
                    # What auto lands on with no preference, reported separately
                    # so the control can name the default even while overridden.
                    "automatic": pick_model(models, budget.tier_for(task), ""),
                    # Blank unless the user has picked one, so the control can
                    # tell an override apart from agreeing with the default.
                    "chosen": settings.llm.tier_models.get(budget.tier_for(task), ""),
                    # Everything of a similar size, so a Gemini, Grok or GPT
                    # equivalent can be chosen without leaving auto.
                    "options": rank_models(models).get(budget.tier_for(task), []),
                }
                for task in BUDGET_TASKS
            ],
        }

    # --- settings ----------------------------------------------------------
    @app.get("/api/settings")
    def get_settings(app_state: UserState = Depends(current)) -> dict[str, Any]:
        settings = app_state.settings
        return {
            "version": __version__,
            "llm_provider": settings.llm.provider,
            "llm_auto": settings.llm.auto,
            "llm_budget_level": settings.llm.budget_level,
            "llm_tier_models": dict(settings.llm.tier_models),
            "llm_budget_levels": [
                {
                    "level": item.level,
                    "label": item.label,
                    "note": item.note,
                    "diagrams": item.include_diagrams,
                    "max_enrichment": item.max_enrichment,
                }
                for item in BUDGET_LEVELS
            ],
            "llm_model": settings.llm.vscode.model or "",
            "vscode_reasoning_effort": settings.llm.vscode.reasoning_effort or "",
            "vscode_context_window_tokens": settings.llm.vscode.context_window_tokens or 0,
            "copilot_cli_model": settings.llm.copilot_cli.model or "",
            "copilot_cli_reasoning_effort": settings.llm.copilot_cli.reasoning_effort or "",
            "copilot_cli_context_window": settings.llm.copilot_cli.context_window,
            "llm_preset": settings.llm.openai_compat.preset,
            "openai_base_url": settings.llm.openai_compat.base_url or "",
            "openai_model": settings.llm.openai_compat.model,
            "openai_auth_style": settings.llm.openai_compat.auth_style,
            "openai_api_key_env": settings.llm.openai_compat.api_key_env,
            "anthropic_model": settings.llm.anthropic.model,
            "anthropic_base_url": settings.llm.anthropic.base_url,
            "anthropic_api_key_env": settings.llm.anthropic.api_key_env,
            "whisper_model": settings.transcription.model,
            "keep_importance_threshold": settings.analysis.keep_importance_threshold,
            "notes_chapters_per_file": settings.notes.chapters_per_file,
            "render_crf": settings.render.crf,
            "render_preset": settings.render.preset,
            "ingest_browser_cookies": settings.ingest.browser_cookies,
            "presets": [preset.to_dict() for preset in PRESETS],
            "browsers": list(SUPPORTED_BROWSERS),
            # Which environment variables are actually set, so the UI can say
            # "key present" without ever revealing the value.
            "keys_present": {
                name: bool(os.environ.get(name, "").strip())
                for name in sorted(
                    {preset.api_key_env for preset in PRESETS}
                    | {
                        settings.llm.openai_compat.api_key_env,
                        settings.llm.anthropic.api_key_env,
                    }
                )
            },
        }

    @app.put("/api/settings")
    def put_settings(
        update: SettingsUpdate, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {}

        if app_state.hosted and any(
            value is not None
            for value in (
                update.llm_preset,
                update.openai_base_url,
                update.openai_auth_style,
                update.openai_api_key_env,
                update.anthropic_base_url,
                update.whisper_model,
            )
        ):
            raise HTTPException(
                status_code=403,
                detail="Provider endpoints, secret names and provisioning models are managed by the hosted server administrator.",
            )

        def put(section: str, *path_and_value: Any) -> None:
            *path, value = path_and_value
            cursor = overrides.setdefault(section, {})
            for key in path[:-1]:
                cursor = cursor.setdefault(key, {})
            cursor[path[-1]] = value

        # Choosing a preset sets everything that goes with it, so the user never
        # has to know that Azure sends the key in `api-key` and the rest do not.
        if update.llm_preset:
            preset = get_preset(update.llm_preset)
            if preset is None:
                raise HTTPException(
                    status_code=400, detail=f"Unknown preset '{update.llm_preset}'."
                )
            put("llm", "provider", preset.provider)
            if preset.provider == "anthropic":
                put("llm", "anthropic", "base_url", preset.base_url)
                put("llm", "anthropic", "api_key_env", preset.api_key_env)
                if preset.suggested_models:
                    put("llm", "anthropic", "model", preset.suggested_models[0])
            else:
                put("llm", "openai_compat", "preset", preset.key)
                put("llm", "openai_compat", "auth_style", preset.auth_style)
                put("llm", "openai_compat", "api_key_env", preset.api_key_env)
                if preset.base_url:
                    put("llm", "openai_compat", "base_url", preset.base_url)
                if preset.suggested_models:
                    put("llm", "openai_compat", "model", preset.suggested_models[0])

        if update.llm_provider:
            put("llm", "provider", update.llm_provider)
        if update.llm_auto is not None:
            put("llm", "auto", update.llm_auto)
        if update.llm_budget_level is not None:
            put("llm", "budget_level", update.llm_budget_level)
        if update.llm_tier_models is not None:
            # Settings files are deep-merged, so a key left out is a key kept.
            # All three are written every time, with "" meaning "choose for me",
            # otherwise a preference could never be undone.
            current = dict(app_state.settings.llm.tier_models)
            current.update(update.llm_tier_models)
            put(
                "llm",
                "tier_models",
                {tier: str(current.get(tier, "") or "") for tier in ("small", "balanced", "strong")},
            )
        if update.llm_model is not None:
            put("llm", "vscode", "model", update.llm_model or None)
        if update.vscode_reasoning_effort is not None:
            put("llm", "vscode", "reasoning_effort", update.vscode_reasoning_effort or None)
        if update.vscode_context_window_tokens is not None:
            put(
                "llm",
                "vscode",
                "context_window_tokens",
                update.vscode_context_window_tokens or None,
            )
        if update.copilot_cli_model is not None:
            # "auto" is the CLI's own word for "you pick", and so is an empty
            # value here: both mean "do not pass --model at all".
            choice = update.copilot_cli_model.strip()
            put("llm", "copilot_cli", "model", None if choice.lower() in ("", "auto") else choice)
        if update.copilot_cli_reasoning_effort is not None:
            put(
                "llm",
                "copilot_cli",
                "reasoning_effort",
                update.copilot_cli_reasoning_effort or None,
            )
        if update.copilot_cli_context_window is not None:
            put("llm", "copilot_cli", "context_window", update.copilot_cli_context_window)
        if update.openai_base_url is not None:
            put("llm", "openai_compat", "base_url", update.openai_base_url or None)
        if update.openai_model:
            put("llm", "openai_compat", "model", update.openai_model)
        if update.openai_auth_style:
            put("llm", "openai_compat", "auth_style", update.openai_auth_style)
        if update.openai_api_key_env:
            put("llm", "openai_compat", "api_key_env", update.openai_api_key_env)
        if update.anthropic_model:
            put("llm", "anthropic", "model", update.anthropic_model)
        if update.anthropic_base_url:
            put("llm", "anthropic", "base_url", update.anthropic_base_url)
        if update.whisper_model:
            put("transcription", "model", update.whisper_model)
        if update.keep_importance_threshold is not None:
            put("analysis", "keep_importance_threshold", update.keep_importance_threshold)
        if update.notes_chapters_per_file:
            put("notes", "chapters_per_file", update.notes_chapters_per_file)
        if update.render_crf is not None:
            put("render", "crf", update.render_crf)
        if update.render_preset:
            put("render", "preset", update.render_preset)
        if update.ingest_browser_cookies is not None:
            browser = update.ingest_browser_cookies.strip()
            if browser and browser not in SUPPORTED_BROWSERS:
                raise HTTPException(status_code=400, detail=f"Unsupported browser '{browser}'.")
            put("ingest", "browser_cookies", browser)

        if overrides:
            save_local_overrides(overrides, app_state.settings_path)
            app_state.reload_settings()
        return get_settings(app_state)

    @app.post("/api/shutdown")
    def shutdown(
        request: ShutdownRequest,
        background_tasks: BackgroundTasks,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        if not _is_local_only(app_state.settings):
            raise HTTPException(
                status_code=403,
                detail="Closing ClipDesk from the browser is only available on localhost.",
            )
        if app_state.shutdown_callback is None:
            raise HTTPException(
                status_code=503,
                detail="This ClipDesk server was not started by the application launcher.",
            )

        active = app_state.jobs.active_count()
        if active and not request.force:
            noun = "job is" if active == 1 else "jobs are"
            raise HTTPException(
                status_code=409,
                detail=f"{active} background {noun} still running.",
            )

        background_tasks.add_task(app_state.shutdown_callback)
        return {"shutting_down": True, "terminated_jobs": active}

    @app.post("/api/links/inspect")
    def inspect_link(
        request: InspectLinkRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """What ClipDesk makes of a link, before committing to a download."""
        _require_link_allowed(app_state, request.url)
        try:
            described = describe_link(request.url, app_state.settings.paths.vendor_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        effective_url = recalled_resolution(app_state.state_dir, request.url) or request.url
        jar = find_for_url(app_state.state_dir, effective_url)
        described["has_saved_session"] = jar is not None
        return described

    @app.post("/api/links/browse")
    def browse_link(
        request: InspectLinkRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """List the videos in a shared folder so the user can choose.

        A folder share has no single file to fetch, and guessing would be worse
        than asking.
        """
        _require_link_allowed(app_state, request.url)
        effective_url = recalled_resolution(app_state.state_dir, request.url) or request.url
        _require_link_allowed(app_state, effective_url)
        jar = find_for_url(app_state.state_dir, effective_url)
        cookie_header = cookie_header_from_jar(jar)
        # A share link is an opaque token until it has been followed.
        target = resolve_share(effective_url, cookie_header=cookie_header)
        _require_link_allowed(app_state, target)
        try:
            items = list_folder(target, cookie_header=cookie_header)
        except SharePointError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        media = [item for item in items if item.is_media]
        return {
            "folder": folder_path(target),
            "total": len(items),
            "items": [
                {
                    "name": item.name,
                    "path": item.path,
                    "size_bytes": item.size_bytes,
                    "modified": item.modified,
                    "url": file_url(target, item),
                }
                for item in media
            ],
            "skipped": len(items) - len(media),
            "has_saved_session": jar is not None,
        }

    # --- signed-in sessions ------------------------------------------------
    # Reading the browser's own cookie store no longer works on Edge or Chrome:
    # Chromium 127 wrapped the key so only the browser can unwrap it. These
    # endpoints take the session from the user instead.
    @app.get("/api/sessions")
    def list_sessions(app_state: UserState = Depends(current)) -> dict[str, Any]:
        return {"sessions": saved_hosts(app_state.state_dir)}

    @app.post("/api/sessions")
    def save_session(
        request: SessionSaveRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        try:
            bundle = parse_pasted(request.pasted, fallback_host=request.url)
        except CookieError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = save_for_host(app_state.state_dir, bundle)
        return {"host": bundle.host, "cookies": len(bundle.cookies), "path": str(path)}

    @app.delete("/api/sessions/{host}")
    def delete_session(
        host: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        return {"deleted": forget_cookies(app_state.state_dir, host)}

    @app.post("/api/sessions/sign-in", status_code=202)
    def start_sign_in(
        request: InspectLinkRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """Open a browser ClipDesk controls, and keep whatever session it ends up with.

        Reading Edge's own cookies is impossible now that Chromium encrypts them,
        but a browser ClipDesk launched will hand them back over the DevTools
        protocol. On a joined machine the sign-in is usually silent.
        """
        if app_state.hosted:
            raise HTTPException(
                status_code=403,
                detail="Interactive browser sign-in is only available when ClipDesk runs "
                "locally. Hosted users must use a delegated identity provider or paste "
                "a session into their isolated account.",
            )
        if find_browser() is None:
            raise HTTPException(
                status_code=409,
                detail="No Edge or Chrome installation was found, so ClipDesk cannot "
                "open a sign-in window.",
            )

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "sign-in"
            bus.stage_start(stage, "Signing in")
            try:
                result = browser_sign_in(
                    request.url,
                    app_state.state_dir,
                    lambda fraction, message: bus.progress(stage, fraction, message),
                )
            except SignInError as exc:
                raise RuntimeError(str(exc)) from exc

            # The tenant host the redirect lands on is usually not the host that
            # was clicked, and it is the one holding the session that matters.
            for host, cookies in result.by_host.items():
                save_for_host(app_state.state_dir, CookieBundle(host, cookies))
            remember_resolution(app_state.state_dir, request.url, result.final_url)

            hosts = ", ".join(sorted(result.by_host))
            bus.stage_end(stage, f"Session saved for {hosts}")
            return {
                "hosts": sorted(result.by_host),
                "cookies": sum(len(values) for values in result.by_host.values()),
                # The signed-in URL carries the real library path, which an
                # opaque share token does not.
                "resolved_url": result.final_url,
            }

        job = app_state.jobs.start(
            "sign-in", "-", work, label="Signing in to Microsoft", tab=""
        )
        return {"job_id": job.id, "kind": "sign-in"}

    @app.get("/api/sessions/capability")
    def sign_in_capability(
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        if app_state.hosted:
            return {"available": False, "browser": "", "hosted": True}
        browser = find_browser()
        return {
            "available": browser is not None,
            "browser": browser[0] if browser else "",
            "hosted": False,
        }

    # --- files this machine already has ------------------------------------
    # OneDrive is already signed in and already syncing, so a file it has is
    # reachable with no tokens, no cookies and no paste. A SharePoint library
    # added with "Add shortcut to OneDrive" shows up here as an ordinary folder.
    @app.get("/api/sources")
    def list_sources(app_state: UserState = Depends(current)) -> dict[str, Any]:
        if app_state.hosted:
            return {"roots": [], "available": False, "hosted": True}
        roots = cloud_roots()
        return {
            "roots": [root.to_dict() for root in roots],
            "available": any(root.path.is_dir() for root in roots),
        }

    @app.get("/api/sources/{root_id}/browse")
    def browse_source(
        root_id: str, path: str = "", app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        if app_state.hosted:
            raise HTTPException(
                status_code=403, detail="Server-local OneDrive is disabled in hosted mode."
            )
        try:
            root = find_root(cloud_roots(), root_id)
            entries = browse_folder(root, path)
        except BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "root": root.to_dict(),
            "path": path.strip("/"),
            "entries": [entry.to_dict() for entry in entries],
        }

    @app.get("/api/sources/{root_id}/search")
    def search_source(
        root_id: str, q: str = "", app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        if app_state.hosted:
            raise HTTPException(
                status_code=403, detail="Server-local OneDrive is disabled in hosted mode."
            )
        try:
            root = find_root(cloud_roots(), root_id)
            entries = search_folder(root, q)
        except BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "root": root.to_dict(),
            "query": q,
            "entries": [entry.to_dict() for entry in entries],
        }

    @app.post("/api/projects/from-local", status_code=202)
    def import_from_local(
        request: LocalImportRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        if app_state.hosted:
            raise HTTPException(
                status_code=403, detail="Server-local OneDrive is disabled in hosted mode."
            )
        try:
            root = find_root(cloud_roots(), request.root)
            source = resolve_within(root, request.path)
        except BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not source.is_file():
            raise HTTPException(status_code=404, detail=f"'{request.path}' is not a file.")
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(
                status_code=400, detail=f"'{source.name}' is not a media file ClipDesk can read."
            )

        filename = safe_filename(source.name, fallback="video.mp4")
        project = app_state.store.create(filename, title=request.title.strip() or source.stem)
        project.meta.status = "downloading"
        project.meta.source_url = str(source)
        project.save()

        cloud_only = is_cloud_only(source)

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "copy"
            bus.stage_start(
                stage,
                "Downloading from OneDrive" if cloud_only else "Copying from OneDrive",
            )
            if cloud_only:
                bus.progress(
                    stage,
                    None,
                    "This file is stored online only, so OneDrive has to fetch it first.",
                )
            try:
                written = copy_into(
                    source,
                    project.source_path,
                    lambda fraction, message: bus.progress(stage, fraction, message),
                )
            except BrowseError as exc:
                project.meta.status = "failed"
                project.meta.error = str(exc)
                project.save()
                raise RuntimeError(str(exc)) from exc

            project.meta.size_bytes = written
            project.meta.status = "new"
            project.save()
            bus.stage_end(stage, f"Copied {written / 1e6:.0f} MB")
            return {"project_id": project.id, "file": filename, "size_bytes": written}

        job = app_state.jobs.start(
            "download",
            project.id,
            work,
            label=f"Copying {source.name}",
            tab="overview",
            meta={"source": str(source), "cloud_only": cloud_only},
        )
        return {
            "job_id": job.id,
            "project_id": project.id,
            "kind": "download",
            "cloud_only": cloud_only,
        }

    @app.post("/api/projects/from-link", status_code=202)
    def import_from_link(
        request: ImportLinkRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """Create a project and download the recording into it.

        The project is created immediately so the UI has somewhere to navigate to
        and the download has somewhere to write; the fetch itself is a job,
        because a 2 GB recording is not an HTTP request you want to hold open.
        """
        _require_link_allowed(app_state, request.url)
        settings = app_state.settings
        vendor = settings.paths.vendor_dir
        effective_url = recalled_resolution(app_state.state_dir, request.url) or request.url
        _require_link_allowed(app_state, effective_url)
        try:
            link = describe_link(effective_url, vendor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        tools = find_tools(vendor)
        ffmpeg_dir = Path(tools.ffmpeg).parent if tools else None
        browser = (request.browser_cookies or settings.ingest.browser_cookies or "").strip()
        if browser and browser not in SUPPORTED_BROWSERS:
            raise HTTPException(status_code=400, detail=f"Unsupported browser '{browser}'.")
        cookie_file = find_for_url(app_state.state_dir, effective_url)

        title = request.title.strip()
        project = app_state.store.create(
            safe_filename(
                str(link.get("suggested_name") or "") or "video.mp4", fallback="video.mp4"
            ),
            title=title or "Downloading…",
        )
        project.meta.status = "downloading"
        project.meta.source_url = request.url
        project.save()

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "download"
            bus.stage_start(stage, f"Fetching from {link.get('kind', 'link')}")
            try:
                result = fetch_video(
                    effective_url,
                    project.source_dir,
                    lambda fraction, message: bus.progress(stage, fraction, message),
                    ffmpeg_dir=ffmpeg_dir,
                    vendor_dir=vendor,
                    browser_cookies=browser,
                    cookie_file=cookie_file,
                    max_mb=settings.ingest.max_download_mb,
                    youtube_clients=settings.ingest.youtube_player_clients,
                )
            except FetchError as exc:
                project.meta.status = "failed"
                project.meta.error = str(exc)
                project.save()
                raise RuntimeError(str(exc)) from exc

            project.meta.source_filename = result.path.name
            project.meta.size_bytes = result.bytes_written
            project.meta.status = "new"
            if not title:
                project.meta.title = result.path.stem.replace("_", " ")
            project.save()
            bus.stage_end(stage, f"Downloaded {result.path.name}")
            return {
                "project_id": project.id,
                "file": result.path.name,
                "method": result.method,
                "size_bytes": result.bytes_written,
            }

        job = app_state.jobs.start(
            "download",
            project.id,
            work,
            label=f"Downloading from {link.get('kind', 'link')}",
            tab="overview",
            meta={"url": request.url},
        )
        return {
            "job_id": job.id,
            "project_id": project.id,
            "kind": "download",
            "link": link,
        }

    # --- projects ----------------------------------------------------------
    @app.get("/api/projects")
    def list_projects(app_state: UserState = Depends(current)) -> list[dict[str, Any]]:
        return [with_credits(meta.to_dict()) for meta in app_state.store.list()]

    @app.post("/api/projects", status_code=201)
    async def create_project(
        video: UploadFile = File(...),
        transcript: UploadFile | None = File(default=None),
        title: str = Form(default=""),
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        filename = safe_filename(video.filename or "video.mp4", fallback="video.mp4")
        if Path(filename).suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"'{filename}' is not a media file this app can read. "
                f"Supported: {', '.join(sorted(VIDEO_SUFFIXES))}",
            )

        project = app_state.store.create(filename, title=title.strip())
        limit = app_state.settings.server.max_upload_mb * 1024 * 1024
        written = 0

        try:
            with project.source_path.open("wb") as handle:
                while chunk := await video.read(_UPLOAD_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"That file is larger than the "
                            f"{app_state.settings.server.max_upload_mb} MB limit. "
                            "Raise server.max_upload_mb in config/local.yaml.",
                        )
                    handle.write(chunk)

            if transcript is not None and transcript.filename:
                transcript_name = safe_filename(transcript.filename, fallback="transcript.srt")
                if Path(transcript_name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise HTTPException(
                        status_code=400,
                        detail="The transcript must be a .srt, .vtt or .json file.",
                    )
                destination = project.source_dir / transcript_name
                # A subtitle file is small by nature, so this is not a real
                # constraint on anyone, but without it the transcript field is an
                # unbounded write that sidesteps the limit applied to the video.
                transcript_written = 0
                with destination.open("wb") as handle:
                    while chunk := await transcript.read(_UPLOAD_CHUNK):
                        transcript_written += len(chunk)
                        if transcript_written > _MAX_TRANSCRIPT_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail="That transcript is larger than "
                                f"{_MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB, "
                                "which means it is not a subtitle file.",
                            )
                        handle.write(chunk)
                project.meta.transcript_filename = transcript_name
                project.meta.has_uploaded_transcript = True
        except Exception:
            app_state.store.delete(project.id)
            raise

        project.meta.size_bytes = written
        project.save()
        return project_payload(project)

    @app.get("/api/projects/{project_id}")
    def get_project(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        return project_payload(require_project(project_id, app_state))

    @app.delete("/api/projects/{project_id}")
    def delete_project(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        require_project(project_id, app_state)
        if app_state.jobs.active_count(project_id):
            raise HTTPException(
                status_code=409,
                detail="Wait for the project's active job to finish before deleting it.",
            )
        return {"deleted": app_state.store.delete(project_id)}

    @app.get("/api/projects/{project_id}/analysis")
    def get_analysis(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        return report.model_dump(mode="json")

    # --- actions -----------------------------------------------------------
    @app.post("/api/projects/{project_id}/analyze")
    def start_analysis(
        project_id: str, request: AnalyzeRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        app_state.ffmpeg()
        settings = app_state.settings
        llm = (
            None
            if request.skip_llm
            else app_state.llm(request.llm_provider, duration_s=project.meta.duration_s)
        )

        def work(bus: EventBus) -> dict[str, Any]:
            try:
                report = analyze_project(project, settings, bus, llm=llm)
            except Exception as exc:
                project.meta.status = "failed"
                project.meta.error = str(exc) or exc.__class__.__name__
                project.save()
                raise
            finally:
                track_tokens(project, llm)
            return {
                "project_id": project.id,
                "title": report.title,
                "chapters": len(report.chapters),
                "segments": len(report.transcript.segments),
                "warnings": report.warnings,
            }

        return start_job(
            app_state,
            "analyze",
            project,
            work,
            label="Transcribing and analysing" if llm else "Transcribing",
        )

    @app.post("/api/projects/{project_id}/notes")
    def start_notes(
        project_id: str, request: NotesRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        settings = app_state.settings.model_copy(deep=True)
        settings.notes.include_mermaid = request.include_mermaid
        settings.notes.include_timestamps = request.include_timestamps
        settings.notes.enrichment = request.enrichment
        if request.chapters_per_file:
            settings.notes.chapters_per_file = request.chapters_per_file
        llm = app_state.llm(duration_s=report.media.duration_s)

        def work(bus: EventBus) -> dict[str, Any]:
            llm.for_task("notes")
            try:
                paths = generate_notes(project, report, settings, llm, bus)
            finally:
                track_tokens(project, llm)
            return {"files": [path.name for path in paths]}

        return start_job(
            app_state,
            "notes",
            project,
            work,
            label=f"Writing notes ({len(report.chapters)} chapters)",
            queue=request.queue,
            produces="notes.md",
        )

    @app.get("/api/article/options")
    def article_options() -> dict[str, Any]:
        return {
            "formats": [
                {"id": "docx", "label": "Word (.docx)", "note": "Fills the supplied template."},
                {"id": "md", "label": "Markdown (.md)", "note": "Free-form, for a wiki or repo."},
            ],
            "shapes": [
                {"id": shape, "label": SHAPE_LABEL[shape]} for shape in ARTICLE_SHAPES
            ],
            "template_available": template_available(),
            "template_sections": ["Title", "Symptoms/Summary", "Cause", "Resolution/Workaround", "More Information", "Keyword(s)"],
        }

    @app.post("/api/projects/{project_id}/article")
    def start_article(
        project_id: str, request: ArticleRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        settings = app_state.settings
        if request.format == "docx" and not template_available():
            raise HTTPException(
                status_code=409,
                detail="The Word template is missing from tools/template/file.docx.",
            )
        llm = app_state.llm(duration_s=report.media.duration_s)
        stem = safe_filename(request.output_name, fallback="article") if request.output_name else ""
        output_name = f"{Path(stem).stem or 'article'}.{request.format}"

        def work(bus: EventBus) -> dict[str, Any]:
            try:
                path = generate_article(
                    project,
                    report,
                    settings,
                    llm,
                    bus,
                    article_format=request.format,
                    shape=request.shape,
                    title=request.title,
                    audience=request.audience,
                    extra_sections=list(request.extra_sections),
                    enrichment=request.enrichment,
                    include_diagram=request.include_diagram,
                    output_name=output_name,
                )
            finally:
                track_tokens(project, llm)
            return {"file": path.name}

        return start_job(
            app_state,
            "article",
            project,
            work,
            label=f"Writing {SHAPE_LABEL[request.shape].lower()} ({request.format})",
            queue=request.queue,
            produces=output_name,
        )

    @app.post("/api/projects/{project_id}/ask")
    def ask_question(
        project_id: str, request: AskRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """Answer one question inline. Short enough not to be worth a job."""
        project = require_project(project_id, app_state)
        report = require_report(project)
        llm = app_state.llm(duration_s=report.media.duration_s)
        try:
            answer = ask_report(report, request.question, llm, grounded=request.grounded)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            track_tokens(project, llm)

        usage = llm.meter.to_dict()
        return {
            "answer": answer.text,
            "went_beyond": answer.went_beyond,
            "grounded": answer.grounded,
            # Returned with the answer so the cost of asking is visible where it
            # was incurred, rather than only in the Library total.
            "usage": {**usage, **credits_for_tokens(usage)},
        }

    @app.post("/api/projects/{project_id}/cleanup/plan")
    def preview_cleanup(
        project_id: str, request: CleanupRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        options = CleanupOptions(**request.model_dump(exclude={"output_name", "queue"}))
        spans = plan_cleanup(report, app_state.settings, options)
        kept = sum(span.duration for span in spans)
        original = report.media.duration_s
        return {
            "spans": [span.model_dump(mode="json") for span in spans],
            "span_count": len(spans),
            "kept_seconds": round(kept, 2),
            "original_seconds": round(original, 2),
            "removed_seconds": round(max(0.0, original - kept), 2),
            "removed_percent": round((1 - kept / original) * 100, 1) if original else 0.0,
            "summary": f"{format_duration(kept)} kept of {format_duration(original)}",
            **explain_cleanup(report, app_state.settings, options),
        }

    @app.post("/api/projects/{project_id}/cleanup")
    def start_cleanup(
        project_id: str, request: CleanupRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        tools = app_state.ffmpeg()
        settings = app_state.settings
        options = CleanupOptions(**request.model_dump(exclude={"output_name", "queue"}))
        output_name = video_output_name(request.output_name, fallback="cleaned.mp4")

        def work(bus: EventBus) -> dict[str, Any]:
            path = render_cleanup(
                project, report, settings, options, tools.ffmpeg, bus, output_name
            )
            return {"file": path.name}

        return start_job(
            app_state,
            "cleanup",
            project,
            work,
            label=f"Clean cut → {output_name}",
            queue=request.queue,
            produces=output_name,
        )

    @app.post("/api/projects/{project_id}/clips/find")
    def find_clips(
        project_id: str, request: ClipFindRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """Search for clip options. Renders nothing, but can take a while.

        This runs as a job rather than a held-open request. It is one model call
        over a whole transcript, which is slow enough that a user will switch
        tabs, and the result is a set of options they still have to choose from
        — losing that to a navigation is the worst possible outcome.
        """
        project = require_project(project_id, app_state)
        report = require_report(project)
        llm = app_state.llm(duration_s=report.media.duration_s) if request.use_llm else None
        query = request.query.strip()
        highlights = request.mode == "highlight"

        def work(bus: EventBus) -> dict[str, Any]:
            bus.stage_start("find", "Reading the transcript")
            if llm is not None:
                llm.for_task("clips")
            try:
                result = find_candidates(
                    report,
                    app_state.settings,
                    mode=request.mode,
                    target_seconds=request.target_seconds,
                    query=query,
                    count=request.count,
                    llm=llm,
                    bus=bus,
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            finally:
                track_tokens(project, llm)
            payload = result.to_dict()
            bus.stage_end("find", f"{len(payload.get('candidates') or [])} option(s) found")
            # The query and mode ride along so the tab can rebuild its heading
            # from the job alone when the user comes back to it.
            return {**payload, "mode": request.mode, "query": query}

        return start_job(
            app_state,
            "highlights-find" if highlights else "clips-find",
            project,
            work,
            label="Finding highlights" if highlights else "Finding clip options",
            meta={"mode": request.mode, "query": query},
            queue=request.queue,
        )

    @app.post("/api/projects/{project_id}/clips/render")
    def render_clips_selection(
        project_id: str, request: ClipRenderRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        tools = app_state.ffmpeg()
        settings = app_state.settings

        limit = report.media.duration_s or 0.0
        clips: list[ClipCandidate] = []
        for selection in request.selections:
            end = min(selection.end, limit) if limit else selection.end
            if end <= selection.start:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{selection.title}' has an empty or reversed time range.",
                )
            clips.append(
                ClipCandidate(
                    title=selection.title,
                    start=selection.start,
                    end=end,
                    summary=selection.summary,
                )
            )

        kind = ArtifactKind.HIGHLIGHT if request.reframe else ArtifactKind.CLIP
        prefix = safe_filename(request.prefix or "clip", fallback="clip")
        output_name = (
            video_output_name(request.output_name, fallback="clip.mp4")
            if request.output_name
            else ""
        )

        def work(bus: EventBus) -> dict[str, Any]:
            paths = render_selection(
                project,
                report,
                clips,
                settings,
                tools.ffmpeg,
                bus,
                combine=request.combine,
                reframe=request.reframe,
                kind=kind,
                prefix=prefix,
                title=request.title,
                output_name=output_name,
            )
            return {"files": [path.name for path in paths]}

        return start_job(
            app_state,
            "clips",
            project,
            work,
            label=(
                f"Rendering {len(clips)} highlight(s)"
                if request.reframe
                else (
                    "Joining into one video"
                    if request.combine
                    else f"Rendering {len(clips)} clip(s)"
                )
            ),
            tab="highlights" if request.reframe else "clip",
            meta={"count": len(clips), "combine": request.combine},
            queue=request.queue,
            produces=output_name,
        )

    @app.post("/api/projects/{project_id}/bookend")
    def start_bookend(
        project_id: str, request: BookendRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        tools = app_state.ffmpeg()
        settings = app_state.settings

        def queued_attachment(name: str | None) -> Path | None:
            if not name:
                return None
            try:
                return resolve_attachment(project, name)
            except ValueError:
                if request.queue and name in pending_outputs(app_state, project):
                    return project.output_path(safe_filename(name))
                raise

        try:
            header = queued_attachment(request.header_asset)
            footer = queued_attachment(request.footer_asset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        trimming = request.trim_start > 0.0 or request.trim_end is not None
        if header is None and footer is None and not trimming:
            raise HTTPException(
                status_code=400, detail="Choose an intro, an outro, or a trim range."
            )
        if request.trim_end is not None and request.trim_end <= request.trim_start:
            raise HTTPException(status_code=400, detail="The trim must end after it starts.")

        body = request.body_filename or project.meta.source_filename
        output_name = video_output_name(request.output_name, fallback="final.mp4")

        def work(bus: EventBus) -> dict[str, Any]:
            path = apply_bookends(
                project,
                settings,
                tools.ffprobe,
                tools.ffmpeg,
                bus,
                body_filename=body,
                header=header,
                footer=footer,
                output_name=output_name,
                intro_transition=request.intro_transition,
                outro_transition=request.outro_transition,
                trim_start=request.trim_start,
                trim_end=request.trim_end,
            )
            return {"file": path.name}

        return start_job(
            app_state,
            "bookend",
            project,
            work,
            label=f"Assemble → {output_name}",
            queue=request.queue,
            produces=output_name,
        )

    @app.post("/api/projects/{project_id}/intro")
    @app.post("/api/projects/{project_id}/outro")
    def start_intro(
        project_id: str,
        request: IntroRequest,
        http_request: Request,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        tools = app_state.ffmpeg()
        media = probe(project.source_path, tools.ffprobe)
        report = project.load_analysis()
        try:
            style = resolve_style(app_state.state_dir, request.style_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        analysed_title = (report.title if report else project.meta.title) or project.source_path.stem
        analysed_summary = (
            (report.abstract or report.summary)
            if report
            else "A visual overview created from the source video."
        )
        is_outro = http_request.url.path.rstrip("/").endswith("/outro")
        title = request.title.strip() or (
            "Thank you for watching" if is_outro else analysed_title
        )
        subtitle = request.subtitle.strip() or (
            analysed_title if is_outro else _first_sentence(analysed_summary)
        )
        end_card_text = request.end_card_text.strip() or (
            "See you next time" if is_outro and request.include_final_message else ""
        )

        plan = (
            plan_outro(
                style,
                total_seconds=request.duration_seconds,
                source_duration=media.duration_s,
                include_final_message=request.include_final_message,
            )
            if is_outro
            else plan_intro(
                style,
                total_seconds=request.duration_seconds,
                shot_count=request.shot_count,
                source_duration=media.duration_s,
                report=report,
                subtitle=subtitle,
            )
        )
        if request.show_shot_tags and not is_outro:
            spans = [scene.span or (0.0, 0.0) for scene in plan.shots]
            tags = shot_labels(spans, report)
            tagged: list[IntroScene] = []
            shot_index = 0
            for scene in plan.scenes:
                if scene.kind == "shot":
                    tagged.append(
                        replace(scene, text=tags[shot_index] if shot_index < len(tags) else "")
                    )
                    shot_index += 1
                else:
                    tagged.append(scene)
            plan = IntroPlan(
                scenes=tuple(tagged),
                total_seconds=plan.total_seconds,
                transition_seconds=plan.transition_seconds,
            )

        sequence_kind = "outro" if is_outro else "intro"
        output_name = video_output_name(
            request.output_name, fallback=f"{sequence_kind}.mp4"
        )
        narration = fit_narration(
            request.voiceover_text.strip() or subtitle or analysed_summary,
            request.duration_seconds,
        )
        if request.voiceover and not local_voiceover_available():
            raise HTTPException(
                status_code=409,
                detail="Local voiceover is not available on this machine.",
            )

        def work(bus: EventBus) -> dict[str, Any]:
            stage = sequence_kind
            bus.stage_start(stage, f"Building the {style.name} {sequence_kind}")
            path = project.output_path(output_name)
            with tempfile.TemporaryDirectory(
                prefix=f".{sequence_kind}-", dir=project.output_dir
            ) as name:
                voiceover_path: Path | None = None
                soundtrack_source = (
                    None
                    if request.audio_id == "none"
                    else resolve_audio(app_state.state_dir, request.audio_id)
                )
                soundtrack_path = prepare_soundtrack(
                    request.audio_id,
                    plan.total_seconds,
                    Path(name) / "soundtrack.wav",
                    tools.ffmpeg,
                    imported=soundtrack_source,
                )
                if request.voiceover:
                    bus.progress(stage, None, "Generating local voiceover")
                    voiceover_path = synthesize_voiceover(
                        narration, Path(name) / "voiceover.wav", request.voice_id
                    )
                render_intro(
                    project.source_path,
                    path,
                    plan,
                    style=style,
                    render=app_state.settings.render,
                    ffmpeg_bin=tools.ffmpeg,
                    soundtrack_path=soundtrack_path,
                    title=title,
                    subtitle=subtitle,
                    end_card_text=end_card_text,
                    repeat_title=request.repeat_title,
                    voiceover_path=voiceover_path,
                    on_progress=lambda fraction: bus.progress(stage, fraction, "Rendering intro"),
                )
            project.add_artifact(
                ArtifactKind.OUTRO if is_outro else ArtifactKind.INTRO,
                f"{style.name} {sequence_kind} ({format_duration(plan.total_seconds)})",
                path,
                duration_s=plan.total_seconds,
                meta={
                    "style": style.id,
                    "style_name": style.name,
                    "sequence_kind": sequence_kind,
                    "scenes": str(len(plan.scenes)),
                    "shots": str(len(plan.shots)),
                    "title": title,
                    "subtitle": subtitle,
                    "audio": request.audio_id,
                    "voiceover": str(request.voiceover).lower(),
                    "voice": request.voice_id,
                    "narration": narration if request.voiceover else "",
                },
            )
            bus.stage_end(stage, f"{sequence_kind.capitalize()} ready")
            return {"file": path.name, "plan": plan.describe(), "style": style.public()}

        return start_job(
            app_state,
            sequence_kind,
            project,
            work,
            label=f"Building the {style.name} {sequence_kind}",
            queue=request.queue,
            produces=output_name,
        )

    @app.get("/api/intro/styles")
    def intro_styles(app_state: UserState = Depends(current)) -> dict[str, Any]:
        installed = available_styles(app_state.state_dir)
        installed_ids = {style.id for style in installed}
        return {
            "installed": [style.public() for style in installed],
            "catalog": [
                style.public() for style in CATALOG_STYLES if style.id not in installed_ids
            ],
            "voiceover_available": local_voiceover_available(),
            "voices": list(available_voices()),
        }

    @app.post("/api/intro/voices/refresh")
    def refresh_intro_voices(app_state: UserState = Depends(current)) -> dict[str, Any]:
        available_voices.cache_clear()
        return {"voices": list(available_voices())}

    @app.get("/api/intro/audio")
    def intro_audio_library(app_state: UserState = Depends(current)) -> dict[str, Any]:
        return list_audio(app_state.state_dir)

    @app.get("/api/intro/audio/preview")
    def preview_intro_audio(
        audio_id: str, app_state: UserState = Depends(current)
    ) -> FileResponse:
        tools = app_state.ffmpeg()
        try:
            path = preview_audio(app_state.state_dir, audio_id, tools.ffmpeg)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, media_type="audio/mpeg", filename=path.name)

    @app.post("/api/intro/audio", status_code=201)
    async def upload_intro_audio(
        file: UploadFile = File(...), app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        name = safe_filename(file.filename or "soundtrack.wav", fallback="soundtrack.wav")
        if Path(name).suffix.lower() not in AUDIO_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail="Intro audio must be MP3, WAV, M4A, AAC, FLAC or OGG.",
            )
        staging = audio_dir(app_state.state_dir) / ".uploads"
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(suffix=Path(name).suffix, dir=staging)
        os.close(descriptor)
        temporary = Path(temporary_name)
        limit_mb = min(app_state.settings.server.max_upload_mb, 100)
        limit = limit_mb * 1024 * 1024
        written = 0
        try:
            with temporary.open("wb") as handle:
                while chunk := await file.read(_UPLOAD_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"That audio file exceeds {limit_mb} MB.",
                        )
                    handle.write(chunk)
            tools = app_state.ffmpeg()
            info = probe(temporary, tools.ffprobe)
            if not info.has_audio or info.duration_s <= 0:
                raise HTTPException(status_code=400, detail="That file has no readable audio stream.")
            destination = store_audio(temporary, app_state.state_dir, name)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "id": f"imported:{destination.name}",
            "name": destination.name,
            "size_bytes": destination.stat().st_size,
        }

    @app.delete("/api/intro/audio/{name}")
    def delete_intro_audio(
        name: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        try:
            path = resolve_audio(app_state.state_dir, f"imported:{safe_filename(name)}")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path.unlink(missing_ok=True)
        return {"deleted": True}

    @app.post("/api/intro/styles/install", status_code=201)
    def install_intro_style(
        request: IntroStyleInstallRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, object]:
        try:
            return install_catalog_style(app_state.state_dir, request.style_id).public()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/intro/styles/import", status_code=201)
    def import_intro_style(
        request: IntroStyleImportRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, object]:
        try:
            return import_custom_style(
                app_state.state_dir, request.model_dump(mode="json")
            ).public()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/plan")
    def plan_prompt_request(
        project_id: str,
        request: PromptEditRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        """Work out which capability a written instruction is asking for.

        Planning only. Nothing is rendered until the user confirms and the client
        calls the typed endpoint for that capability, which validates again.
        """
        project = require_project(project_id, app_state)
        tools = app_state.ffmpeg()
        if not project.source_path.is_file():
            raise HTTPException(
                status_code=409,
                detail="The original recording has been deleted, so there is nothing to plan against.",
            )
        media = probe(project.source_path, tools.ffprobe)
        report = project.load_analysis()
        context = PromptContext(
            duration_s=media.duration_s,
            media_names=tuple(str(item["name"]) for item in list_media(project)),
            output_names=tuple(
                str(artifact.get("filename", ""))
                # Most recent first, so "export it smaller" means the latest render.
                for artifact in reversed(project.meta.artifacts)
                if str(artifact.get("filename", "")).endswith((".mp4", ".mov", ".mkv", ".webm"))
            ),
            style_ids=tuple(style.id for style in available_styles(app_state.state_dir)),
            title=(report.title if report else project.meta.title) or "",
            source_filename=project.meta.source_filename,
            has_analysis=report is not None,
        )
        try:
            plan = plan_prompt(request.prompt, context)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"plan": plan.to_dict()}

    def _attachment_request(prompt: str, media_names: tuple[str, ...]) -> tuple[str, str] | None:
        """Detect "add intro X" / "add outro Y", which joins files rather than edits."""
        lowered = prompt.lower()
        header = footer = ""
        for keyword, slot in (("intro", "header"), ("outro", "footer")):
            if not re.search(rf"\b(add|attach|append|prepend|join)\b.*\b{keyword}\b", lowered):
                continue
            tail = lowered.split(keyword, 1)[1]
            found = sorted(
                (tail.find(name.lower()), -len(name), name)
                for name in media_names
                if name.lower() in tail
            )
            if not found:
                continue
            if slot == "header":
                header = found[0][2]
            else:
                footer = found[0][2]
        return (header, footer) if header or footer else None

    @app.post("/api/projects/{project_id}/edit")
    def prompt_edit(
        project_id: str,
        request: PromptEditRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        tools = app_state.ffmpeg()
        media = probe(project.source_path, tools.ffprobe)
        asset_names = tuple(str(item["name"]) for item in list_media(project))
        attach = _attachment_request(request.prompt, asset_names)
        try:
            program = (
                None if attach else parse_edit_program(request.prompt, media.duration_s, asset_names)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.preview_only:
            if program is None:
                return {"plan": {"operations": [f"Add {attach[0] or attach[1]}"]}}
            return {
                "plan": {
                    "operations": program.describe(),
                    "start": program.trim.start if program.trim else 0.0,
                    "end": program.trim.end if program.trim else None,
                    "output_seconds": program.output_duration,
                }
            }

        output_name = video_output_name(request.output_name, fallback="edited.mp4")
        if program is not None:
            try:
                program = program.keeping(request.steps)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "prompt-edit"
            bus.stage_start(stage, "Applying edit plan")
            if program is None:
                header, footer = attach
                path = apply_bookends(
                    project,
                    app_state.settings,
                    tools.ffprobe,
                    tools.ffmpeg,
                    bus,
                    body_filename=project.meta.source_filename,
                    header=resolve_attachment(project, header) if header else None,
                    footer=resolve_attachment(project, footer) if footer else None,
                    output_name=output_name,
                )
                operations = [f"Add {header or footer}"]
            else:
                path = project.output_path(output_name)
                render_program(
                    project.source_path,
                    path,
                    program,
                    has_audio=media.has_audio,
                    render=app_state.settings.render,
                    ffmpeg_bin=tools.ffmpeg,
                    on_progress=lambda fraction: bus.progress(stage, fraction, "Rendering edit"),
                )
                operations = program.describe()
                project.add_artifact(
                    ArtifactKind.EDIT,
                    f"Prompt edit ({format_duration(program.output_duration)})",
                    path,
                    duration_s=program.output_duration,
                    meta={"operations": "; ".join(operations)},
                )
            bus.stage_end(stage, "Edited video ready")
            return {"file": path.name, "plan": {"operations": operations}}

        return start_job(
            app_state,
            "prompt-edit",
            project,
            work,
            label=f"Edit → {output_name}",
            queue=request.queue,
            produces=output_name,
        )

    @app.post("/api/projects/{project_id}/transcript")
    def export_transcript_file(
        project_id: str,
        request: TranscriptExportRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        if request.format not in TRANSCRIPT_FORMATS:
            raise HTTPException(
                status_code=400,
                detail=f"Format must be one of: {', '.join(TRANSCRIPT_FORMATS)}",
            )
        path = export_transcript(project, report, request.format)
        return {
            "file": path.name,
            "url": f"/api/projects/{project.id}/outputs/{path.name}",
        }

    @app.post("/api/projects/{project_id}/summary")
    def export_summary_file(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        path = export_summary(project, report)
        return {"file": path.name, "url": f"/api/projects/{project.id}/outputs/{path.name}"}

    # --- files -------------------------------------------------------------
    @app.get("/api/projects/{project_id}/outputs/{filename}")
    def download_output(
        project_id: str, filename: str, app_state: UserState = Depends(current)
    ):
        project = require_project(project_id, app_state)
        try:
            path = project.output_path(safe_filename(filename))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"No output named '{filename}'.")
        return FileResponse(path, filename=path.name)

    @app.get("/api/projects/{project_id}/outputs/{filename}/document")
    def read_output_as_text(
        project_id: str, filename: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """A Word output as Markdown, so it can be read without opening Word."""
        project = require_project(project_id, app_state)
        try:
            path = project.output_path(safe_filename(filename))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if path.suffix.lower() != ".docx":
            raise HTTPException(status_code=400, detail="Only Word documents can be read back.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"No output named '{filename}'.")
        try:
            return {"filename": path.name, "markdown": read_markdown(path)}
        except (TemplateError, KeyError, zipfile.BadZipFile) as exc:
            raise HTTPException(
                status_code=422, detail=f"That document could not be read: {exc}"
            ) from exc

    @app.delete("/api/projects/{project_id}/outputs/{filename}")
    def remove_output(
        project_id: str, filename: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        project = require_project(project_id, app_state)
        try:
            existed = delete_output(project, safe_filename(filename))
        except OutputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": existed}

    @app.post("/api/projects/{project_id}/outputs/{filename}/rename")
    def rename_project_output(
        project_id: str,
        filename: str,
        request: OutputRenameRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, str]:
        project = require_project(project_id, app_state)
        try:
            path = rename_output(
                project,
                safe_filename(filename),
                safe_filename(request.name, fallback="output"),
            )
        except OutputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "file": path.name,
            "url": f"/api/projects/{project.id}/outputs/{quote(path.name)}",
        }

    @app.post("/api/projects/{project_id}/outputs/bundle")
    def bundle(
        project_id: str,
        request: OutputSelection,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        try:
            path = bundle_outputs(
                project,
                [safe_filename(name) for name in request.filenames],
                safe_filename(request.archive_name) if request.archive_name else "",
            )
        except OutputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "file": path.name,
            "size_bytes": path.stat().st_size,
            "url": f"/api/projects/{project.id}/outputs/{path.name}",
        }

    @app.post("/api/projects/{project_id}/outputs/reveal")
    def reveal_outputs(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, str]:
        project = require_project(project_id, app_state)
        if app_state.hosted or not _is_local_only(app_state.settings):
            # Opening a file manager only makes sense when the browser and the
            # server are the same machine.
            raise HTTPException(
                status_code=409,
                detail="Opening the folder is only available when ClipDesk runs on this machine.",
            )
        project.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            reveal(project.output_dir)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not open the folder: {exc}"
            ) from exc
        return {"path": str(project.output_dir)}

    @app.get("/api/projects/{project_id}/outputs")
    def list_outputs(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        return {
            "artifacts": artifact_payload(project),
            "folder": str(project.output_dir),
            "can_reveal": not app_state.hosted and _is_local_only(app_state.settings),
            "source": source_info(project),
        }

    @app.delete("/api/projects/{project_id}/source")
    def remove_source(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        """Delete the original recording but keep everything derived from it.

        Importing from a link can leave several gigabytes behind that the user
        never chose to download. Reclaiming that is reasonable; the UI warns that
        nothing further can be cut afterwards.
        """
        project = require_project(project_id, app_state)
        return {"deleted": delete_source(project)}

    @app.get("/api/projects/{project_id}/preview")
    def preview_source(
        project_id: str, request: Request, app_state: UserState = Depends(current)
    ) -> Response:
        project = require_project(project_id, app_state)
        return _ranged_file(project.source_path, request)

    @app.get("/api/projects/{project_id}/preview/{filename}")
    def preview_output(
        project_id: str,
        filename: str,
        request: Request,
        app_state: UserState = Depends(current),
    ) -> Response:
        project = require_project(project_id, app_state)
        try:
            path = project.output_path(safe_filename(filename))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _ranged_file(path, request)

    # --- intro / outro library --------------------------------------------
    @app.get("/api/projects/{project_id}/media")
    def get_project_media(
        project_id: str, app_state: UserState = Depends(current)
    ) -> list[dict[str, Any]]:
        project = require_project(project_id, app_state)
        return [
            {
                **item,
                "url": f"/api/projects/{project.id}/media/{quote(str(item['name']), safe='')}",
            }
            for item in list_media(project)
        ]

    @app.get("/api/projects/{project_id}/media-library")
    def get_media_library(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        """Media held by other projects, offered only when explicitly asked for."""
        project = require_project(project_id, app_state)
        store = app_state.store
        sources: list[dict[str, Any]] = []
        for meta in store.list():
            if meta.id == project.id:
                continue
            other = store.get(meta.id)
            if other is None:
                continue
            items = list_media(other)
            if items:
                sources.append(
                    {"project_id": meta.id, "title": meta.title or meta.id, "items": items}
                )
        shared = list_assets(app_state.settings.paths.workspace_dir)
        if shared:
            sources.append({"project_id": "", "title": "Shared library", "items": shared})
        return {"sources": sources}

    @app.post("/api/projects/{project_id}/media/adopt", status_code=201)
    def adopt_project_media(
        project_id: str,
        request: MediaAdoptRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        name = safe_filename(request.name)
        try:
            if request.source_project_id:
                source_project = require_project(request.source_project_id, app_state)
                origin = resolve_media(source_project, name)
            else:
                origin = resolve_asset(app_state.settings.paths.workspace_dir, name)
            destination = copy_into_project(origin, project, name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"name": destination.name, "size_bytes": destination.stat().st_size}

    @app.post("/api/projects/{project_id}/media", status_code=201)
    async def upload_project_media(
        project_id: str,
        file: UploadFile = File(...),
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        name = safe_filename(file.filename or "media.mp4", fallback="media.mp4")
        if Path(name).suffix.lower() not in ASSET_SUFFIXES:
            raise HTTPException(
                status_code=400, detail="Imported media must be .mp4, .mov, .mkv or .webm."
            )
        staging = media_dir(project) / ".uploads"
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(suffix=Path(name).suffix, dir=staging)
        os.close(descriptor)
        temporary = Path(temporary_name)
        limit = app_state.settings.server.max_upload_mb * 1024 * 1024
        written = 0
        try:
            with temporary.open("wb") as handle:
                while chunk := await file.read(_UPLOAD_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "That file is larger than the "
                                f"{app_state.settings.server.max_upload_mb} MB limit."
                            ),
                        )
                    handle.write(chunk)
            destination = store_media(temporary, project, name)
        finally:
            temporary.unlink(missing_ok=True)
        return {"name": destination.name, "size_bytes": destination.stat().st_size}

    @app.post("/api/projects/{project_id}/media/from-link", status_code=202)
    def import_project_media_from_link(
        project_id: str,
        request: AssetLinksRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        settings = app_state.settings
        prepared: list[tuple[str, str, Path | None]] = []
        for item in request.items:
            _require_link_allowed(app_state, item.url)
            effective_url = recalled_resolution(app_state.state_dir, item.url) or item.url
            _require_link_allowed(app_state, effective_url)
            try:
                link = describe_link(effective_url, settings.paths.vendor_dir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if link.get("is_folder"):
                raise HTTPException(
                    status_code=409,
                    detail="Choose one or more videos from the folder before adding them.",
                )
            preferred_name = safe_filename(item.name) if item.name.strip() else ""
            prepared.append(
                (
                    effective_url,
                    preferred_name,
                    find_for_url(app_state.state_dir, effective_url),
                )
            )

        tools = find_tools(settings.paths.vendor_dir)
        ffmpeg_dir = Path(tools.ffmpeg).parent if tools else None

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "asset-download"
            total = len(prepared)
            bus.stage_start(stage, f"Adding {total} video(s) to this project")
            added: list[dict[str, Any]] = []
            staging_parent = media_dir(project)
            for index, (effective_url, preferred_name, cookie_file) in enumerate(prepared):
                try:
                    with tempfile.TemporaryDirectory(
                        prefix=".clipdesk-media-", dir=staging_parent
                    ) as temporary_dir:
                        result = fetch_video(
                            effective_url,
                            Path(temporary_dir),
                            lambda fraction, message, index=index: bus.progress(
                                stage,
                                (index + (fraction or 0.0)) / total
                                if fraction is not None
                                else None,
                                message,
                            ),
                            ffmpeg_dir=ffmpeg_dir,
                            vendor_dir=settings.paths.vendor_dir,
                            browser_cookies=settings.ingest.browser_cookies,
                            cookie_file=cookie_file,
                            max_mb=settings.ingest.max_download_mb,
                            youtube_clients=settings.ingest.youtube_player_clients,
                        )
                        destination = store_media(result.path, project, preferred_name)
                except (FetchError, ValueError) as exc:
                    raise RuntimeError(str(exc)) from exc
                added.append(
                    {"name": destination.name, "size_bytes": destination.stat().st_size}
                )
                bus.progress(stage, (index + 1) / total, f"Added {destination.name}")
            bus.stage_end(stage, f"Added {len(added)} video(s)")
            return {"assets": added}

        job = app_state.jobs.start(
            "asset-download",
            project.id,
            work,
            label="Adding media",
            tab="editor",
            meta={"count": len(prepared)},
        )
        return {"job_id": job.id, "project_id": project.id, "kind": "asset-download"}

    @app.get("/api/projects/{project_id}/media/{name}")
    def preview_project_media(
        project_id: str,
        name: str,
        request: Request,
        app_state: UserState = Depends(current),
    ) -> Response:
        project = require_project(project_id, app_state)
        try:
            path = resolve_media(project, safe_filename(name))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Ranged, so the dashboard player can seek instead of only playing from 0.
        return _ranged_file(path, request)

    @app.delete("/api/projects/{project_id}/media/{name}")
    def delete_project_media(
        project_id: str, name: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        project = require_project(project_id, app_state)
        try:
            path = resolve_media(project, safe_filename(name))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path.unlink(missing_ok=True)
        return {"deleted": True}

    @app.get("/api/export/options")
    def get_export_options(app_state: UserState = Depends(current)) -> dict[str, Any]:
        return export_options()

    @app.post("/api/projects/{project_id}/export")
    def start_export(
        project_id: str, request: ExportRequest, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        tools = app_state.ffmpeg()
        name = safe_filename(request.source)
        try:
            if request.source_kind == "media":
                source = resolve_media(project, name)
            elif request.source_kind == "source":
                source = project.source_path
            else:
                source = project.output_path(name)
            plan = plan_export(name, request.format, request.quality, request.output_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # A queued export may name a file an earlier queued step has not written
        # yet. Only insist the file is there when the export runs immediately.
        if not source.is_file() and name not in pending_outputs(app_state, project):
            raise HTTPException(status_code=404, detail=f"Could not find {name}.")

        def work(bus: EventBus) -> dict[str, Any]:
            stage = "export"
            bus.stage_start(stage, f"Exporting {plan.describe()}")
            path = render_export(
                project,
                plan,
                source=source,
                ffmpeg_bin=tools.ffmpeg,
                ffprobe_bin=tools.ffprobe,
                on_progress=lambda fraction: bus.progress(stage, fraction, "Encoding"),
            )
            bus.stage_end(stage, "Export ready")
            return {"file": path.name, "size_bytes": path.stat().st_size}

        return start_job(
            app_state,
            "export",
            project,
            work,
            label=f"Exporting {name}",
            tab="outputs",
            queue=request.queue,
            produces=plan.output_name,
        )

    # --- flows -------------------------------------------------------------
    @app.get("/api/flows")
    def get_flows(app_state: UserState = Depends(current)) -> dict[str, Any]:
        return {
            "flows": [flow.model_dump(mode="json") for flow in load_flows(app_state.state_dir)]
        }

    @app.put("/api/flows/{flow_id}")
    def put_flow(
        flow_id: str,
        flow: FlowDefinition,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        if flow.id != flow_id:
            raise HTTPException(status_code=400, detail="The Flow ID does not match its URL.")
        saved = save_flow(app_state.state_dir, flow)
        return saved.model_dump(mode="json")

    @app.delete("/api/flows/{flow_id}")
    def remove_flow(
        flow_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, bool]:
        if not delete_flow(app_state.state_dir, flow_id):
            raise HTTPException(status_code=404, detail="That Flow does not exist.")
        return {"deleted": True}

    @app.post("/api/projects/{project_id}/flows/{flow_id}/run")
    def run_flow(
        project_id: str,
        flow_id: str,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        report = require_report(project)
        flow = next((item for item in load_flows(app_state.state_dir) if item.id == flow_id), None)
        if flow is None:
            raise HTTPException(status_code=404, detail="That Flow does not exist.")
        if app_state.sequence.list(project.id):
            raise HTTPException(
                status_code=409,
                detail="Run or empty the existing queue before starting a Flow.",
            )
        if app_state.jobs.active_count(project.id):
            raise HTTPException(
                status_code=409,
                detail="Wait for this project's current job to finish before starting a Flow.",
            )

        seen_intro = False
        seen_outro = False
        available_sources = {"source"}
        output_names: set[str] = set()

        def step_output(step: Any) -> str:
            if isinstance(step, FlowCleanupStep):
                return video_output_name(step.output_name, fallback="cleaned.mp4")
            if isinstance(step, FlowClipStep):
                return video_output_name(step.output_name, fallback="clip.mp4")
            if isinstance(step, FlowHighlightStep):
                return video_output_name(step.output_name, fallback="highlight.mp4")
            if isinstance(step, FlowPromptStep):
                return video_output_name(step.output_name, fallback="prompt-edit.mp4")
            if isinstance(step, FlowBookendStep) and step.source == "generate":
                return video_output_name(step.output_name, fallback=f"{step.type}.mp4")
            if isinstance(step, FlowAssembleStep):
                return video_output_name(step.output_name, fallback="final.mp4")
            return ""

        for index, step in enumerate(flow.steps, start=1):
            if isinstance(step, FlowClipStep) and step.end <= step.start:
                raise HTTPException(
                    status_code=400,
                    detail=f"Flow step {index}: the clip must end after it starts.",
                )
            input_from = getattr(step, "input_from", "source")
            if isinstance(step, (FlowClipStep, FlowPromptStep, FlowAssembleStep)) or (
                isinstance(step, FlowBookendStep) and step.source == "generate"
            ):
                if input_from not in available_sources:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Flow step {index}: source '{input_from}' must be produced by an earlier step.",
                    )
            if isinstance(step, FlowHighlightStep) and step.mode == "topic" and not step.query.strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"Flow step {index}: enter a topic for the Topic highlight.",
                )
            if isinstance(step, FlowBookendStep):
                if step.source == "local":
                    if app_state.hosted:
                        raise HTTPException(
                            status_code=403,
                            detail="Hosted Flows cannot read server-local file paths.",
                        )
                    source = Path(step.local_path).expanduser()
                    if not source.is_file() or source.suffix.lower() not in ASSET_SUFFIXES:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Flow step {index}: local {step.type} file was not found or is not a supported video.",
                        )
                seen_intro = seen_intro or step.type == "intro"
                seen_outro = seen_outro or step.type == "outro"
            if isinstance(step, FlowAssembleStep) and not (seen_intro or seen_outro):
                raise HTTPException(
                    status_code=400,
                    detail=f"Flow step {index}: add an Intro or Outro before Assemble.",
                )

            output = step_output(step)
            if output:
                if output in output_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Flow step {index}: '{output}' is already produced by another step.",
                    )
                output_names.add(output)
                available_sources.add(output)

        final_output = ""
        intro_asset: str | None = None
        outro_asset: str | None = None
        queued: list[dict[str, Any]] = []

        def flow_source(name: str) -> Path:
            return project.source_path if name == "source" else project.output_path(safe_filename(name))

        def add_pending(
            kind: str,
            label: str,
            produces: str,
            work: Callable[[EventBus], dict[str, Any]],
            *,
            tab: str = "flows",
        ) -> str:
            pending = app_state.sequence.add(
                project.id,
                kind,
                work,
                label=label,
                produces=produces,
                tab=tab,
            )
            queued.append(pending.summary())
            return produces

        def remember(result: dict[str, Any]) -> str:
            queued.append(result["step"])
            return str(result["step"].get("produces") or "")

        def synthetic_request(kind: str) -> Request:
            path = f"/api/projects/{project.id}/{kind}"
            return Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "raw_path": path.encode("ascii"),
                    "query_string": b"",
                    "headers": [],
                    "scheme": "http",
                    "server": ("localhost", 80),
                    "client": ("127.0.0.1", 0),
                    "root_path": "",
                }
            )

        try:
            for step in flow.steps:
                if isinstance(step, FlowNotesStep):
                    remember(
                        start_notes(
                            project.id,
                            NotesRequest(
                                queue=True,
                                enrichment=step.enrichment,
                                include_mermaid=step.include_mermaid,
                                include_timestamps=step.include_timestamps,
                            ),
                            app_state,
                        )
                    )
                elif isinstance(step, FlowCleanupStep):
                    final_output = remember(
                        start_cleanup(
                            project.id,
                            CleanupRequest(queue=True, **step.model_dump(exclude={"type"})),
                            app_state,
                        )
                    )
                elif isinstance(step, FlowClipStep):
                    output = step_output(step)
                    source = flow_source(step.input_from)

                    def clip_work(bus: EventBus, item=step, path=source, name=output):
                        media = probe(path, app_state.ffmpeg().ffprobe)
                        end = min(item.end, media.duration_s)
                        if end <= item.start:
                            raise RuntimeError("The Flow clip starts after its selected source ends.")
                        clips = [
                            ClipCandidate(
                                title=item.title,
                                start=item.start,
                                end=end,
                                summary="Flow-selected range",
                            )
                        ]
                        paths = render_selection(
                            project,
                            report,
                            clips,
                            app_state.settings,
                            app_state.ffmpeg().ffmpeg,
                            bus,
                            combine=True,
                            reframe=item.reframe,
                            kind=ArtifactKind.HIGHLIGHT if item.reframe else ArtifactKind.CLIP,
                            title=item.title,
                            output_name=name,
                            source_path=path,
                            source_has_audio=media.has_audio,
                        )
                        return {"files": [written.name for written in paths]}

                    final_output = add_pending(
                        "clips", f"Clip {step.input_from} → {output}", output, clip_work
                    )
                elif isinstance(step, FlowHighlightStep):
                    output = step_output(step)
                    llm = app_state.llm(duration_s=report.media.duration_s)

                    def highlight_work(bus: EventBus, item=step, name=output, client=llm):
                        client.for_task("clips")
                        try:
                            result = find_candidates(
                                report,
                                app_state.settings,
                                mode="highlight",
                                target_seconds=item.target_seconds,
                                query=item.query.strip(),
                                count=1,
                                llm=client,
                                bus=bus,
                            )
                            if not result.candidates:
                                raise RuntimeError(result.note or "No suitable highlight was found.")
                            paths = render_selection(
                                project,
                                report,
                                [result.candidates[0]],
                                app_state.settings,
                                app_state.ffmpeg().ffmpeg,
                                bus,
                                combine=True,
                                reframe=item.reframe,
                                kind=ArtifactKind.HIGHLIGHT,
                                title=result.candidates[0].title,
                                output_name=name,
                            )
                            return {"files": [written.name for written in paths]}
                        finally:
                            track_tokens(project, client)

                    final_output = add_pending(
                        "highlights", f"Finding best highlight → {output}", output, highlight_work
                    )
                elif isinstance(step, FlowPromptStep):
                    output = step_output(step)
                    source = flow_source(step.input_from)

                    def prompt_work(bus: EventBus, item=step, path=source, name=output):
                        media = probe(path, app_state.ffmpeg().ffprobe)
                        try:
                            program = parse_edit_program(
                                item.prompt,
                                media.duration_s,
                                tuple(str(entry["name"]) for entry in list_media(project)),
                            )
                        except ValueError as exc:
                            raise RuntimeError(str(exc)) from exc
                        destination = project.output_path(name)
                        render_program(
                            path,
                            destination,
                            program,
                            has_audio=media.has_audio,
                            render=app_state.settings.render,
                            ffmpeg_bin=app_state.ffmpeg().ffmpeg,
                            on_progress=lambda fraction: bus.progress("prompt-edit", fraction, "Rendering edit"),
                        )
                        project.add_artifact(
                            ArtifactKind.EDIT,
                            f"Prompt edit ({format_duration(program.output_duration)})",
                            destination,
                            duration_s=program.output_duration,
                            meta={"operations": "; ".join(program.describe())},
                        )
                        return {"file": destination.name, "plan": {"operations": program.describe()}}

                    final_output = add_pending(
                        "prompt-edit", f"Prompt edit {step.input_from} → {output}", output, prompt_work
                    )
                elif isinstance(step, FlowBookendStep):
                    if step.source == "local":
                        stored = copy_into_project(
                            Path(step.local_path).expanduser(), project
                        )
                        produced = stored.name
                    elif step.input_from != "source":
                        output = step_output(step)
                        source = flow_source(step.input_from)
                        style = resolve_style(app_state.state_dir, step.style_id)

                        def derived_bookend_work(
                            bus: EventBus,
                            item=step,
                            path=source,
                            name=output,
                            chosen_style=style,
                        ):
                            tools = app_state.ffmpeg()
                            media = probe(path, tools.ffprobe)
                            analysed_title = report.title or project.meta.title or path.stem
                            analysed_summary = report.abstract or report.summary or analysed_title
                            title = item.title.strip() or (
                                "Thank you for watching" if item.type == "outro" else analysed_title
                            )
                            subtitle = item.subtitle.strip() or (
                                analysed_title if item.type == "outro" else _first_sentence(analysed_summary)
                            )
                            end_card = item.final_message.strip() if item.include_final_message else ""
                            plan = (
                                plan_outro(
                                    chosen_style,
                                    total_seconds=item.duration_seconds,
                                    source_duration=media.duration_s,
                                    include_final_message=item.include_final_message,
                                )
                                if item.type == "outro"
                                else plan_intro(
                                    chosen_style,
                                    total_seconds=item.duration_seconds,
                                    shot_count=item.shot_count,
                                    source_duration=media.duration_s,
                                    report=None,
                                    subtitle=subtitle,
                                )
                            )
                            destination = project.output_path(name)
                            stage = item.type
                            bus.stage_start(stage, f"Building {chosen_style.name} {item.type}")
                            with tempfile.TemporaryDirectory(
                                prefix=f".flow-{item.type}-", dir=project.output_dir
                            ) as temporary:
                                soundtrack_source = (
                                    None
                                    if item.audio_id == "none"
                                    else resolve_audio(app_state.state_dir, item.audio_id)
                                )
                                soundtrack = prepare_soundtrack(
                                    item.audio_id,
                                    plan.total_seconds,
                                    Path(temporary) / "soundtrack.wav",
                                    tools.ffmpeg,
                                    imported=soundtrack_source,
                                )
                                render_intro(
                                    path,
                                    destination,
                                    plan,
                                    style=chosen_style,
                                    render=app_state.settings.render,
                                    ffmpeg_bin=tools.ffmpeg,
                                    soundtrack_path=soundtrack,
                                    title=title,
                                    subtitle=subtitle,
                                    end_card_text=end_card,
                                    on_progress=lambda fraction: bus.progress(stage, fraction, "Rendering"),
                                )
                            project.add_artifact(
                                ArtifactKind.OUTRO if item.type == "outro" else ArtifactKind.INTRO,
                                f"{chosen_style.name} {item.type} ({format_duration(plan.total_seconds)})",
                                destination,
                                duration_s=plan.total_seconds,
                                meta={
                                    "style": chosen_style.id,
                                    "style_name": chosen_style.name,
                                    "sequence_kind": item.type,
                                    "source": item.input_from,
                                    "audio": item.audio_id,
                                },
                            )
                            bus.stage_end(stage, f"{item.type.capitalize()} ready")
                            return {"file": destination.name, "plan": plan.describe()}

                        produced = add_pending(
                            step.type,
                            f"{step.type.capitalize()} {step.input_from} → {output}",
                            output,
                            derived_bookend_work,
                        )
                    else:
                        produced = remember(
                            start_intro(
                                project.id,
                                IntroRequest(
                                    queue=True,
                                    duration_seconds=step.duration_seconds,
                                    shot_count=step.shot_count,
                                    style_id=step.style_id,
                                    title=step.title,
                                    subtitle=step.subtitle,
                                    audio_id=step.audio_id,
                                    include_final_message=step.include_final_message,
                                    end_card_text=step.final_message,
                                    output_name=step.output_name,
                                ),
                                synthetic_request(step.type),
                                app_state,
                            )
                        )
                    if step.type == "intro":
                        intro_asset = produced
                    else:
                        outro_asset = produced
                elif isinstance(step, FlowAssembleStep):
                    final_output = remember(
                        start_bookend(
                            project.id,
                            BookendRequest(
                                queue=True,
                                body_filename="" if step.input_from == "source" else step.input_from,
                                header_asset=intro_asset,
                                footer_asset=outro_asset,
                                intro_transition=step.intro_transition,
                                outro_transition=step.outro_transition,
                                output_name=step.output_name,
                            ),
                            app_state,
                        )
                    )
            if not queued:
                raise HTTPException(
                    status_code=400,
                    detail="This Flow only copies files; add an action or Assemble step.",
                )
            started = flush_sequence(app_state, project)
        except Exception:
            app_state.sequence.clear(project.id)
            raise

        return {
            "flow": flow.model_dump(mode="json"),
            "queued_steps": queued,
            "job_ids": started,
            "final_output": final_output,
        }

    # --- queue -------------------------------------------------------------
    def queue_payload(app_state: UserState, project: Project) -> dict[str, Any]:
        steps = app_state.sequence.list(project.id)
        return {
            "steps": [step.summary() for step in steps],
            "count": len(steps),
            "produces": [step.produces for step in steps if step.produces],
        }

    @app.get("/api/projects/{project_id}/queue")
    def get_queue(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        return queue_payload(app_state, project)

    @app.delete("/api/projects/{project_id}/queue")
    def clear_queue(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        removed = app_state.sequence.clear(project.id)
        return {"removed": removed, **queue_payload(app_state, project)}

    @app.delete("/api/projects/{project_id}/queue/{step_id}")
    def remove_queue_step(
        project_id: str, step_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        if not app_state.sequence.remove(project.id, step_id):
            raise HTTPException(status_code=404, detail="That step is no longer queued.")
        return queue_payload(app_state, project)

    @app.post("/api/projects/{project_id}/queue/{step_id}/move")
    def move_queue_step(
        project_id: str,
        step_id: str,
        request: QueueMoveRequest,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        if not app_state.sequence.move(project.id, step_id, request.offset):
            raise HTTPException(status_code=409, detail="That step cannot move any further.")
        return queue_payload(app_state, project)

    @app.post("/api/projects/{project_id}/queue/run")
    def run_queue(
        project_id: str, app_state: UserState = Depends(current)
    ) -> dict[str, Any]:
        project = require_project(project_id, app_state)
        if not app_state.sequence.list(project.id):
            raise HTTPException(status_code=409, detail="Nothing is queued.")
        started = flush_sequence(app_state, project)
        return {
            "job_ids": started,
            "jobs": [
                job for job in app_state.jobs.list(project.id) if job["id"] in set(started)
            ],
        }

    # --- jobs --------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(
        project_id: str = "",
        active: bool = False,
        app_state: UserState = Depends(current),
    ) -> dict[str, Any]:
        return {
            "jobs": app_state.jobs.list(project_id, active_only=active),
            "active": app_state.jobs.active_count(),
            "active_here": app_state.jobs.active_count(project_id) if project_id else 0,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, app_state: UserState = Depends(current)) -> dict[str, Any]:
        job = app_state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
        return job.snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, app_state: UserState = Depends(current)) -> dict[str, bool]:
        job = app_state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
        if not app_state.jobs.cancel(job_id):
            raise HTTPException(
                status_code=409,
                detail="That job has already started. Stopping it midway would leave a "
                "half-written file behind, so it has to finish.",
            )
        return {"cancelled": True}

    @app.websocket("/ws/jobs/{job_id}")
    async def job_stream(websocket: WebSocket, job_id: str) -> None:
        # WebSocket handshakes are not subject to CORS, so the origin has to be
        # checked here or any page the user happens to visit could open this
        # socket and read job progress. A browser always sends Origin; a local
        # script sends none, and those are the clients this is meant to serve.
        origin = websocket.headers.get("origin")
        if origin and origin not in origins:
            await websocket.close(code=1008)
            return

        try:
            app_state = state.authenticate(websocket.headers)
        except HTTPException:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        job = app_state.jobs.get(job_id)
        if job is None:
            await websocket.send_json({"type": "error", "message": f"No job '{job_id}'."})
            await websocket.close()
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # The pipeline publishes from a worker thread, so hop back onto the loop.
        remove = job.add_listener(
            lambda payload: loop.call_soon_threadsafe(queue.put_nowait, payload)
        )
        try:
            while True:
                if job.finished.is_set() and queue.empty():
                    await websocket.send_json(
                        {"type": "closed", "status": job.status, "result": job.result,
                         "error": job.error}
                    )
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            pass
        finally:
            remove()
            try:
                await websocket.close()
            except RuntimeError:
                pass

    # --- static UI ---------------------------------------------------------
    if WEB_DIR.is_dir():
        app.mount("/", RevalidatingStatics(directory=str(WEB_DIR), html=True), name="web")

    return app


# --- byte-range streaming ----------------------------------------------------
def _ranged_file(path: Path, request: Request) -> Response:
    """Serve a file with Range support so the browser can scrub a large video."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path.name}")

    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"accept-ranges": "bytes", "content-length": str(size)},
        )

    match = _RANGE_RE.match(range_header)
    if match is None:
        raise HTTPException(status_code=416, detail="Malformed Range header")
    if size == 0:
        # A failed download can leave a zero-byte file. There is no satisfiable
        # range in it, and pretending otherwise sends a content-length that does
        # not match the body.
        raise HTTPException(status_code=416, detail="That file is empty.")
    start_raw, end_raw = match.groups()
    start = int(start_raw) if start_raw else 0
    end = int(end_raw) if end_raw else size - 1
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def iterator():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(),
        status_code=206,
        media_type=media_type,
        headers={
            "content-range": f"bytes {start}-{end}/{size}",
            "accept-ranges": "bytes",
            "content-length": str(length),
        },
    )
