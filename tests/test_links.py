"""Link classification: the rewriting rules that turn a share URL into a
download, and the guard that stops a sign-in page being saved as a video."""

from importlib import import_module

import httpx
import pytest

from clipdesk.ingest.links import LinkKind, classify, is_probably_url


def kind(url: str) -> LinkKind:
    return classify(url).kind


# --- validation --------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "not a url", "ftp://host/file.mp4", "/local/path.mp4"])
def test_non_http_input_is_rejected(value):
    assert is_probably_url(value) is False
    with pytest.raises(ValueError):
        classify(value)


def test_http_and_https_are_accepted():
    assert is_probably_url("http://example.com/a.mp4")
    assert is_probably_url("https://example.com/a.mp4")


# --- recognition -------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=abc",
    ],
)
def test_youtube_is_recognised(url):
    assert kind(url) is LinkKind.YOUTUBE
    assert classify(url).prefers_ytdlp is True


def test_a_direct_media_url_is_direct():
    link = classify("https://example.com/videos/standup.mp4")
    assert link.kind is LinkKind.DIRECT
    assert link.suggested_name == "standup.mp4"
    assert link.prefers_ytdlp is False


def test_a_page_url_is_unknown_rather_than_direct():
    assert kind("https://example.com/watch/some-page") is LinkKind.UNKNOWN


def test_sharepoint_and_stream_are_recognised():
    assert kind("https://contoso.sharepoint.com/sites/Team/x.mp4") is LinkKind.SHAREPOINT
    assert kind("https://contoso-my.sharepoint.com/:v:/g/personal/u/Ea") is LinkKind.SHAREPOINT


def test_onedrive_is_recognised():
    assert kind("https://1drv.ms/v/s!AbCdEf") is LinkKind.ONEDRIVE
    assert kind("https://onedrive.live.com/?id=1234") is LinkKind.ONEDRIVE


def test_google_drive_is_recognised():
    assert kind("https://drive.google.com/file/d/1AbCdEfGhIjKl/view") is LinkKind.GOOGLE_DRIVE


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "https://acme.panopto.com/Panopto/Pages/Viewer.aspx?id=x",
        "https://www.loom.com/share/abc",
    ],
)
def test_streaming_sites_route_to_the_extractor(url):
    assert classify(url).prefers_ytdlp is True


# --- rewriting ---------------------------------------------------------------
def test_google_drive_viewer_link_becomes_a_download_link():
    link = classify("https://drive.google.com/file/d/1AbCdEfGhIjKl/view?usp=sharing")
    assert "1AbCdEfGhIjKl" in link.download_url
    assert "export=download" in link.download_url


def test_google_drive_open_link_uses_the_id_parameter():
    link = classify("https://drive.google.com/open?id=9ZyXwVu")
    assert "9ZyXwVu" in link.download_url


def test_a_drive_link_with_no_file_id_is_flagged():
    link = classify("https://drive.google.com/drive/folders/abc")
    assert link.needs_auth is True
    assert "file id" in link.note


def test_sharepoint_gets_the_download_parameter():
    link = classify("https://contoso.sharepoint.com/sites/T/Recordings/standup.mp4")
    assert "download=1" in link.download_url
    assert link.needs_auth is True


def test_sharepoint_keeps_existing_query_parameters():
    link = classify("https://contoso.sharepoint.com/:v:/g/personal/u/Ea?e=AbC")
    assert "e=AbC" in link.download_url
    assert "download=1" in link.download_url


def test_onedrive_gets_the_download_parameter():
    assert "download=1" in classify("https://1drv.ms/v/s!AbCdEf").download_url


def test_the_original_url_is_always_preserved():
    original = "https://drive.google.com/file/d/1AbCdEfGhIjKl/view"
    assert classify(original).original == original


# --- auth expectations -------------------------------------------------------
def test_tenant_content_is_marked_as_needing_a_session():
    # This is what drives the "use my browser session" prompt in the UI.
    assert classify("https://contoso.sharepoint.com/sites/T/x.mp4").needs_auth is True
    assert classify("https://1drv.ms/v/s!Ab").needs_auth is True


