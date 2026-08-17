"""Where things live on disk.

Every path the app uses is derived from the repository root so the whole tree —
code, bundled ffmpeg, model weights and per-video projects — can be zipped up and
moved to another machine without anything to reinstall.
"""

from __future__ import annotations

import os
import sys
import hashlib
from pathlib import Path


def app_root() -> Path:
    """The directory that contains ``clipdesk/``, ``config/`` and ``vendor/``.

    When frozen into a single executable the bundle directory is used instead, so
    a packaged build finds its bundled binaries the same way a source checkout
    does.
    """
    override = os.environ.get("CLIPDESK_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve(path: str | Path) -> Path:
    """Resolve ``path`` against the app root unless it is already absolute."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (app_root() / candidate).resolve()


def user_state_dir() -> Path:
    """Per-user directory for cross-process handshake files."""
    base = os.environ.get("CLIPDESK_STATE_DIR")
    if base:
        directory = Path(base).expanduser()
    else:
        directory = Path.home() / ".clipdesk"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def isolated_user_dir(root: Path, identity: str) -> Path:
    """A non-identifying, traversal-safe storage root for one authenticated user."""
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = root / "users" / key
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory
