"""SharePoint and OneDrive for Business: turning a share link into a file.

A share link is not a file. It is a page, and the page has a Download button.
Everything here is about finding what that button would have fetched.

Three shapes matter, and they are all recognisable from the URL alone:

* ``_layouts/15/stream.aspx?id=<path>`` -- the video player. ``id`` is already
  the server-relative path of the file, so the download endpoint takes it
  verbatim.
* ``.../Forms/AllItems.aspx?id=<path>`` and ``/:f:/`` -- a *folder*. There is no
  single file to fetch; the contents have to be listed so the user can choose.
* ``/:v:/``, ``/:b:/``, ``/:u:/`` and friends -- a short share link to one item.
  The letter says what kind, and ``?download=1`` is what the web UI itself uses.

Listing a folder uses SharePoint's REST API rather than scraping the page: it is
documented, stable, and returns exactly the fields needed. It requires the same
signed-in session as the download does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

#: The letter in a /:x:/ share link says what was shared.
SHARE_KIND = {
    "v": "video",
    "i": "image",
    "b": "document",
    "u": "file",
    "w": "word",
    "x": "excel",
    "p": "powerpoint",
    "f": "folder",
    "t": "text",
}

_SHARE_MARKER_RE = re.compile(r"/:([a-z]):/", re.IGNORECASE)

_MEDIA_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
}

#: Where a site collection ends and the library path begins. A tenant serves many
#: sites and the REST call has to be made against the right one.
_SITE_PREFIXES = ("/sites/", "/teams/", "/personal/")
_ONEDRIVE_CLOUD_HOST = "onedrive.cloud.microsoft"
_SIGN_IN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com")


@dataclass(slots=True)
class SharePointItem:
    name: str
    #: Server-relative path, which is what every SharePoint endpoint wants.
    path: str
    size_bytes: int = 0
    modified: str = ""

    @property
    def is_media(self) -> bool:
        dot = self.name.rfind(".")
        return dot > 0 and self.name[dot:].lower() in _MEDIA_SUFFIXES


class SharePointError(RuntimeError):
    """A folder could not be read, with a reason worth showing the user."""


def share_marker(url: str) -> str:
    match = _SHARE_MARKER_RE.search(urlparse(url).path)
    return match.group(1).lower() if match else ""


def looks_like_folder(url: str) -> bool:
    """Whether the link points at a folder rather than one file.

    Getting this wrong in the safe direction costs a redundant listing call;
    getting it wrong the other way means trying to download a web page.
    """
    if share_marker(url) == "f":
        return True
    path = urlparse(url).path.lower()
    if path.endswith("/forms/allitems.aspx"):
        return True
    query = parse_qs(urlparse(url).query)
    target = (query.get("id") or query.get("RootFolder") or [""])[0]
    if not target:
        return False
    # An id with no file extension on its last segment is a folder path.
    last = target.rstrip("/").rsplit("/", 1)[-1]
    return "." not in last


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def site_url(url: str) -> str:
    """The site collection a path belongs to, e.g. ``/teams/InShorts``.

    A REST call made against the tenant root instead of the owning site returns
    404, so this has to be right.
    """
    parsed = urlparse(url)
    path = parsed.path
    if parsed.hostname == _ONEDRIVE_CLOUD_HOST:
        first = path.strip("/").split("/", 1)[0]
        if first and "@" in first:
            return f"{_origin(url)}/{first}"
    query = parse_qs(parsed.query)
    target = (query.get("id") or query.get("RootFolder") or [""])[0]
    if target:
        path = unquote(target)

    lowered = path.lower()
    for prefix in _SITE_PREFIXES:
        start = lowered.find(prefix)
        if start >= 0:
            rest = path[start + len(prefix) :]
            name = rest.split("/", 1)[0]
            if name:
                return f"{_origin(url)}{path[start : start + len(prefix)]}{name}"
    return _origin(url)


def folder_path(url: str) -> str:
    """The server-relative folder a listing should be made against."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    target = (query.get("id") or query.get("RootFolder") or [""])[0]
    if target:
        return unquote(target).rstrip("/")
    path = unquote(parsed.path)
    if path.lower().endswith("/forms/allitems.aspx"):
        # .../Shared Documents/Forms/AllItems.aspx -> .../Shared Documents
        return path[: path.lower().rfind("/forms/allitems.aspx")]
    return path.rstrip("/")


