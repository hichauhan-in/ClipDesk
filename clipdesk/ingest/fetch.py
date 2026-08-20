"""Downloading a video from a link.

Two strategies, tried in the order that suits the link:

* **Direct HTTP** — streams the file straight to disk. Works for plain file URLs
  and for share links that can be rewritten into a download endpoint.
* **yt-dlp** — a media extractor that understands page structure. Needed for
  YouTube and similar, and for SharePoint/Stream where the video is delivered as
  a manifest rather than a file.

The hard part is not downloading; it is failing *clearly*. A link behind a
sign-in returns a perfectly valid 200 with an HTML login page, which would
otherwise be saved as ``video.mp4`` and fail much later inside ffmpeg. Every
response is checked for that before anything is kept.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipdesk.ingest.links import Link, LinkKind, classify

ProgressFn = Callable[[float | None, str], None]

_CHUNK = 1024 * 512
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")

# A response that is really a sign-in page or an error, not a video.
_HTML_TYPES = ("text/html", "application/xhtml")

SUPPORTED_BROWSERS = ("edge", "chrome", "firefox", "brave", "vivaldi", "opera", "safari")

# YouTube signs its media URLs with a challenge that only runs as JavaScript, so
# the extractor needs a runtime to solve it. Only deno is enabled by default and
# almost nobody has deno; without one the formats are dropped and the download
# ends as a 403 that reads like a sign-in problem. Named in yt-dlp's own order
# of preference.
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")

# The extractor's default client resolves to android_vr, whose media URLs 403.
# These are the clients that actually serve bytes. The web ones are deliberately
# absent: they still resolve, but for some videos they offer nothing above 360p,
# so including them buys a download that succeeds at a quality nobody wanted.
YOUTUBE_CLIENTS = "mweb,tv_simply,web_embedded,android"


def _js_runtime_args() -> list[str]:
    """``--js-runtimes`` for whatever is installed, or nothing if none is."""
    args: list[str] = []
    for runtime in JS_RUNTIMES:
        found = shutil.which(runtime)
        if found:
            args += ["--js-runtimes", f"{runtime}:{found}"]
    return args


def js_runtime_available() -> bool:
    return any(shutil.which(runtime) for runtime in JS_RUNTIMES)



class FetchError(RuntimeError):
    """A download failed for a reason worth showing the user verbatim."""


@dataclass(slots=True)
class FetchResult:
    path: Path
    source_url: str
    method: str  # "direct" | "yt-dlp"
    bytes_written: int


def _safe_name(name: str, fallback: str = "video.mp4") -> str:
    cleaned = _SAFE_NAME_RE.sub("_", Path(name).name).strip(". ")
    return cleaned or fallback


def _name_from_headers(response: httpx.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = _FILENAME_RE.search(disposition)
    if match:
        from urllib.parse import unquote

        return _safe_name(unquote(match.group(1)))
    return ""


def _looks_like_a_web_page(response: httpx.Response) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    return any(kind in content_type for kind in _HTML_TYPES)


def _auth_message(link: Link) -> str:
    if link.kind in {LinkKind.SHAREPOINT, LinkKind.ONEDRIVE}:
        return (
            "That link returned a sign-in page instead of the video. Tick "
            "\"Use my browser session\" so ClipDesk can reuse the sign-in you already "
            "have, or open the link, choose Download, and upload the file instead."
        )
    if link.kind is LinkKind.GOOGLE_DRIVE:
        return (
            "That Drive link returned a sign-in or permission page. Set the file's "
            "sharing to \"Anyone with the link\", or download it and upload the file."
        )
    return (
        "That link returned a web page rather than a video file. It probably needs "
        "a sign-in, or it points at a page rather than the file itself."
    )


# --- direct ------------------------------------------------------------------
def _too_big(limit_mb: int, size_bytes: int) -> str:
    return (
        f"That file is {size_bytes / 1e6:.0f} MB, over the {limit_mb} MB limit. "
        "Raise 'Maximum download size' in Settings if you really want it."
    )


def fetch_direct(
    link: Link,
    destination_dir: Path,
    on_progress: ProgressFn,
    *,
    timeout: float = 60.0,
    max_mb: int = 0,
    cookie_header: str = "",
) -> FetchResult:
    destination_dir.mkdir(parents=True, exist_ok=True)
    on_progress(None, "Connecting…")

    limit = max_mb * 1_000_000 if max_mb > 0 else 0

    headers = {
        # Some services vary their response on this; a browser-ish agent gets the
        # file rather than a "please use a supported browser" page.
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClipDesk/0.1",
        "accept": "*/*",
    }
    if cookie_header:
        headers["cookie"] = cookie_header

    try:
        with httpx.stream(
            "GET",
            link.download_url,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, read=300.0),
            headers=headers,
        ) as response:
            if response.status_code in {401, 403}:
                raise FetchError(_auth_message(link))
            if response.status_code == 404:
                raise FetchError("That link returned 404 — the file may have been moved.")
            response.raise_for_status()

            if _looks_like_a_web_page(response):
                raise FetchError(_auth_message(link))

            name = _name_from_headers(response) or link.suggested_name or "video.mp4"
            if not Path(name).suffix:
                name += ".mp4"
            path = destination_dir / _safe_name(name)

            total = int(response.headers.get("content-length") or 0)
            if limit and total > limit:
                # Refuse before writing anything when the server declares the size.
                raise FetchError(_too_big(max_mb, total))

            written = 0
            with path.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
                    if limit and written > limit:
                        # Servers are not obliged to send content-length, so the
                        # running total has to be checked as well or the cap is
                        # trivially bypassed by omitting the header.
                        handle.close()
                        path.unlink(missing_ok=True)
                        raise FetchError(_too_big(max_mb, written))
                    if total:
                        on_progress(
                            written / total,
                            f"Downloading… {written / 1e6:.0f} / {total / 1e6:.0f} MB",
                        )
                    else:
                        on_progress(None, f"Downloading… {written / 1e6:.0f} MB")
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"The download failed with HTTP {exc.response.status_code}.") from exc
    except httpx.RequestError as exc:
        raise FetchError(f"Could not reach that link: {exc}") from exc

    if written < 10_000:
        # Far too small to be a recording; almost always an error page.
        path.unlink(missing_ok=True)
        raise FetchError(_auth_message(link))

    on_progress(1.0, f"Downloaded {written / 1e6:.0f} MB")
    return FetchResult(path=path, source_url=link.original, method="direct", bytes_written=written)


# --- yt-dlp ------------------------------------------------------------------
def _resolve_extractor(vendor_dir: Path | None) -> list[str] | None:
    """The extractor command, preferring the copy provisioned into ``vendor/``.

    The vendored form is a zipapp, so it runs under the interpreter ClipDesk is
    already using — no separate executable for Application Control to block.
    """
    if vendor_dir is not None:
        from clipdesk.bootstrap import ytdlp_binary

        vendored = ytdlp_binary(vendor_dir)
        if vendored is not None:
            return [sys.executable, str(vendored)]

    on_path = shutil.which("yt-dlp")
    if on_path:
        return [on_path]

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_available(vendor_dir: Path | None = None) -> bool:
    return _resolve_extractor(vendor_dir) is not None


def fetch_with_ytdlp(
    link: Link,
    destination_dir: Path,
    on_progress: ProgressFn,
    *,
    ffmpeg_dir: Path | None = None,
    vendor_dir: Path | None = None,
    browser_cookies: str = "",
    cookie_file: Path | None = None,
    timeout: float = 3600.0,
    max_mb: int = 0,
    youtube_clients: str = YOUTUBE_CLIENTS,
) -> FetchResult:
    executable = _resolve_extractor(vendor_dir)
    if executable is None:
        raise FetchError(
            "This link needs the media extractor, which is not installed. Open "
            "Settings and install it under Dependencies."
        )

    destination_dir.mkdir(parents=True, exist_ok=True)
    template = str(destination_dir / "%(title).120B.%(ext)s")

    command = [
        *executable,
        "--newline",
        "--no-color",
        "--no-playlist",
        "--no-part",
        "--restrict-filenames",
        "--progress-template",
        (
            "PROGRESS:%(progress._percent_str)s|%(progress._downloaded_bytes_str)s|"
            "%(progress._total_bytes_estimate_str)s"
        ),
        # Prefer a single already-merged mp4 to avoid a remux step where possible.
        "--format",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        template,
        "--print",
        "after_move:FINALPATH:%(filepath)s",
    ]
    command += _js_runtime_args()
    if youtube_clients:
        # Scoped to the youtube extractor, so it is inert for every other site.
        command += ["--extractor-args", f"youtube:player_client={youtube_clients}"]
    if ffmpeg_dir is not None:
        command += ["--ffmpeg-location", str(ffmpeg_dir)]
    if max_mb > 0:
        # Only stops downloads the extractor can size up front, which is most of
        # them; the disk-space check after the fact is the real backstop.
        command += ["--max-filesize", f"{max_mb}M"]
    if browser_cookies:
        if browser_cookies not in SUPPORTED_BROWSERS:
            raise FetchError(f"Unsupported browser '{browser_cookies}'.")
        command += ["--cookies-from-browser", browser_cookies]
    elif cookie_file is not None:
        command += ["--cookies", str(cookie_file)]
    command.append(link.original)

    on_progress(None, "Starting the media extractor…")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FetchError("The media extractor could not be started.") from exc

    final_path: Path | None = None
    tail: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]

        if line.startswith("FINALPATH:"):
            final_path = Path(line[len("FINALPATH:") :].strip())
        elif line.startswith("PROGRESS:"):
            percent, done, total = (line[len("PROGRESS:") :].split("|") + ["", ""])[:3]
            fraction = _parse_percent(percent)
            on_progress(fraction, f"Downloading… {done.strip()} of {total.strip()}".strip())

    process.wait(timeout=timeout)

    if process.returncode != 0 or final_path is None or not final_path.is_file():
        raise FetchError(_ytdlp_error(tail, link, browser_cookies))

    size = final_path.stat().st_size
    if max_mb > 0 and size > max_mb * 1_000_000:
        final_path.unlink(missing_ok=True)
        raise FetchError(_too_big(max_mb, size))
    on_progress(1.0, f"Downloaded {size / 1e6:.0f} MB")
    return FetchResult(
        path=final_path, source_url=link.original, method="yt-dlp", bytes_written=size
    )


def _parse_percent(text: str) -> float | None:
    try:
        return max(0.0, min(1.0, float(text.strip().rstrip("%")) / 100))
    except (TypeError, ValueError):
        return None


def _ytdlp_error(tail: list[str], link: Link, browser_cookies: str) -> str:
    joined = "\n".join(tail[-8:])
    # Matched against everything captured, not just the shown tail: the runtime
    # warning is printed before the download starts and the error that follows
    # it can be many lines later.
    lowered = "\n".join(tail).lower()

    # Chromium 127 wraps the cookie key so only the browser can unwrap it, so
    # reading Edge's or Chrome's cookie store now fails however the user asks.
    # This is not something they did wrong and not something a retry fixes.
    if "dpapi" in lowered or "could not copy" in lowered or "failed to decrypt" in lowered:
        return (
            f"Windows will not let ClipDesk read {browser_cookies or 'that browser'}'s "
            "cookies. Recent versions of Edge and Chrome encrypt them so that only the "
            "browser itself can read them, and closing the browser does not change "
            "that.\n\n"
            "Use \"Paste a signed-in session\" instead: open the video in your browser, "
            "press F12, reload, right-click the request in the Network tab and choose "
            "Copy as cURL. That works without installing anything.\n\n"
            f"{joined}"
        )
    # A missing JavaScript runtime also ends in 403, so it has to be ruled out
    # before the sign-in advice below -- otherwise the user is sent to fetch
    # cookies for a problem cookies cannot fix.
    if "javascript runtime" in lowered or "challenge solving failed" in lowered:
        return (
            "This video needs a JavaScript runtime that is not installed. Its provider "
            "signs downloads with a challenge that only runs as JavaScript, and "
            "without one the extractor is refused with a 403 that looks like a "
            "sign-in problem.\n\n"
            "Install Node.js from https://nodejs.org and try again — ClipDesk picks "
            "it up automatically, and nothing else needs configuring.\n\n"
            f"{joined}"
        )
    if "sign in" in lowered or "login" in lowered or "cookies" in lowered or "403" in lowered:
        if not js_runtime_available():
            return (
                "The download was refused, and no JavaScript runtime is installed. "
                "This provider signs downloads with a challenge that only runs as "
                "JavaScript, so that is the likely cause rather than sign-in.\n\n"
                "Install Node.js from https://nodejs.org and try again. If it still "
                "fails, use \"Paste a signed-in session\".\n\n"
                f"{joined}"
            )
        if not browser_cookies:
            return (
                "That video needs a signed-in session. Use \"Paste a signed-in session\" "
                "to give ClipDesk the one you already have in your browser.\n\n"
                f"{joined}"
            )
        return (
            f"Signing in with your {browser_cookies} session did not work. Try "
            "\"Paste a signed-in session\" instead, which does not depend on reading "
            f"the browser's cookie store.\n\n{joined}"
        )
    if "unsupported url" in lowered:
        return (
            "The media extractor does not recognise that link. If it points at a "
            f"file, paste the direct file URL instead.\n\n{joined}"
        )
    return f"The download failed.\n\n{joined}"


# --- orchestration -----------------------------------------------------------
def fetch(
    url: str,
    destination_dir: Path,
    on_progress: ProgressFn,
    *,
    ffmpeg_dir: Path | None = None,
    vendor_dir: Path | None = None,
    browser_cookies: str = "",
    cookie_file: Path | None = None,
    max_mb: int = 0,
    youtube_clients: str = YOUTUBE_CLIENTS,
) -> FetchResult:
    """Download ``url`` into ``destination_dir``, choosing the best strategy.

    A direct download is tried first for anything that looks like a file, because
    it is faster and needs no extractor. The extractor is the fallback, and the
    first choice for sites where a plain GET cannot work.
    """
    link = classify(url)
    cookie_header = _header_from_jar(cookie_file, link.download_url) if cookie_file else ""

    # With a saved session the direct path is the better bet for Microsoft links:
    # the download endpoint is known exactly, whereas the extractor has to guess
    # at page structure and does not recognise the newer cloud.microsoft hosts.
    tenant_link = link.kind in {LinkKind.SHAREPOINT, LinkKind.ONEDRIVE}
    if link.prefers_ytdlp and not (cookie_header and tenant_link):
        return fetch_with_ytdlp(
            link,
            destination_dir,
            on_progress,
            ffmpeg_dir=ffmpeg_dir,
            vendor_dir=vendor_dir,
            browser_cookies=browser_cookies,
            cookie_file=cookie_file,
            max_mb=max_mb,
            youtube_clients=youtube_clients,
        )
    if browser_cookies:
        return fetch_with_ytdlp(
            link,
            destination_dir,
            on_progress,
            ffmpeg_dir=ffmpeg_dir,
            vendor_dir=vendor_dir,
            browser_cookies=browser_cookies,
            cookie_file=cookie_file,
            max_mb=max_mb,
            youtube_clients=youtube_clients,
        )

    try:
        return fetch_direct(
            link, destination_dir, on_progress, max_mb=max_mb, cookie_header=cookie_header
        )
    except FetchError as direct_error:
        if not ytdlp_available(vendor_dir):
            raise
        on_progress(None, "Direct download did not work — trying the media extractor…")
        try:
            return fetch_with_ytdlp(
                link,
                destination_dir,
                on_progress,
                ffmpeg_dir=ffmpeg_dir,
                vendor_dir=vendor_dir,
                cookie_file=cookie_file,
                max_mb=max_mb,
                youtube_clients=youtube_clients,
            )
        except FetchError as extractor_error:
            raise FetchError(f"{direct_error}\n\n{extractor_error}") from extractor_error


def cookie_header_from_jar(cookie_file: Path | None, url: str = "") -> str:
    """Flatten a saved jar into a Cookie header for anything using plain HTTP."""
    from clipdesk.ingest.cookies import parse_netscape

    if cookie_file is None:
        return ""
    try:
        entries = parse_netscape(cookie_file.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return "; ".join(f"{name}={value}" for name, (_domain, value) in entries.items())


_header_from_jar = cookie_header_from_jar


def describe(url: str, vendor_dir: Path | None = None) -> dict[str, object]:
    """What ClipDesk thinks a link is, for the UI to show before committing."""
    link = classify(url)
    return {
        "kind": link.kind.value,
        "needs_auth": link.needs_auth,
        "note": link.note,
        "suggested_name": link.suggested_name,
        "uses_extractor": link.prefers_ytdlp,
        "extractor_available": ytdlp_available(vendor_dir),
        "is_folder": link.is_folder,
    }
