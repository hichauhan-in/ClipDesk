"""Managing the files an action produced: delete, bundle, reveal.

Everything here resolves names through :meth:`Project.output_path`, which refuses
anything that escapes the project's ``output/`` folder — these entry points take
filenames straight from the browser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from clipdesk.models import ArtifactKind
from clipdesk.store import Project


class OutputError(ValueError):
    pass


def _bare_name(name: str) -> str:
    """Accept only a plain filename.

    Stripping a path down to its last component would work, but it silently
    turns ``sub/dir/notes.md`` into ``notes.md`` — a surprising remap that hides
    a caller bug. Refusing is clearer and just as safe.
    """
    candidate = name.strip()
    if not candidate or candidate in {".", ".."} or candidate != Path(candidate).name:
        raise OutputError(f"Invalid output name: {name!r}")
    return candidate


def resolve_outputs(project: Project, filenames: list[str]) -> list[Path]:
    """Resolve requested names to real files, rejecting anything unexpected."""
    resolved: list[Path] = []
    missing: list[str] = []
    for name in filenames:
        try:
            path = project.output_path(_bare_name(name))
        except ValueError as exc:
            raise OutputError(str(exc)) from exc
        if path.is_file():
            resolved.append(path)
        else:
            missing.append(name)
    if missing:
        raise OutputError(f"No such output: {', '.join(missing)}")
    if not resolved:
        raise OutputError("No files selected.")
    return resolved


def delete_output(project: Project, filename: str) -> bool:
    """Delete one output and forget the artifact that pointed at it."""
    try:
        path = project.output_path(_bare_name(filename))
    except ValueError as exc:
        raise OutputError(str(exc)) from exc

    existed = path.is_file()
    path.unlink(missing_ok=True)

    remaining = [
        entry for entry in project.meta.artifacts if entry.get("filename") != path.name
    ]
    if len(remaining) != len(project.meta.artifacts):
        project.meta.artifacts = remaining
        project.save()
    return existed


def rename_output(project: Project, filename: str, new_name: str) -> Path:
    """Rename an output and the artifact that owns it, preserving its real file type."""
    try:
        source = project.output_path(_bare_name(filename))
    except ValueError as exc:
        raise OutputError(str(exc)) from exc
    if not source.is_file():
        raise OutputError(f"No such output: {filename}")

    requested = _bare_name(new_name)
    if not Path(requested).suffix:
        requested += source.suffix
    if Path(requested).suffix.lower() != source.suffix.lower():
        raise OutputError(
            f"The file type must stay {source.suffix.lower()}; rename only the part before it."
        )
    try:
        destination = project.output_path(requested)
    except ValueError as exc:
        raise OutputError(str(exc)) from exc
    if destination == source:
        return source
    if destination.exists():
        raise OutputError(f"An output named '{destination.name}' already exists.")

    changed: list[tuple[dict[str, object], str, str]] = []
    source.replace(destination)
    try:
        for entry in project.meta.artifacts:
            if entry.get("filename") != source.name:
                continue
            old_filename = str(entry.get("filename") or "")
            old_id = str(entry.get("id") or "")
            entry["filename"] = destination.name
            kind = str(entry.get("kind") or "output")
            entry["id"] = f"{kind}-{destination.stem}"
            changed.append((entry, old_filename, old_id))
        if not changed:
            raise OutputError(f"No artifact owns the output '{source.name}'.")
        project.save()
    except Exception:
        destination.replace(source)
        for entry, old_filename, old_id in changed:
            entry["filename"] = old_filename
            entry["id"] = old_id
        raise
    return destination


def bundle_outputs(project: Project, filenames: list[str], archive_name: str = "") -> Path:
    """Zip the chosen outputs into ``output/`` and return the archive path.

    Written to disk rather than streamed so the browser gets an ordinary download
    with a real content-length and a resumable, cancellable transfer — rendered
    video adds up quickly. It is registered as an artifact so it shows up in the
    Outputs list and can be deleted, instead of quietly consuming disk.
    """
    files = resolve_outputs(project, filenames)
    stem = Path(archive_name).stem if archive_name else ""
    destination = project.output_path(f"{stem or 'clipdesk-selection'}.zip")

    # Never let a previous bundle end up inside the new one.
    destination.unlink(missing_ok=True)
    files = [path for path in files if path != destination]
    if not files:
        raise OutputError("No files selected.")

    with zipfile.ZipFile(
        destination, "w", allowZip64=True
    ) as archive:
        for path in files:
            compression = (
                zipfile.ZIP_STORED
                if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".aac", ".zip"}
                else zipfile.ZIP_DEFLATED
            )
            archive.write(path, arcname=path.name, compress_type=compression)

    project.add_artifact(
        ArtifactKind.BUNDLE,
        f"Bundle of {len(files)} file(s)",
        destination,
        meta={"contains": ", ".join(path.name for path in files)[:400]},
    )
    return destination


def _raise_explorer_window(directory: Path, timeout_s: float = 2.5) -> bool:
    """Bring the Explorer window showing ``directory`` to the front.

    Opening the folder is not enough: a server process is not the foreground
    application, and Windows only lets the foreground process call
    ``SetForegroundWindow``. Attaching to the current foreground window's input
    queue lifts that restriction for the duration of the call, which is the
    documented way to hand focus to a window you just opened.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    wanted = directory.name or str(directory)
    deadline = time.monotonic() + timeout_s

    def matches(title: str) -> bool:
        # Explorer titles the window "<folder> - File Explorer", or
        # "<folder> and 2 more tabs - File Explorer" once tabs are in play.
        if title == wanted:
            return True
        head = title.rsplit(" - ", 1)[0]
        return head == wanted or head.startswith(f"{wanted} and ")

    def find() -> int:
        found = 0

        def visit(hwnd: int, _param: int) -> bool:
            nonlocal found
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value not in ("CabinetWClass", "ExploreWClass"):
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if matches(title.value):
                found = hwnd
                return False
            return True

        user32.EnumWindows(enum_proc(visit), 0)
        return found

    hwnd = 0
    while not hwnd and time.monotonic() < deadline:
        hwnd = find()
        if not hwnd:
            time.sleep(0.1)
    if not hwnd:
        return False

    foreground = user32.GetForegroundWindow()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = []
    for thread in {foreground_thread, target_thread} - {current_thread, 0}:
        if user32.AttachThreadInput(current_thread, thread, True):
            attached.append(thread)
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE, in case it is minimized.
        user32.BringWindowToTop(hwnd)
        if not user32.SetForegroundWindow(hwnd):
            # Windows refuses focus changes while the session is locked or a
            # full-screen app holds it. Flashing is what the shell does instead.
            class FlashInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD),
                ]

            flash = FlashInfo(ctypes.sizeof(FlashInfo), hwnd, 0x00000003, 3, 0)
            user32.FlashWindowEx(ctypes.byref(flash))
    finally:
        for thread in attached:
            user32.AttachThreadInput(current_thread, thread, False)
    return True


