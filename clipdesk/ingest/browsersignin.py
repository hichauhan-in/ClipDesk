"""Signing in to a tenant link, without asking the user to paste anything.

The problem this solves: an org-scoped OneDrive or SharePoint link always ends at
``login.microsoftonline.com``, and ClipDesk cannot sign in on the user's behalf.
Reading Edge's own cookies is no longer possible either -- Chromium 127 encrypts
them so only the browser can read them.

So ClipDesk opens *its own* Edge window instead. The browser it launched will
hand its cookies back over the DevTools protocol, and because the machine is
usually joined to the tenant the sign-in is often silent. The profile is kept, so
this happens once and later links go through with no window at all.

Nothing here needs an app registration, an admin, or a download: Edge is already
on every managed Windows machine.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

#: Hosts that mean "still signing in", not "arrived".
_SIGN_IN_HOSTS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
    "msft.sts.microsoft.com",
    "device.login.microsoftonline.com",
)

_EDGE_CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)

_NO_WINDOW = 0  # the browser is meant to be seen
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: How long a tab must sit on a non-sign-in page before its address is accepted
#: whatever its shape. OneDrive changes what a share resolves to, and an
#: unrecognised address should cost a few seconds, not the whole sign-in.
_SETTLE_GRACE_S = 10.0


class SignInError(RuntimeError):
    """Sign-in could not be completed, for a reason worth showing the user."""


@dataclass(slots=True)
class SignInResult:
    host: str
    cookies: dict[str, str]
    final_url: str
    #: Every host that ended up with a session, because the tenant host the
    #: redirect lands on is usually not the host that was clicked.
    by_host: dict[str, dict[str, str]]


def find_browser() -> tuple[str, str] | None:
    """The browser to drive, as ``(label, path)``."""
    if sys.platform != "win32":
        return None
    for template in _EDGE_CANDIDATES:
        path = os.path.expandvars(template)
        if os.path.isfile(path):
            return ("Microsoft Edge", path)
    for template in _CHROME_CANDIDATES:
        path = os.path.expandvars(template)
        if os.path.isfile(path):
            return ("Google Chrome", path)
    return None


def profile_dir(state_dir: Path) -> Path:
    """Where the sign-in is remembered between runs."""
    directory = state_dir / "browser"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _resolution_key(url: str) -> str:
    """Stable identity for an opaque share link, excluding optional tracking data."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.endswith((".sharepoint.com", ".sharepoint.us", ".cloud.microsoft")) and re.match(
        r"^/:[a-z]:/", parsed.path, re.IGNORECASE
    ):
        return f"{parsed.scheme.lower()}://{host}{parsed.path.rstrip('/')}"
    return url.strip()


