"""Getting a signed-in session out of the browser and into a download.

The obvious route -- read the browser's cookie store directly -- stopped working
on Windows. Chromium 127 introduced App-Bound Encryption, which wraps the cookie
key so that only the browser's own process can unwrap it; Edge and Chrome both
fail with a DPAPI decryption error even when fully closed. That is deliberate
anti-malware hardening, not a bug to route around, so this module provides the
routes that do work:

* **Paste from DevTools.** The user copies the request as cURL (or just the
  ``Cookie:`` header) and pastes it in. Needs no extension, no admin rights and
  no add-in, which matters on a managed machine where all three are restricted.
* **A cookies.txt file** exported by an extension, for people who already have
  one.

Either way the result is written as a Netscape cookie jar, which is what yt-dlp
wants, and kept per host so it can be reused for the next recording from the
same site.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n# Written by ClipDesk.\n"

#: A cURL header argument. Windows browsers offer three "Copy as cURL" flavours
#: and they are not interchangeable: bash uses 'single quotes', PowerShell uses
#: "double quotes", and cmd escapes every quote with a caret (^"). The caret form
#: is normalised away first, so this only has to cover the two quote styles.
_HEADER_RE = re.compile(r"""-H\s+(['"])(.*?)\1""", re.DOTALL)
#: curl's own cookie flag, used by some "Copy as cURL" variants.
_COOKIE_FLAG_RE = re.compile(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", re.DOTALL)

_HOST_RE = re.compile(r"https?://([^/\s'\"^]+)", re.IGNORECASE)

#: A cookie name is an RFC 6265 token. Anything else -- spaces, colons, slashes,
#: quotes -- means a header line was misread as a cookie, which is how a jar ends
#: up full of entries the downloader then rejects one by one.
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")

#: Set by the browser, carried by every request, and of no use to a downloader.
_NOT_REALLY_COOKIES = {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}


class CookieError(ValueError):
    """The pasted text did not contain a usable session."""


@dataclass(slots=True)
class CookieBundle:
    host: str
    cookies: dict[str, str]

    @property
    def header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())


def _clean_host(host: str) -> str:
    host = host.strip().lower()
    if "://" in host:
        host = urlparse(host).hostname or ""
    return host.split(":")[0].lstrip(".^")


def _uncaret(text: str) -> str:
    """Undo cmd.exe escaping from Edge's "Copy as cURL (cmd)".

    That flavour wraps every quote as ``^"``, escapes ``%`` and ``^`` the same
    way, and continues lines with a trailing ``^``. Left alone it defeats every
    quote-matching rule below, and the text then falls through to a permissive
    reading that turns header lines into cookies.
    """
    if "^" not in text:
        return text
    # A trailing caret is a line continuation, so the line joins to the next.
    text = re.sub(r"\^\s*\r?\n\s*", " ", text)
    return re.sub(r"\^(.)", r"\1", text)


def _is_cookie_name(name: str) -> bool:
    return bool(_COOKIE_NAME_RE.match(name)) and name.lower() not in _NOT_REALLY_COOKIES


def parse_cookie_header(text: str) -> dict[str, str]:
    """Read a raw ``Cookie:`` header value into name/value pairs.

    SimpleCookie is strict about characters that real session cookies contain,
    so a manual split is more reliable here than the standard parser.
    """
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]

    cookies: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if _is_cookie_name(name):
            cookies[name] = value.strip()
    return cookies


def looks_like_netscape(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("# netscape http cookie file") or head.startswith("# http cookie file")


def parse_netscape(text: str) -> dict[str, tuple[str, str]]:
    """Read a cookies.txt into ``{name: (domain, value)}``."""
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = fields[:7]
        if name:
            found[name] = (domain.lstrip("."), value)
    return found


def parse_pasted(text: str, *, fallback_host: str = "") -> CookieBundle:
    """Turn whatever the user pasted into a host and a set of cookies.

    Accepts any of the three "Copy as cURL" flavours, a bare ``Cookie:`` header,
    or a cookies.txt.
    """
    text = (text or "").strip()
    if not text:
        raise CookieError("Nothing was pasted.")

    if looks_like_netscape(text):
        entries = parse_netscape(text)
        if not entries:
            raise CookieError("That cookies.txt has no cookie lines in it.")
        host = _clean_host(fallback_host) or next(iter(entries.values()))[0]
        return CookieBundle(host, {name: value for name, (_, value) in entries.items()})

    text = _uncaret(text)
    host = ""
    match = _HOST_RE.search(text)
    if match:
        host = _clean_host(match.group(1))
    host = host or _clean_host(fallback_host)

    cookies: dict[str, str] = {}
    for _quote, header in _HEADER_RE.findall(text):
        name, _, value = header.partition(":")
        if name.strip().lower() == "cookie":
            cookies.update(parse_cookie_header(value))
    if not cookies:
        for _quote, value in _COOKIE_FLAG_RE.findall(text):
            cookies.update(parse_cookie_header(value))
    if not cookies and "-H" not in text and "curl" not in text.lower():
        # Only treat the whole paste as a header value when it plainly is one.
        # Doing it for a curl command that simply did not match reads every
        # header line as a cookie and produces a jar of nonsense.
        cookies = parse_cookie_header(text)

    if not cookies:
        raise CookieError(
            "No cookies were found in that text. In the Network tab, right-click the "
            "request and choose Copy, then \"Copy as cURL\" — any of the bash, cmd or "
            "PowerShell forms works. Make sure the request you pick is one that was "
            "sent while you were signed in."
        )
    if not host:
        raise CookieError(
            "Could not tell which site those cookies are for. Paste the whole cURL "
            "command, which includes the URL."
        )
    return CookieBundle(host, cookies)


def write_netscape(bundle: CookieBundle, path: Path, *, days_valid: int = 14) -> Path:
    """Write a cookie jar yt-dlp can read.

    The domain is stored with a leading dot so it also matches subdomains, which
    SharePoint needs: the page and the file it serves are frequently on
    different hosts within the same tenant.
    """
    if not bundle.cookies:
        raise CookieError("There are no cookies to save.")

    expiry = int(time.time()) + days_valid * 86_400
    lines = [NETSCAPE_HEADER]
    domain = f".{bundle.host}"
    for name, value in bundle.cookies.items():
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Best effort: a session cookie is sensitive, but on some filesystems
        # this is not supported and failing the import over it would be worse.
        pass
    return path


def jar_dir(state_dir: Path) -> Path:
    directory = state_dir / "cookies"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def jar_path(state_dir: Path, host: str) -> Path:
    safe = re.sub(r"[^a-z0-9.\-]+", "_", _clean_host(host)) or "site"
    return jar_dir(state_dir) / f"{safe}.txt"


def save_for_host(state_dir: Path, bundle: CookieBundle) -> Path:
    return write_netscape(bundle, jar_path(state_dir, bundle.host))


def find_for_url(state_dir: Path, url: str) -> Path | None:
    """The saved jar that covers ``url``, if there is one.

    Matches the closest parent domain, so cookies saved for
    ``contoso.sharepoint.com`` are also used for
    ``contoso-my.sharepoint.com``'s parent and any subdomain of the host.
    """
    host = _clean_host(urlparse(url).hostname or "")
    if not host:
        return None

    parts = host.split(".")
    for index in range(len(parts) - 1):
        candidate = jar_path(state_dir, ".".join(parts[index:]))
        if candidate.is_file():
            return candidate
    return None


def saved_hosts(state_dir: Path) -> list[dict[str, object]]:
    """What is currently stored, so the UI can show and clear it."""
    entries: list[dict[str, object]] = []
    for path in sorted(jar_dir(state_dir).glob("*.txt")):
        stat = path.stat()
        entries.append(
            {
                "host": path.stem.replace("_", "."),
                "saved_at": stat.st_mtime,
                "size_bytes": stat.st_size,
            }
        )
    return entries


def forget(state_dir: Path, host: str) -> bool:
    path = jar_path(state_dir, host)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed
