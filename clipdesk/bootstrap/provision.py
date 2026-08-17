"""Fetch, verify and unpack everything ClipDesk needs into ``vendor/``.

Design goals, in order:

1. The user installs nothing by hand. First run downloads ffmpeg and the
   speech-to-text weights, with progress, and never asks again.
2. It works on a locked-down machine. If the network is blocked, dropping the
   archive into ``vendor/downloads/`` is enough — the same code path unpacks it.
3. It is safe. Archive members are path-checked before extraction (no zip-slip),
   downloads are written to a temp file and only moved into place once complete,
   and a declared sha256 is enforced.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipdesk.bootstrap.manifest import (
    DOWNLOAD_CACHE_DIR,
    Component,
    all_components,
    whisper_component,
)
from clipdesk.bootstrap.manifest import FFMPEG as FFMPEG_COMPONENT
from clipdesk.bootstrap.manifest import YTDLP as YTDLP_COMPONENT

ProgressFn = Callable[[float | None, str], None]

_CHUNK = 1024 * 256


@dataclass(slots=True)
class ComponentStatus:
    key: str
    label: str
    description: str
    installed: bool
    approx_mb: int
    location: str
    detail: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "installed": self.installed,
            "approx_mb": self.approx_mb,
            "location": self.location,
            "detail": self.detail,
            "notes": self.notes,
        }


class ProvisionError(RuntimeError):
    pass


# --- filesystem helpers ------------------------------------------------------
def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _safe_join(root: Path, member: str) -> Path:
    """Resolve ``member`` inside ``root``, refusing to escape it (zip-slip)."""
    candidate = (root / member).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ProvisionError(f"Archive member escapes the target directory: {member}")
    return candidate


def _strip(member: str, count: int) -> str:
    if count <= 0:
        return member
    parts = Path(member).parts
    return str(Path(*parts[count:])) if len(parts) > count else ""


def _wanted(member: str, patterns: Iterable[str]) -> bool:
    patterns = tuple(patterns)
    if not patterns:
        return True
    normalised = "/" + member.replace("\\", "/").lstrip("/")
    return any(pattern in normalised for pattern in patterns)


# --- download ----------------------------------------------------------------
def _download(url: str, dest: Path, expected_sha256: str | None, on_progress: ProgressFn) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    tmp_path = Path(tmp_name)
    os.close(tmp_fd)

    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            written = 0
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if total:
                        on_progress(
                            written / total,
                            f"Downloading… {written / 1e6:.0f} / {total / 1e6:.0f} MB",
                        )
                    else:
                        on_progress(None, f"Downloading… {written / 1e6:.0f} MB")

        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise ProvisionError(f"Checksum mismatch for {url}")

        tmp_path.replace(dest)
        return dest
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# --- extraction --------------------------------------------------------------
def _extract(archive: Path, target: Path, component: Component, on_progress: ProgressFn) -> int:
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    name = archive.name.lower()

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            for index, member in enumerate(members):
                if not _wanted(member.filename, component.extract_only):
                    continue
                relative = _strip(member.filename, component.strip_components)
                if not relative:
                    continue
                out = _safe_join(target, relative)
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
                _make_executable(out)
                extracted += 1
                on_progress((index + 1) / max(1, len(members)), f"Unpacking {out.name}")
        return extracted

    if ".tar" in name or name.endswith((".tgz", ".txz")):
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            for index, member in enumerate(members):
                if not _wanted(member.name, component.extract_only):
                    continue
                relative = _strip(member.name, component.strip_components)
                if not relative:
                    continue
                out = _safe_join(target, relative)
                out.parent.mkdir(parents=True, exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    continue
                with source, out.open("wb") as dst:
                    shutil.copyfileobj(source, dst, _CHUNK)
                _make_executable(out)
                extracted += 1
                on_progress((index + 1) / max(1, len(members)), f"Unpacking {out.name}")
        return extracted

    raise ProvisionError(f"Unsupported archive format: {archive.name}")


def _normalise_layout(target: Path, component: Component) -> None:
    """Move loose executables into ``bin/`` when the archive had no bin folder."""
    if not component.required_files:
        return
    for required in component.required_files:
        destination = target / required
        if destination.exists():
            continue
        loose = target / Path(required).name
        if loose.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            loose.replace(destination)
            _make_executable(destination)


# --- ffmpeg ------------------------------------------------------------------
def ffmpeg_dir(vendor: Path) -> Path:
    return vendor / FFMPEG_COMPONENT.target_dir


def ffmpeg_installed(vendor: Path) -> bool:
    base = ffmpeg_dir(vendor)
    return all((base / required).is_file() for required in FFMPEG_COMPONENT.required_files)


def provision_ffmpeg(vendor: Path, on_progress: ProgressFn) -> Path:
    component = FFMPEG_COMPONENT
    target = ffmpeg_dir(vendor)
    if ffmpeg_installed(vendor):
        on_progress(1.0, "ffmpeg is already installed")
        return target

    cache = vendor / DOWNLOAD_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    offline = cache / component.offline_filename

    archives: list[Path] = []
    if offline.is_file():
        on_progress(None, f"Using {offline.name} from vendor/{DOWNLOAD_CACHE_DIR}")
        archives.append(offline)
    else:
        # macOS publishes ffmpeg and ffprobe as separate downloads, so every
        # source is needed. Elsewhere the sources are mirrors of one another and
        # the first that succeeds wins.
        need_all = component.mirrors_are_parts
        errors: list[str] = []
        for index, source in enumerate(component.sources):
            filename = source.url.rstrip("/").split("/")[-1] or component.offline_filename
            if "." not in filename:
                filename = f"{component.key}-{index}.zip"
            try:
                archives.append(
                    _download(source.url, cache / filename, source.sha256, on_progress)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source.url}: {exc}")
                continue
            if not need_all:
                break
        if not archives or (need_all and len(archives) < len(component.sources)):
            raise ProvisionError(
                "Could not download ffmpeg. Either allow access to one of the URLs "
                "below, or download it yourself and save it as "
                f"vendor/{DOWNLOAD_CACHE_DIR}/{component.offline_filename}.\n"
                + "\n".join(errors)
            )

    for archive in archives:
        _extract(archive, target, component, on_progress)
    _normalise_layout(target, component)

    if not ffmpeg_installed(vendor):
        raise ProvisionError(
            f"ffmpeg unpacked but {component.required_files} were not found under {target}."
        )

    # Reclaim the archive once it has served its purpose — it is ~160 MB and can
    # always be fetched again. An archive the user placed here themselves is left
    # alone, because on a locked-down machine it is the only way to reinstall.
    for archive in archives:
        if archive.name != component.offline_filename:
            archive.unlink(missing_ok=True)

    on_progress(1.0, "ffmpeg ready")
    return target


# --- single-binary components ------------------------------------------------
def binary_dir(vendor: Path, component: Component) -> Path:
    return vendor / component.target_dir


def binary_installed(vendor: Path, component: Component) -> bool:
    base = binary_dir(vendor, component)
    return all((base / required).is_file() for required in component.required_files)


def provision_binary(vendor: Path, component: Component, on_progress: ProgressFn) -> Path:
    """Fetch an executable that is published directly rather than in an archive."""
    target = binary_dir(vendor, component)
    if binary_installed(vendor, component):
        on_progress(1.0, f"{component.label} is already installed")
        return target

    destination = target / component.required_files[0]
    destination.parent.mkdir(parents=True, exist_ok=True)

    cache = vendor / DOWNLOAD_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    offline = cache / component.offline_filename
    if offline.is_file():
        on_progress(None, f"Using {offline.name} from vendor/{DOWNLOAD_CACHE_DIR}")
        shutil.copyfile(offline, destination)
    else:
        errors: list[str] = []
        for source in component.sources:
            try:
                _download(source.url, destination, source.sha256, on_progress)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source.url}: {exc}")
        else:
            raise ProvisionError(
                f"Could not download {component.label}. Either allow access to the URL "
                f"below, or download it yourself and save it as "
                f"vendor/{DOWNLOAD_CACHE_DIR}/{component.offline_filename}.\n"
                + "\n".join(errors)
            )

    _make_executable(destination)
    on_progress(1.0, f"{component.label} ready")
    return target


def ytdlp_binary(vendor: Path) -> Path | None:
    path = binary_dir(vendor, YTDLP_COMPONENT) / YTDLP_COMPONENT.required_files[0]
    return path if path.is_file() else None


# --- whisper -----------------------------------------------------------------
def whisper_cache_dir(vendor: Path) -> Path:
    return vendor / "models" / "whisper"


def whisper_installed(vendor: Path, model: str) -> bool:
    """True when a converted model is already cached locally.

    The hub lays snapshots out as ``models--<org>--<repo>/snapshots/<rev>/``; the
    presence of ``model.bin`` in any snapshot is the reliable signal.
    """
    cache = whisper_cache_dir(vendor)
    if not cache.exists():
        return False
    token = model.replace("/", "--").replace(".", "")
    for candidate in cache.glob("**/model.bin"):
        parent = str(candidate.parent).replace("\\", "/").lower()
        if token.lower() in parent.replace(".", "") or model.lower() in parent:
            return True
    return False


def _configure_hub_env(vendor: Path) -> None:
    """Keep the model hub inside ``vendor/`` and quiet."""
    cache = whisper_cache_dir(vendor)
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ["HF_HOME"] = str(cache)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache)


def provision_whisper(vendor: Path, model: str, on_progress: ProgressFn) -> Path:
    cache = whisper_cache_dir(vendor)
    if whisper_installed(vendor, model):
        on_progress(1.0, f"Speech-to-text model '{model}' is already installed")
        return cache

    _configure_hub_env(vendor)
    try:
        from faster_whisper import download_model
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ProvisionError(
            "The speech-to-text engine is not installed. Run the launcher again, or "
            'install it with: pip install -e ".[transcribe]"'
        ) from exc

    expected_bytes = whisper_component(model).approx_mb * 1_000_000
    baseline = _dir_size(cache)
    done = False

    def watch() -> None:
        import time

        while not done:
            grown = max(0, _dir_size(cache) - baseline)
            fraction = min(0.99, grown / expected_bytes) if expected_bytes else None
            on_progress(fraction, f"Downloading speech-to-text model… {grown / 1e6:.0f} MB")
            time.sleep(1.0)

    import threading

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        download_model(model, cache_dir=str(cache))
    except Exception as exc:
        raise ProvisionError(
            f"Could not download the '{model}' speech-to-text model: {exc}\n"
            "If this machine has no access to the model hub, copy an existing "
            f"cache folder into {cache}."
        ) from exc
    finally:
        done = True
        watcher.join(timeout=2.0)

    on_progress(1.0, f"Speech-to-text model '{model}' ready")
    return cache


def prepare_whisper_env(vendor: Path) -> None:
    """Point the model hub at ``vendor/`` before the transcriber loads a model."""
    _configure_hub_env(vendor)


# --- status ------------------------------------------------------------------
def component_statuses(vendor: Path, whisper_model: str) -> list[ComponentStatus]:
    statuses: list[ComponentStatus] = []
    for component in all_components(whisper_model):
        if component.key == "ffmpeg":
            installed = ffmpeg_installed(vendor)
            location = str(ffmpeg_dir(vendor))
            detail = "ffmpeg and ffprobe found" if installed else "Not installed yet"
        elif component.is_single_binary:
            installed = binary_installed(vendor, component)
            location = str(binary_dir(vendor, component))
            detail = "Installed" if installed else "Not installed yet"
        else:
            installed = whisper_installed(vendor, whisper_model)
            location = str(whisper_cache_dir(vendor))
            detail = "Model cached locally" if installed else "Not downloaded yet"
        statuses.append(
            ComponentStatus(
                key=component.key,
                label=component.label,
                description=component.description,
                installed=installed,
                approx_mb=component.approx_mb,
                location=location,
                detail=detail,
                notes=component.notes,
            )
        )
    return statuses


def provision(component_key: str, vendor: Path, whisper_model: str, on_progress: ProgressFn) -> None:
    if component_key == "ffmpeg":
        provision_ffmpeg(vendor, on_progress)
    elif component_key == "ytdlp":
        provision_binary(vendor, YTDLP_COMPONENT, on_progress)
    elif component_key == "whisper":
        provision_whisper(vendor, whisper_model, on_progress)
    else:
        raise ProvisionError(f"Unknown component: {component_key}")


def provision_all(
    vendor: Path, whisper_model: str, on_progress: ProgressFn, *, include_whisper: bool = True
) -> None:
    provision_ffmpeg(vendor, on_progress)
    # Optional: link import still works for direct file URLs without it, so a
    # failure here must not stop the rest of setup.
    try:
        provision_binary(vendor, YTDLP_COMPONENT, on_progress)
    except ProvisionError as exc:
        on_progress(None, f"Skipping the media extractor: {exc}")
    if include_whisper:
        provision_whisper(vendor, whisper_model, on_progress)