def download_url(url: str) -> str:
    """The URL the Download button would have fetched.

    ``stream.aspx?id=`` and ``download.aspx?SourceUrl=`` take the same
    server-relative path, so the player link converts directly. Everything else
    falls back to ``?download=1``, which is what the web UI appends.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    target = (query.get("id") or [""])[0]

    if parsed.path.lower().endswith("/download.aspx") and query.get("SourceUrl"):
        return url  # already the download endpoint

    if target and parsed.path.lower().endswith(("/stream.aspx", "/onedrive.aspx")):
        path = unquote(target)
        site = site_url(url)
        return f"{site}/_layouts/15/download.aspx?SourceUrl={quote(path, safe='')}"

    separator = "&" if parsed.query else "?"
    if "download=1" in parsed.query:
        return url
    return f"{url}{separator}download=1"


def item_name(url: str) -> str:
    """The filename a link implies, when it carries one."""
    query = parse_qs(urlparse(url).query)
    # SourceUrl appears on links this module generates itself, which then go back
    # through classification when the user picks one out of a folder.
    target = (query.get("id") or query.get("SourceUrl") or [""])[0]
    candidate = unquote(target) if target else unquote(urlparse(url).path)
    name = candidate.rstrip("/").rsplit("/", 1)[-1]
    return name if "." in name else ""


# --- listing -----------------------------------------------------------------
_REST_HEADERS = {
    # nometadata keeps the payload small and the shape predictable.
    "accept": "application/json;odata=nometadata",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClipDesk/0.1",
}


def resolve(url: str, *, cookie_header: str = "", timeout: float = 30.0) -> str:
    """Follow a share link to wherever it actually lives.

    A ``/:f:/`` or ``/:v:/`` link, and anything on ``cloud.microsoft``, carries no
    library path at all -- it is an opaque token that redirects into the tenant.
    Nothing can be listed or downloaded until that redirect has been followed.
    Returns the original URL unchanged if it cannot be resolved, so the caller
    can carry on and fail with its own better message.
    """
    if not share_marker(url) and ".cloud.microsoft" not in urlparse(url).netloc:
        return url

    headers = dict(_REST_HEADERS)
    headers["accept"] = "text/html,application/xhtml+xml"
    if cookie_header:
        headers["cookie"] = cookie_header
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.RequestError:
        return url
    return str(response.url) or url


def list_folder(
    url: str, *, cookie_header: str = "", timeout: float = 30.0
) -> list[SharePointItem]:
    """List the files in a shared folder.

    Uses the documented REST endpoint rather than scraping the page, which keeps
    working when the web UI is redesigned.
    """
    host = (urlparse(url).hostname or "").lower()
    if any(host == known or host.endswith("." + known) for known in _SIGN_IN_HOSTS):
        raise SharePointError(
            "Your saved Microsoft session has expired. Sign in to Microsoft again, "
            "then retry the link."
        )
    if host == _ONEDRIVE_CLOUD_HOST and parse_qs(urlparse(url).query).get("listurl"):
        return _list_onedrive_folder(url, cookie_header=cookie_header, timeout=timeout)

    site = site_url(url)
    path = folder_path(url)
    if not path:
        raise SharePointError("That link does not say which folder it points at.")

    endpoint = (
        f"{site}/_api/web/GetFolderByServerRelativeUrl('{quote(path, safe='')}')"
        "/Files?$select=Name,ServerRelativeUrl,Length,TimeLastModified&$top=500"
    )
    headers = dict(_REST_HEADERS)
    if cookie_header:
        headers["cookie"] = cookie_header

    try:
        response = httpx.get(
            endpoint, headers=headers, timeout=timeout, follow_redirects=True
        )
    except httpx.RequestError as exc:
        raise SharePointError(f"Could not reach SharePoint: {exc}") from exc

    if response.status_code in {401, 403}:
        if host == _ONEDRIVE_CLOUD_HOST:
            raise SharePointError(
                "Your saved Microsoft session has expired. Sign in to Microsoft again, "
                "then retry the link."
            )
        raise SharePointError(
            "SharePoint refused to list that folder. Save a signed-in session for "
            "this site first, using \"Paste a signed-in session\"."
        )
    if response.status_code == 404:
        raise SharePointError(
            "SharePoint does not have a folder at that path. Open the link in your "
            "browser and copy the address from the address bar."
        )
    if response.status_code >= 400:
        raise SharePointError(f"SharePoint returned HTTP {response.status_code}.")

    content_type = (response.headers.get("content-type") or "").lower()
    if "json" not in content_type:
        # A sign-in page comes back as a perfectly valid 200 of HTML.
        if host == _ONEDRIVE_CLOUD_HOST:
            raise SharePointError(
                "Your saved Microsoft session has expired. Sign in to Microsoft again, "
                "then retry the link."
            )
        raise SharePointError(
            "SharePoint returned a sign-in page instead of the folder contents. "
            "Save a signed-in session for this site and try again."
        )

    payload = response.json()
    rows = payload.get("value") if isinstance(payload, dict) else None
    if rows is None:
        raise SharePointError("SharePoint returned a folder listing ClipDesk could not read.")

    items = [
        SharePointItem(
            name=str(row.get("Name") or ""),
            path=str(row.get("ServerRelativeUrl") or ""),
            size_bytes=int(row.get("Length") or 0),
            modified=str(row.get("TimeLastModified") or ""),
        )
        for row in rows
        if row.get("Name")
    ]
    return sorted(items, key=lambda item: item.name.lower())


def _list_onedrive_folder(
    url: str, *, cookie_header: str, timeout: float
) -> list[SharePointItem]:
    """List a folder shared through the modern OneDrive cloud front door."""
    site = site_url(url)
    path = folder_path(url)
    query = parse_qs(urlparse(url).query)
    list_url = unquote((query.get("listurl") or [""])[0])
    if not site or not path or not list_url:
        raise SharePointError("That OneDrive link is missing its folder information.")

    headers = dict(_REST_HEADERS)
    if cookie_header:
        headers["cookie"] = cookie_header
    try:
        context = httpx.post(
            f"{site}/_api/contextinfo", headers=headers, timeout=timeout
        )
    except httpx.RequestError as exc:
        raise SharePointError(f"Could not reach OneDrive: {exc}") from exc
    if context.status_code in {401, 403} or "json" not in (
        context.headers.get("content-type") or ""
    ).lower():
        raise SharePointError(
            "Your saved Microsoft session has expired. Sign in to Microsoft again, "
            "then retry the link."
        )
    if context.status_code >= 400:
        raise SharePointError(f"OneDrive returned HTTP {context.status_code}.")

    payload = context.json()
    digest = str(
        payload.get("FormDigestValue")
        or payload.get("d", {}).get("GetContextWebInformation", {}).get("FormDigestValue")
        or ""
    )
    if not digest:
        raise SharePointError("OneDrive did not return the request token needed to list files.")

    body = {
        "parameters": {
            "__metadata": {"type": "SP.RenderListDataParameters"},
            "RenderOptions": 5756679,
            "AllowMultipleValueFilterForTaxonomyFields": True,
            "AddRequiredFields": True,
            "RequireFolderColoringFields": True,
        }
    }
    stream_headers = {
        **headers,
        "content-type": "application/json;odata=verbose",
        "x-requestdigest": digest,
    }
    try:
        response = httpx.post(
            f"{site}/_api/SP.List.GetListDataAsStream",
            params={"RootFolder": path, "listFullUrl": f"'{list_url}'"},
            headers=stream_headers,
            json=body,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise SharePointError(f"Could not read the OneDrive folder: {exc}") from exc
    if response.status_code in {401, 403}:
        raise SharePointError(
            "Your saved Microsoft session has expired. Sign in to Microsoft again, "
            "then retry the link."
        )
    if response.status_code >= 400:
        raise SharePointError(f"OneDrive returned HTTP {response.status_code}.")

    payload = response.json()
    rows = (payload.get("ListData") or {}).get("Row") or []
    items: list[SharePointItem] = []
    for row in rows:
        if str(row.get("FSObjType") or "0") != "0":
            continue
        name = str(row.get("FileLeafRef") or "")
        path = str(row.get("FileRef") or "")
        if not name or not path:
            continue
        try:
            size = int(row.get("FileSizeDisplay") or row.get("File_x0020_Size") or 0)
        except (TypeError, ValueError):
            size = 0
        items.append(
            SharePointItem(
                name=name,
                path=path,
                size_bytes=size,
                modified=str(row.get("Modified") or ""),
            )
        )
    return sorted(items, key=lambda item: item.name.lower())


def file_url(origin_url: str, item: SharePointItem) -> str:
    """A direct download URL for one listed item."""
    if (urlparse(origin_url).hostname or "").lower() == _ONEDRIVE_CLOUD_HOST:
        owner = item.path.strip("/").split("/", 1)[0]
        if owner:
            return (
                f"{_origin(origin_url)}/{owner}/_layouts/15/download.aspx?"
                f"SourceUrl={quote(item.path, safe='')}"
            )
    site = site_url(origin_url)
    return f"{site}/_layouts/15/download.aspx?SourceUrl={quote(item.path, safe='')}"