def remember_resolution(state_dir: Path, original_url: str, resolved_url: str) -> None:
    """Remember where an opaque share token landed without storing that token as a key."""
    if not original_url or not resolved_url:
        return
    path = state_dir / "link-resolutions.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    key = hashlib.sha256(_resolution_key(original_url).encode("utf-8")).hexdigest()
    saved[key] = {"url": resolved_url, "saved_at": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def recalled_resolution(
    state_dir: Path, original_url: str, *, max_age_days: int = 14
) -> str | None:
    """Return a recent signed-in destination for an opaque share link."""
    path = state_dir / "link-resolutions.json"
    if not path.is_file() or not original_url:
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    key = hashlib.sha256(_resolution_key(original_url).encode("utf-8")).hexdigest()
    entry = saved.get(key) or {}
    if time.time() - float(entry.get("saved_at") or 0) > max_age_days * 86_400:
        return None
    url = str(entry.get("url") or "")
    return url if urlparse(url).scheme in {"http", "https"} else None


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _is_sign_in(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == known or host.endswith("." + known) for known in _SIGN_IN_HOSTS)


def _has_arrived(url: str) -> bool:
    """Whether the controlled tab has reached a usable signed-in destination."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or _is_sign_in(url):
        return False
    if parsed.hostname == "onedrive.cloud.microsoft":
        path = parsed.path.rstrip("/")
        if re.match(r"^/:[a-z]:/", path, re.IGNORECASE):
            return False
        if path == "/shared":
            query = parse_qs(parsed.query)
            return bool(query.get("id") and query.get("listurl"))
    host = (parsed.hostname or "").lower()
    if host.endswith((".sharepoint.com", ".sharepoint.us", ".cloud.microsoft")) and re.match(
        r"^/:[a-z]:/", parsed.path, re.IGNORECASE
    ):
        return False
    return True


def _debug_json(port: int, path: str, timeout: float = 3.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.load(response)


def _active_debug_port(profile: Path) -> int:
    """The port a browser already running on this profile is listening on.

    Chromium writes this file itself. It is the only reliable way to find a
    window ClipDesk opened earlier, and it matters because launching a second
    time on the same profile does not start a second browser: the new process
    hands the URL to the running one and exits, so the port asked for on the
    command line never opens.
    """
    try:
        first = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        return int(first.strip())
    except (OSError, ValueError, IndexError):
        return 0


def _live_debugger(port: int) -> bool:
    if port <= 0:
        return False
    try:
        _debug_json(port, "/json/version", timeout=1.5)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def _open_tab(port: int, url: str) -> None:
    """Point an already-running browser at ``url``."""
    target = f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}"
    request = urllib.request.Request(target, method="PUT")
    try:
        urllib.request.urlopen(request, timeout=5.0).close()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        # Older builds serve the same endpoint over GET.
        try:
            urllib.request.urlopen(target, timeout=5.0).close()
        except (urllib.error.URLError, OSError):
            pass


def _wait_for_debugger(port: int, deadline: float, profile: Path) -> tuple[int, dict]:
    """Wait for the DevTools endpoint, following the browser if it moved ports."""
    while time.time() < deadline:
        try:
            return port, _debug_json(port, "/json/version")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            # The launch may have been absorbed by a browser already running on
            # this profile, which is listening somewhere else.
            existing = _active_debug_port(profile)
            if existing and existing != port and _live_debugger(existing):
                return existing, _debug_json(existing, "/json/version")
            time.sleep(0.4)
    raise SignInError(
        "The browser started but ClipDesk could not talk to it. If a ClipDesk "
        "sign-in window is already open, close it and try again. Otherwise "
        "corporate policy may block browser automation on this machine; use "
        "\"Paste a signed-in session\" instead."
    )


def _bring_to_foreground(process_id: int, timeout_s: float = 5.0) -> bool:
    """Restore and activate the top-level window owned by the launched browser."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def app_activate() -> bool:
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "exit [int](-not (New-Object -ComObject WScript.Shell).AppActivate([int]$args[0]))",
                    str(process_id),
                ],
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if app_activate():
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        windows: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def collect(window, _data):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == process_id and user32.IsWindowVisible(window):
                windows.append(int(window))
            return True

        user32.EnumWindows(collect, 0)
        if windows:
            window = windows[0]
            foreground = user32.GetForegroundWindow()
            current_thread = kernel32.GetCurrentThreadId()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            target_thread = user32.GetWindowThreadProcessId(window, None)
            attached_foreground = bool(
                foreground_thread
                and foreground_thread != current_thread
                and user32.AttachThreadInput(current_thread, foreground_thread, True)
            )
            attached_target = bool(
                target_thread
                and target_thread != current_thread
                and user32.AttachThreadInput(current_thread, target_thread, True)
            )
            try:
                user32.ShowWindow(window, 9)  # SW_RESTORE
                user32.BringWindowToTop(window)
                user32.SetForegroundWindow(window)
                user32.SetActiveWindow(window)
            finally:
                if attached_target:
                    user32.AttachThreadInput(current_thread, target_thread, False)
                if attached_foreground:
                    user32.AttachThreadInput(current_thread, foreground_thread, False)
            if user32.GetForegroundWindow() == window:
                return True
        time.sleep(0.2)

    # AppActivate can succeed later if Edge delegated the window to a child process.
    return app_activate()


def _page_targets(port: int) -> list[dict]:
    try:
        targets = _debug_json(port, "/json/list")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    return [target for target in targets if target.get("type") == "page"]


def _stop(process: subprocess.Popen | None) -> None:
    """Close a browser this run started, tolerating one that is already gone."""
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    except OSError:
        pass


#: Cookies that actually authorise a SharePoint or OneDrive request. Their
#: presence is the only reliable "the user is through" signal -- the browser sits
#: on an OAuth URL for a while, and other tabs are not evidence of anything.
_AUTH_COOKIE_NAMES = {"fedauth", "rtfa", "edgeaccesscookie", "spoidcrl"}


def _has_auth_cookie(cookies: list[dict]) -> bool:
    return any(str(cookie.get("name", "")).lower() in _AUTH_COOKIE_NAMES for cookie in cookies)


def _session_hosts(cookies: list[dict]) -> set[str]:
    """Hosts that actually hold a session, taken from the cookies themselves.

    A folder share is listed against the tenant host, and the address bar may
    never name it, so the cookie jar is the more reliable place to look.
    """
    hosts: set[str] = set()
    for cookie in cookies:
        if str(cookie.get("name", "")).lower() not in _AUTH_COOKIE_NAMES:
            continue
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain and not any(domain == known or domain.endswith("." + known)
                              for known in _SIGN_IN_HOSTS):
            hosts.add(domain)
    return hosts


async def _read_cookies(ws_url: str) -> list[dict]:
    import websockets

    async with websockets.connect(ws_url, max_size=None) as socket:
        await socket.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        while True:
            message = json.loads(await socket.recv())
            if message.get("id") == 1:
                if "error" in message:
                    raise SignInError(str(message["error"].get("message", "cookie read failed")))
                return message["result"]["cookies"]


def _matching(cookies: list[dict], host: str) -> dict[str, str]:
    """Cookies that would be sent to ``host``, newest wins on a name clash."""
    host = host.lower()
    picked: dict[str, str] = {}
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            picked[str(cookie["name"])] = str(cookie.get("value", ""))
    return picked


