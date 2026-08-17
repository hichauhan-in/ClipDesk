"""The workspace: one folder per video, holding everything derived from it.

    workspace/<slug>-<hash>/
      project.json      what was uploaded and what state it is in
      analysis.json     the analysis artifact — the contract for every action
      source/           the uploaded video (and transcript, if one was supplied)
      audio/            extracted mono audio, deleted after transcription
      output/           everything the user can download

Keeping the layout flat and predictable means a project can be zipped, copied or
inspected without the app.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipdesk.models import AnalysisReport, Artifact, ArtifactKind, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,80}$")

PROJECT_FILE = "project.json"
ANALYSIS_FILE = "analysis.json"


def slugify(name: str, *, fallback: str = "video", max_len: int = 40) -> str:
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")[:max_len].strip("-")
    return slug or fallback


@dataclass(slots=True)
class ProjectMeta:
    id: str
    title: str
    source_filename: str
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    status: str = "new"  # new | downloading | analyzing | ready | failed
    duration_s: float = 0.0
    size_bytes: int = 0
    has_uploaded_transcript: bool = False
    transcript_filename: str = ""
    #: Where it came from, when it was imported from a link rather than uploaded.
    source_url: str = ""
    error: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source_filename": self.source_filename,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "duration_s": self.duration_s,
            "size_bytes": self.size_bytes,
            "has_uploaded_transcript": self.has_uploaded_transcript,
            "transcript_filename": self.transcript_filename,
            "source_url": self.source_url,
            "error": self.error,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectMeta:
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            source_filename=str(data.get("source_filename", "")),
            created_at=str(data.get("created_at", utcnow())),
            updated_at=str(data.get("updated_at", utcnow())),
            status=str(data.get("status", "new")),
            duration_s=float(data.get("duration_s", 0.0) or 0.0),
            size_bytes=int(data.get("size_bytes", 0) or 0),
            has_uploaded_transcript=bool(data.get("has_uploaded_transcript", False)),
            transcript_filename=str(data.get("transcript_filename", "")),
            source_url=str(data.get("source_url", "")),
            error=str(data.get("error", "")),
            artifacts=list(data.get("artifacts") or []),
        )


class Project:
    def __init__(self, root: Path, meta: ProjectMeta) -> None:
        self.root = root
        self.meta = meta

    # --- layout ------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def source_path(self) -> Path:
        return self.source_dir / self.meta.source_filename

    @property
    def transcript_upload_path(self) -> Path | None:
        if not self.meta.transcript_filename:
            return None
        return self.source_dir / self.meta.transcript_filename

    @property
    def analysis_path(self) -> Path:
        return self.root / ANALYSIS_FILE

    def output_path(self, filename: str) -> Path:
        """Resolve a name inside ``output/``, refusing anything that escapes it.

        The parent must be ``output/`` exactly. Checking only that ``output/`` is
        somewhere among the parents would also admit ``sub/dir/file``, which is
        not an escape but is not a name this application ever means to produce.
        """
        candidate = (self.output_dir / filename).resolve()
        if candidate.parent != self.output_dir.resolve():
            raise ValueError("Output path escapes the project directory")
        return candidate

    # --- persistence -------------------------------------------------------
    def save(self) -> None:
        self.meta.updated_at = utcnow()
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / PROJECT_FILE
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".project-", suffix=".json.tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(self.meta.to_dict(), handle, indent=2)
                handle.flush()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def save_analysis(self, report: AnalysisReport) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".analysis-", suffix=".json.tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(report.model_dump_json(indent=2, exclude_none=False))
                handle.flush()
            temporary.replace(self.analysis_path)
        finally:
            temporary.unlink(missing_ok=True)
        self.meta.status = "ready"
        self.meta.title = report.title or self.meta.title
        self.meta.duration_s = report.media.duration_s
        self.save()
        return self.analysis_path

    def load_analysis(self) -> AnalysisReport | None:
        if not self.analysis_path.is_file():
            return None
        return AnalysisReport.model_validate_json(
            self.analysis_path.read_text(encoding="utf-8")
        )

    # --- artifacts ---------------------------------------------------------
    def add_artifact(
        self,
        kind: ArtifactKind,
        label: str,
        path: Path,
        *,
        duration_s: float | None = None,
        meta: dict[str, str] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=f"{kind.value}-{path.stem}",
            kind=kind,
            label=label,
            filename=path.name,
            size_bytes=path.stat().st_size if path.is_file() else 0,
            duration_s=duration_s,
            meta=meta or {},
        )
        # Re-running an action replaces its previous output rather than piling up.
        self.meta.artifacts = [
            existing for existing in self.meta.artifacts if existing.get("id") != artifact.id
        ]
        self.meta.artifacts.append(artifact.model_dump(mode="json"))
        self.save()
        return artifact

    def remove_artifact(self, artifact_id: str) -> bool:
        remaining = []
        removed = False
        for entry in self.meta.artifacts:
            if entry.get("id") == artifact_id:
                removed = True
                filename = entry.get("filename")
                if filename:
                    (self.output_dir / str(filename)).unlink(missing_ok=True)
                continue
            remaining.append(entry)
        self.meta.artifacts = remaining
        if removed:
            self.save()
        return removed

    def cleanup_scratch(self) -> None:
        """Drop the extracted audio; it is large and trivially regenerated."""
        shutil.rmtree(self.audio_dir, ignore_errors=True)


class ProjectStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _project_root(self, project_id: str) -> Path:
        if not _SAFE_ID_RE.match(project_id):
            raise ValueError(f"Invalid project id: {project_id!r}")
        return self.workspace / project_id

    def create(self, source_filename: str, title: str = "") -> Project:
        stem = Path(source_filename).stem
        digest = hashlib.sha256(
            f"{source_filename}{utcnow()}".encode()
        ).hexdigest()[:8]
        project_id = f"{slugify(stem)}-{digest}"
        root = self._project_root(project_id)
        for directory in (root, root / "source", root / "output"):
            directory.mkdir(parents=True, exist_ok=True)

        project = Project(
            root,
            ProjectMeta(
                id=project_id,
                title=title or stem,
                source_filename=source_filename,
            ),
        )
        project.save()
        return project

    def get(self, project_id: str) -> Project | None:
        # Ids arrive straight from the URL, so a malformed one is a normal event
        # and should read as "no such project", not as a server error.
        try:
            root = self._project_root(project_id)
        except ValueError:
            return None
        meta_path = root / PROJECT_FILE
        if not meta_path.is_file():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Project(root, ProjectMeta.from_dict(data))

    def require(self, project_id: str) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(f"No project with id {project_id!r}")
        return project

    def list(self) -> list[ProjectMeta]:
        projects: list[ProjectMeta] = []
        for entry in self.workspace.iterdir():
            if not entry.is_dir():
                continue
            project = self.get(entry.name)
            if project is not None:
                projects.append(project.meta)
        projects.sort(key=lambda meta: meta.created_at, reverse=True)
        return projects

    def delete(self, project_id: str) -> bool:
        root = self._project_root(project_id)
        if not root.is_dir():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return True
