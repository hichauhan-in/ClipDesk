"""Files the machine already has: OneDrive, and anything synced into it.

This is the way in that needs no authentication at all. The OneDrive client is
already signed in, already syncing, and already handles tokens, MFA and
conditional access. A SharePoint library added with "Add shortcut to OneDrive"
or "Sync" shows up here too, as an ordinary folder.

Two Windows details matter:

* **Files On-Demand.** A file can be a placeholder with no data on disk. It looks
  normal and reports its full size, but opening it makes the OneDrive client
  fetch it, which for a 2 GB recording is a download with no progress bar of its
  own. Those files are marked so the user is not left wondering.
* **Reparse points.** Every top-level OneDrive folder is a junction, so paths
  must be resolved before any containment check or the check is meaningless.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MEDIA_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
}

# Windows attributes that mean "this is a placeholder, the bytes are in the cloud".
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY_MASK = (
    _FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

#: Folders that are always noise in a picker.
_SKIP_NAMES = {"$recycle.bin", "system volume information", ".git", "__pycache__"}


class BrowseError(ValueError):
    """A path could not be listed, for a reason worth showing the user."""


@dataclass(slots=True)
class Root:
    id: str
    label: str
    path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "path": str(self.path),
            "available": self.path.is_dir(),
        }


@dataclass(slots=True)
class Entry:
    name: str
    #: Path relative to its root, which is all the browser ever needs to send.
    relative: str
    is_dir: bool
    size_bytes: int = 0
    modified: float = 0.0
    cloud_only: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.relative,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
            "cloud_only": self.cloud_only,
        }


def is_cloud_only(path: Path) -> bool:
    """Whether the bytes still live in the cloud rather than on this disk."""
    if sys.platform != "win32":
        return False
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _CLOUD_ONLY_MASK)


def _slug(text: str) -> str:
    keep = [character if character.isalnum() else "-" for character in text.lower()]
    return "".join(keep).strip("-") or "root"


def cloud_roots(extra: list[Path] | None = None) -> list[Root]:
    """Every synced cloud folder this machine has.

    The environment variables cover the common case; the registry catches the
    second and third account, which is normal on a work machine that also has a
    personal sign-in.
    """
    found: dict[str, Path] = {}

    for variable in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        value = os.environ.get(variable, "").strip()
        if value:
            found.setdefault(str(Path(value)), Path(value))

    for path in _registry_folders():
        found.setdefault(str(path), path)

    for path in extra or []:
        found.setdefault(str(path), Path(path))

    roots: list[Root] = []
    seen_ids: set[str] = set()
    for path in found.values():
        if not path.is_dir():
            continue
        base = _slug(path.name)
        identifier = base
        suffix = 2
        while identifier in seen_ids:
            identifier = f"{base}-{suffix}"
            suffix += 1
        seen_ids.add(identifier)
        roots.append(Root(identifier, path.name, path))
    return sorted(roots, key=lambda root: root.label.lower())


def _registry_folders() -> list[Path]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    folders: list[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts") as key:
            index = 0
            while True:
                try:
                    account = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, account) as sub:
                        value, _kind = winreg.QueryValueEx(sub, "UserFolder")
                except OSError:
                    continue
                if value:
                    folders.append(Path(value))
    except OSError:
        return []
    return folders


def resolve_within(root: Root, relative: str) -> Path:
    """Turn a browser-supplied relative path into a real one inside ``root``.

    Everything here arrives over HTTP, so this is the boundary that stops the
    picker from becoming a read-anything API. OneDrive folders are junctions, so
    both sides are resolved before comparing.
    """
    base = root.path.resolve()
    candidate = (base / relative.replace("\\", "/").lstrip("/")).resolve()
    if candidate != base and base not in candidate.parents:
        raise BrowseError("That path is outside the folder being browsed.")
    return candidate


def browse(root: Root, relative: str = "", *, media_only: bool = True) -> list[Entry]:
    """List one folder: sub-folders first, then the files worth offering."""
    target = resolve_within(root, relative)
    if not target.is_dir():
        raise BrowseError("That folder no longer exists.")

    base = root.path.resolve()
    directories: list[Entry] = []
    files: list[Entry] = []

    try:
        listing = list(os.scandir(target))
    except PermissionError as exc:
        raise BrowseError("Windows would not let ClipDesk read that folder.") from exc
    except OSError as exc:
        raise BrowseError(f"That folder could not be read: {exc}") from exc

    for item in listing:
        if item.name.startswith(".") or item.name.lower() in _SKIP_NAMES:
            continue
        path = Path(item.path)
        try:
            relative_path = path.resolve().relative_to(base).as_posix()
        except ValueError:
            # A junction pointing outside the root: skip rather than expose it.
            continue

        try:
            if item.is_dir():
                directories.append(Entry(item.name, relative_path, True))
                continue
        except OSError:
            continue

        if media_only and path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        try:
            info = item.stat()
        except OSError:
            continue
        files.append(
            Entry(
                item.name,
                relative_path,
                False,
                size_bytes=info.st_size,
                modified=info.st_mtime,
                cloud_only=is_cloud_only(path),
            )
        )

    directories.sort(key=lambda entry: entry.name.lower())
    files.sort(key=lambda entry: entry.name.lower())
    return directories + files


def find_root(roots: list[Root], root_id: str) -> Root:
    for root in roots:
        if root.id == root_id:
            return root
    raise BrowseError(f"No folder called '{root_id}'.")


#: Bounds on a search. A work OneDrive can hold a hundred thousand files, and an
#: unbounded walk would hang the request for a result nobody would read anyway.
_SEARCH_MAX_ENTRIES = 40_000
_SEARCH_MAX_RESULTS = 200


def search(root: Root, query: str, *, limit: int = _SEARCH_MAX_RESULTS) -> list[Entry]:
    """Find media files by name anywhere under ``root``.

    Clicking through ``Initiatives/FY26/Content Creation FY26/WDAC`` to reach
    three files is not a reasonable way to find a recording, and a folder someone
    has just shared could be anywhere.
    """
    needle = query.strip().lower()
    if len(needle) < 2:
        raise BrowseError("Type at least two characters to search.")

    base = root.path.resolve()
    results: list[Entry] = []
    scanned = 0
    stack = [base]

    while stack and len(results) < limit and scanned < _SEARCH_MAX_ENTRIES:
        current = stack.pop()
        try:
            listing = list(os.scandir(current))
        except OSError:
            continue

        for item in listing:
            scanned += 1
            if item.name.startswith(".") or item.name.lower() in _SKIP_NAMES:
                continue
            try:
                if item.is_dir():
                    stack.append(Path(item.path))
                    continue
            except OSError:
                continue

            path = Path(item.path)
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            if needle not in item.name.lower():
                continue
            try:
                relative = path.resolve().relative_to(base).as_posix()
                info = item.stat()
            except (OSError, ValueError):
                continue
            results.append(
                Entry(
                    item.name,
                    relative,
                    False,
                    size_bytes=info.st_size,
                    modified=info.st_mtime,
                    cloud_only=is_cloud_only(path),
                )
            )
            if len(results) >= limit:
                break

    results.sort(key=lambda entry: entry.name.lower())
    return results


#: Big enough to be quick, small enough that progress moves on a cloud file that
#: is being hydrated a chunk at a time.
_COPY_CHUNK = 1024 * 1024 * 4

ProgressFn = Callable[[float | None, str], None]


def copy_into(source: Path, destination: Path, on_progress: ProgressFn) -> int:
    """Copy a picked file into a project, reporting progress.

    A cloud-only placeholder is downloaded by the OneDrive client the moment it
    is read, so this can be slow for a reason the byte count does not show. The
    caller says so up front; here it just means the first chunk may take a while.
    """
    if not source.is_file():
        raise BrowseError(f"'{source.name}' is no longer there.")

    total = source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            while True:
                chunk = reader.read(_COPY_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
                written += len(chunk)
                on_progress(
                    written / total if total else None,
                    f"Copying… {written / 1e6:.0f} / {total / 1e6:.0f} MB",
                )
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BrowseError(
            f"'{source.name}' could not be read: {exc}. If it is stored online "
            "only, open it once in File Explorer so OneDrive downloads it, then "
            "try again."
        ) from exc

    return written