def reveal(path: Path) -> None:
    """Open a folder in the desktop file manager, in front of the browser.

    Only meaningful when the server and the browser are the same machine, which
    is the default deployment. The caller checks that before offering it.
    """
    directory = path if path.is_dir() else path.parent
    directory.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        os.startfile(directory)
        try:
            _raise_explorer_window(directory)
        except OSError:
            # Focus is a courtesy; the folder is already open either way.
            pass
    elif sys.platform == "darwin":
        subprocess.run(["open", str(directory)], check=False)
    else:
        subprocess.run(["xdg-open", str(directory)], check=False)


def source_info(project: Project) -> dict[str, object]:
    """The original recording, so the Outputs screen can account for it.

    Importing from a link writes a file the user never explicitly chose to keep,
    and on a laptop a few 2 GB recordings disappear fast. It is not an artifact —
    everything else is derived from it — so it gets its own entry.
    """
    path = project.source_path
    exists = path.is_file()
    return {
        "filename": project.meta.source_filename,
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else 0,
        "source_url": project.meta.source_url,
        "imported": bool(project.meta.source_url),
        "url": f"/api/projects/{project.id}/preview",
        "transcript_filename": project.meta.transcript_filename,
    }


def delete_source(project: Project) -> bool:
    """Delete the original recording.

    The transcript, analysis and anything already rendered survive, but nothing
    new can be cut afterwards. The caller is responsible for saying so first.
    """
    path = project.source_path
    existed = path.is_file()
    path.unlink(missing_ok=True)
    shutil.rmtree(project.audio_dir, ignore_errors=True)
    project.meta.size_bytes = 0
    project.save()
    return existed