def sign_in(
    url: str,
    state_dir: Path,
    on_progress,
    *,
    timeout_s: float = 240.0,
) -> SignInResult:
    """Open a browser at ``url`` and return the session it ends up with.

    Blocks until the browser is no longer sitting on a sign-in page, which is the
    signal that the user is through. Usually instant on a joined machine.
    """
    browser = find_browser()
    if browser is None:
        raise SignInError(
            "No Edge or Chrome installation was found, so ClipDesk cannot open a "
            "sign-in window."
        )
    label, executable = browser

    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise SignInError("That is not a link ClipDesk can sign in to.")

    port = _free_port()
    profile = profile_dir(state_dir)
    process: subprocess.Popen | None = None

    def start() -> tuple[int, dict]:
        """Reuse a window already on this profile, or open one, and connect.

        Chromium forwards to an instance already running on the profile and then
        exits, so launching blind leaves ClipDesk talking to a port nothing is
        listening on.
        """
        nonlocal process
        running = _active_debug_port(profile)
        if _live_debugger(running):
            on_progress(None, f"Using the {label} window ClipDesk already opened…")
            _open_tab(running, url)
            return _wait_for_debugger(running, time.time() + 30, profile)

        on_progress(None, f"Opening {label}…")
        chosen = _free_port()
        process = subprocess.Popen(
            [
                executable,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={chosen}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                url,
            ],
            creationflags=_NO_WINDOW,
        )
        return _wait_for_debugger(chosen, time.time() + 30, profile)

    deadline = time.time() + timeout_s
    try:
        port, info = start()
        if process is not None:
            try:
                _bring_to_foreground(process.pid)
            except Exception:  # noqa: BLE001 - window focus must never break authentication
                pass
        on_progress(
            None,
            "Sign in to Microsoft in the window that just opened. This is only "
            "needed once.",
        )

        # Only this tab counts. Edge restores other tabs and opens a new-tab page,
        # and treating any of those as "arrived" reports success while the user is
        # still looking at a sign-in prompt.
        tab_id = ""
        settled_at = 0.0
        current_url = ""
        while time.time() < deadline and not tab_id:
            for target in _page_targets(port):
                if host in str(target.get("url", "")) or _is_sign_in(str(target.get("url", ""))):
                    tab_id = str(target.get("id", ""))
                    break
            if not tab_id:
                time.sleep(0.5)

        # Whether the browser is still there is answered by the DevTools endpoint,
        # not by the process that was launched: Edge hands the URL to an instance
        # already running on this profile and exits, leaving a live window behind
        # a dead launcher. Losing the endpoint is usually a stale port left by an
        # earlier attempt whose browser was still shutting down, so try once more
        # from scratch before giving up on a window the user can plainly see.
        missed = 0
        relaunched = False
        while time.time() < deadline:
            if _live_debugger(port):
                missed = 0
            else:
                missed += 1
                if missed >= 3:
                    if relaunched:
                        raise SignInError(
                            "The sign-in window was closed before it finished."
                        )
                    relaunched = True
                    missed = 0
                    tab_id = ""
                    on_progress(None, "Reconnecting to the sign-in window…")
                    _stop(process)
                    process = None
                    port, info = start()
                    continue
                time.sleep(0.8)
                continue

            targets = {str(target.get("id", "")): target for target in _page_targets(port)}
            # A relaunch leaves no tab to follow, so pick one up again rather than
            # watching an id that belonged to the browser that went away.
            if not tab_id:
                for target in targets.values():
                    candidate = str(target.get("url", ""))
                    if host in candidate or _is_sign_in(candidate):
                        tab_id = str(target.get("id", ""))
                        break
            current = targets.get(tab_id) or {}
            current_url = str(current.get("url", ""))

            # OneDrive keeps changing the address a share resolves to, and a shape
            # nobody anticipated used to mean waiting for ever. Once the tab has
            # been off the sign-in pages for a while, stop insisting on a known
            # address and let the session cookie be the judge.
            if current_url and not _is_sign_in(current_url):
                settled_at = settled_at or time.time()
            else:
                settled_at = 0.0
            settled = bool(settled_at) and time.time() - settled_at >= _SETTLE_GRACE_S

            if _has_arrived(current_url) or settled:
                cookies = asyncio.run(_read_cookies(info["webSocketDebuggerUrl"]))
                # An OAuth round trip passes through plenty of non-login URLs, so
                # the destination alone is not proof. The session cookie is.
                if _has_auth_cookie(cookies):
                    by_host: dict[str, dict[str, str]] = {}
                    # Every tenant host holding a session, not just the two that
                    # happen to be named in the URL: a folder share is listed
                    # against the tenant host, which the address may never show.
                    candidates = {host, (urlparse(current_url).hostname or "").lower()}
                    candidates |= _session_hosts(cookies)
                    for candidate in candidates:
                        if candidate:
                            found = _matching(cookies, candidate)
                            if found:
                                by_host[candidate] = found
                    if by_host:
                        return SignInResult(
                            host, by_host.get(host, {}), current_url, by_host
                        )
            time.sleep(0.8)

        raise SignInError(
            "Timed out waiting for the sign-in to finish. The window was last on "
            f"{current_url or 'no page ClipDesk could see'}. Complete the sign-in "
            "in the browser window, or use \"Paste a signed-in session\"."
        )
    finally:
        # Only close what this call opened. A window the user already had open is
        # theirs, and killing it is what left the profile in a state where the
        # next sign-in could not be reached.
        _stop(process)
