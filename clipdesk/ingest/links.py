"""Recognising share links and turning them into something downloadable.

Corporate reality: nobody downloads a 2 GB recording and re-uploads it. They
paste the link they were sent. Each service hides the actual file behind a
different viewer URL, so this module's job is to work out which service a link
belongs to and rewrite it into a direct download where that is possible.

Where it is not possible — anything behind a sign-in — the classification is
still useful, because it lets the error message say *why* and what to do instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, quote, urlparse, urlunparse


class LinkKind(str, Enum):
    DIRECT = "direct"
    ONEDRIVE = "onedrive"
    SHAREPOINT = "sharepoint"
    GOOGLE_DRIVE = "google_drive"
    YOUTUBE = "youtube"
    STREAMING_SITE = "streaming_site"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Link:
    kind: LinkKind
    #: The URL to actually fetch, which may be a rewritten form of the original.
    download_url: str
    original: str
    #: True when the content almost certainly needs a signed-in session.
    needs_auth: bool = False
    #: Filename hinted by the URL, when there is one.
    suggested_name: str = ""
    note: str = ""
    #: True when the link points at a folder, so there is a choice to make before
    #: anything can be downloaded.
    is_folder: bool = False

    @property
    def prefers_ytdlp(self) -> bool:
        """Sites where a media extractor is the only sensible way in."""
        if self.is_folder:
            return False
        return self.kind in {LinkKind.YOUTUBE, LinkKind.STREAMING_SITE} or (
            self.kind in {LinkKind.SHAREPOINT, LinkKind.ONEDRIVE} and self.needs_auth
        )


_GOOGLE_FILE_ID_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})")
_VIDEO_SUFFIX_RE = re.compile(
    r"\.(mp4|mov|mkv|webm|avi|m4v|wmv|flv|mpe?g|ts|m2ts|mp3|wav|m4a|aac|flac|ogg)$",
    re.IGNORECASE,
)

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}
_GOOGLE_DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}
#: Consumer OneDrive.
_ONEDRIVE_HOSTS = {"1drv.ms", "onedrive.live.com", "storage.live.com"}
#: Microsoft is moving its apps onto cloud.microsoft, and OneDrive share links now
#: come from onedrive.cloud.microsoft. They use the same /:x:/ share format as
#: SharePoint and redirect into the tenant, so they are handled the same way.
_MICROSOFT_CLOUD_SUFFIX = ".cloud.microsoft"

# Sites a media extractor handles well and a plain download does not.
_STREAMING_HINTS = (
    "vimeo.com",
    "dailymotion.com",
    "loom.com",
    "wistia.com",
    "brightcove",
    "kaltura",
    "panopto",
    "echo360",
    "zoom.us",
    "teams.microsoft.com",
)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _filename_from_path(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    return name if _VIDEO_SUFFIX_RE.search(name) else ""


def is_probably_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def classify(raw: str) -> Link:
    """Work out what kind of link this is and how to fetch it."""
    url = raw.strip()
    if not is_probably_url(url):
        raise ValueError("That does not look like an http(s) link.")

    host = _hostname(url)

    if host in _YOUTUBE_HOSTS:
        return Link(LinkKind.YOUTUBE, url, url, note="Video link")

    if host in _GOOGLE_DRIVE_HOSTS:
        return _google_drive(url)

    if host in _ONEDRIVE_HOSTS:
        return _onedrive(url)

    # SharePoint and the service formerly known as Stream both live on
    # <tenant>.sharepoint.com; Stream videos are just files in a document library.
    # onedrive.cloud.microsoft serves the same share links from a newer domain.
    if host.endswith((".sharepoint.com", ".sharepoint.us", _MICROSOFT_CLOUD_SUFFIX)):
        return _sharepoint(url)

    if any(hint in host for hint in _STREAMING_HINTS):
        return Link(
            LinkKind.STREAMING_SITE,
            url,
            url,
            needs_auth=False,
            note=f"{host} — handled by the media extractor",
        )

    # A plain link to a media file is the easy case.
    name = _filename_from_path(url)
    if name:
        return Link(LinkKind.DIRECT, url, url, suggested_name=name)

    return Link(
        LinkKind.UNKNOWN,
        url,
        url,
        note="Unrecognised link — it will be tried as a direct download.",
    )


def _google_drive(url: str) -> Link:
    """Rewrite a Drive viewer link into the download endpoint."""
    match = _GOOGLE_FILE_ID_RE.search(url)
    file_id = match.group(1) if match else None
    if not file_id:
        query = parse_qs(urlparse(url).query)
        file_id = (query.get("id") or [""])[0] or None

    if not file_id:
        return Link(
            LinkKind.GOOGLE_DRIVE,
            url,
            url,
            needs_auth=True,
            note="Could not find a file id in that Drive link. Use the "
            "'Anyone with the link' share URL for the file itself.",
        )

    return Link(
        LinkKind.GOOGLE_DRIVE,
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download",
        url,
        note="Google Drive",
    )


def _onedrive(url: str) -> Link:
    """OneDrive personal links download once `download=1` is set."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["download"] = ["1"]
    rebuilt = urlunparse(
        parsed._replace(
            query="&".join(f"{key}={quote(values[0], safe='')}" for key, values in query.items())
        )
    )
    return Link(
        LinkKind.ONEDRIVE,
        rebuilt,
        url,
        needs_auth=True,
        note="OneDrive — works for 'Anyone with the link' shares. A link "
        "restricted to your organisation needs the browser-session option.",
    )


def _sharepoint(url: str) -> Link:
    """SharePoint, OneDrive for Business, and the service formerly called Stream.

    A share link is a page, not a file, and the rewrite depends on which page it
    is. ``sharepoint.py`` knows the shapes; this just reports what it decided.
    """
    from clipdesk.ingest import sharepoint

    if sharepoint.looks_like_folder(url):
        return Link(
            LinkKind.SHAREPOINT,
            url,
            url,
            needs_auth=True,
            is_folder=True,
            note="A SharePoint folder — ClipDesk will list what is in it so you can choose.",
        )

    name = sharepoint.item_name(url) or _filename_from_path(url)
    return Link(
        LinkKind.SHAREPOINT,
        sharepoint.download_url(url),
        url,
        needs_auth=True,
        suggested_name=name,
        note="SharePoint / Stream — tenant content needs a signed-in session.",
    )
