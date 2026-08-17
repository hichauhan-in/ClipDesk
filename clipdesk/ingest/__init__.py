"""Getting a recording into ClipDesk from a link rather than an upload."""

from clipdesk.ingest.fetch import (
    SUPPORTED_BROWSERS,
    FetchError,
    FetchResult,
    describe,
    fetch,
    ytdlp_available,
)
from clipdesk.ingest.links import Link, LinkKind, classify, is_probably_url

__all__ = [
    "SUPPORTED_BROWSERS",
    "FetchError",
    "FetchResult",
    "Link",
    "LinkKind",
    "classify",
    "describe",
    "fetch",
    "is_probably_url",
    "ytdlp_available",
]