def test_a_public_file_is_not_marked_as_needing_a_session():
    assert classify("https://example.com/a.mp4").needs_auth is False
    assert classify("https://youtu.be/abc").needs_auth is False


def test_authenticated_share_links_prefer_the_extractor():
    # A signed-in SharePoint video is delivered as a manifest, not a file, so a
    # plain GET cannot work even with valid cookies.
    assert classify("https://contoso.sharepoint.com/:v:/g/personal/u/Ea").prefers_ytdlp is True


# --- download cap -------------------------------------------------------------
# The limit existed in config but was never applied, so a bad link could fill the
# disk while the setting implied otherwise.
class _FakeResponse:
    def __init__(self, body, headers):
        self.status_code = 200
        self.headers = headers
        self._body = body

    def raise_for_status(self):
        return None

    def iter_bytes(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _stream_returning(body, headers):
    def stream(*_args, **_kwargs):
        return _FakeResponse(body, headers)

    return stream


def test_a_declared_size_over_the_cap_is_refused(tmp_path, monkeypatch):
    fetch_module = import_module("clipdesk.ingest.fetch")

    body = b"v" * 5_000_000
    monkeypatch.setattr(
        httpx,
        "stream",
        _stream_returning(body, {"content-type": "video/mp4", "content-length": str(len(body))}),
    )

    with pytest.raises(fetch_module.FetchError, match="limit"):
        fetch_module.fetch_direct(classify("https://example.invalid/a.mp4"), tmp_path, lambda *_: None, max_mb=1)


def test_an_undeclared_size_over_the_cap_is_still_refused(tmp_path, monkeypatch):
    # No content-length, so the cap has to be enforced while streaming or it is
    # bypassed simply by omitting the header.
    fetch_module = import_module("clipdesk.ingest.fetch")

    body = b"v" * 5_000_000
    monkeypatch.setattr(
        httpx, "stream", _stream_returning(body, {"content-type": "video/mp4"})
    )

    with pytest.raises(fetch_module.FetchError, match="limit"):
        fetch_module.fetch_direct(classify("https://example.invalid/a.mp4"), tmp_path, lambda *_: None, max_mb=1)


def test_nothing_is_left_on_disk_when_the_cap_is_hit(tmp_path, monkeypatch):
    fetch_module = import_module("clipdesk.ingest.fetch")

    body = b"v" * 5_000_000
    monkeypatch.setattr(
        httpx, "stream", _stream_returning(body, {"content-type": "video/mp4"})
    )

    with pytest.raises(fetch_module.FetchError):
        fetch_module.fetch_direct(classify("https://example.invalid/a.mp4"), tmp_path, lambda *_: None, max_mb=1)

    assert list(tmp_path.iterdir()) == []


def test_a_file_under_the_cap_is_kept(tmp_path, monkeypatch):
    fetch_module = import_module("clipdesk.ingest.fetch")

    body = b"v" * 500_000
    monkeypatch.setattr(
        httpx,
        "stream",
        _stream_returning(body, {"content-type": "video/mp4", "content-length": str(len(body))}),
    )

    result = fetch_module.fetch_direct(
        classify("https://example.invalid/a.mp4"), tmp_path, lambda *_: None, max_mb=10
    )

    assert result.bytes_written == 500_000
    assert result.path.is_file()


def test_a_cap_of_zero_means_no_limit(tmp_path, monkeypatch):
    fetch_module = import_module("clipdesk.ingest.fetch")

    body = b"v" * 500_000
    monkeypatch.setattr(
        httpx, "stream", _stream_returning(body, {"content-type": "video/mp4"})
    )

    result = fetch_module.fetch_direct(
        classify("https://example.invalid/a.mp4"), tmp_path, lambda *_: None, max_mb=0
    )

    assert result.bytes_written == 500_000


