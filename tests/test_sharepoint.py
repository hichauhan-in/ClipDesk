"""SharePoint share links: working out what a page would have downloaded.

A share link is not a file, it is a viewer page, and every shape hides the file
somewhere different. Getting the rewrite wrong means downloading HTML and failing
much later inside ffmpeg, so the shapes are pinned down here.
"""

import httpx
import pytest

from clipdesk.ingest.links import LinkKind, classify
from clipdesk.ingest.sharepoint import (
    SharePointError,
    SharePointItem,
    download_url,
    file_url,
    folder_path,
    item_name,
    list_folder,
    looks_like_folder,
    share_marker,
    site_url,
)

TENANT = "https://contoso.sharepoint.com"
STREAM = (
    f"{TENANT}/teams/InShorts/_layouts/15/stream.aspx"
    "?id=%2Fteams%2FInShorts%2FRecordings%2FHow%5Fto%5Fdecrypt%2Emp4"
    "&referrer=StreamWebApp%2EWeb"
)
FOLDER_VIEW = (
    f"{TENANT}/teams/InShorts/Shared%20Documents/Forms/AllItems.aspx"
    "?id=%2Fteams%2FInShorts%2FRecordings"
)


# --- the player link ---------------------------------------------------------
# This is the link people actually paste: it is what the address bar shows while
# a Stream video is playing, and appending download=1 to it does nothing.
def test_a_player_link_becomes_a_download_link():
    assert "/_layouts/15/download.aspx?SourceUrl=" in download_url(STREAM)


def test_the_download_link_keeps_the_file_path():
    assert "How_to_decrypt.mp4" in download_url(STREAM).replace("%2F", "/").replace("%5F", "_")


def test_the_player_link_yields_the_real_filename():
    assert item_name(STREAM) == "How_to_decrypt.mp4"


def test_the_site_collection_is_found():
    # A REST call against the tenant root 404s; it has to name the owning site.
    assert site_url(STREAM) == f"{TENANT}/teams/InShorts"


@pytest.mark.parametrize("prefix", ["/sites/", "/teams/", "/personal/"])
def test_every_site_prefix_is_recognised(prefix):
    url = f"{TENANT}{prefix}Alpha/_layouts/15/stream.aspx?id=%2F{prefix.strip('/')}%2FAlpha%2Fa%2Emp4"

    assert site_url(url) == f"{TENANT}{prefix}Alpha"


# --- folders -----------------------------------------------------------------
def test_a_folder_share_is_recognised():
    assert looks_like_folder(f"{TENANT}/:f:/s/Team/EaBc123?e=xy") is True


def test_a_video_share_is_not_a_folder():
    assert looks_like_folder(f"{TENANT}/:v:/s/Team/EaBc123?e=xy") is False


def test_a_library_view_is_a_folder():
    assert looks_like_folder(FOLDER_VIEW) is True


def test_a_player_link_is_not_a_folder():
    assert looks_like_folder(STREAM) is False


def test_the_folder_path_is_extracted():
    assert folder_path(FOLDER_VIEW) == "/teams/InShorts/Recordings"


def test_a_library_root_falls_back_to_the_path():
    url = f"{TENANT}/teams/InShorts/Shared%20Documents/Forms/AllItems.aspx"

    assert folder_path(url) == "/teams/InShorts/Shared Documents"


@pytest.mark.parametrize(
    ("url", "marker"),
    [(f"{TENANT}/:v:/s/T/x", "v"), (f"{TENANT}/:f:/s/T/x", "f"), (f"{TENANT}/x/y", "")],
)
def test_the_share_marker_is_read(url, marker):
    assert share_marker(url) == marker


# --- what classification reports ---------------------------------------------
def test_a_folder_link_is_flagged_for_the_caller():
    link = classify(FOLDER_VIEW)

    assert link.kind is LinkKind.SHAREPOINT
    assert link.is_folder is True


def test_a_folder_is_never_handed_to_the_media_extractor():
    # There is no single video at that URL for it to find.
    assert classify(FOLDER_VIEW).prefers_ytdlp is False


def test_a_player_link_carries_its_filename_through_classification():
    assert classify(STREAM).suggested_name == "How_to_decrypt.mp4"


# --- round-tripping a picked file --------------------------------------------
# The picker hands its own generated URL back to the importer, so that URL has to
# survive a second pass through classification.
def test_a_generated_download_url_is_left_alone():
    item = SharePointItem(name="a.mp4", path="/teams/T/Recordings/a.mp4")
    generated = file_url(f"{TENANT}/teams/T/x", item)

    assert download_url(generated) == generated


def test_a_generated_download_url_still_reports_its_filename():
    item = SharePointItem(name="a.mp4", path="/teams/T/Recordings/a.mp4")

    assert item_name(file_url(f"{TENANT}/teams/T/x", item)) == "a.mp4"


def test_a_generated_download_url_is_not_mistaken_for_a_folder():
    item = SharePointItem(name="a.mp4", path="/teams/T/Recordings/a.mp4")

    assert looks_like_folder(file_url(f"{TENANT}/teams/T/x", item)) is False


# --- which files are offered -------------------------------------------------
@pytest.mark.parametrize("name", ["a.mp4", "b.MOV", "c.mkv", "d.m4a", "e.wav"])
def test_media_files_are_offered(name):
    assert SharePointItem(name=name, path=f"/x/{name}").is_media is True


@pytest.mark.parametrize("name", ["notes.docx", "sheet.xlsx", "readme", "a.mp4.txt"])
def test_other_files_are_not_offered(name):
    assert SharePointItem(name=name, path=f"/x/{name}").is_media is False


# --- the newer OneDrive domain ------------------------------------------------
# Microsoft is moving its apps onto cloud.microsoft, and a OneDrive share link now
# arrives from onedrive.cloud.microsoft. It matched no known host, so it fell
# through to a plain download and failed with nothing useful to say.
CLOUD_SHARE = "https://onedrive.cloud.microsoft/:v:/a@xr6s5q8x/S/IQB1basQeBTtQ6iNRGQ?e=HhMYD8"


def test_the_new_onedrive_host_is_recognised():
    assert classify(CLOUD_SHARE).kind is LinkKind.SHAREPOINT


def test_the_new_onedrive_host_is_known_to_need_a_session():
    assert classify(CLOUD_SHARE).needs_auth is True


def test_a_video_share_on_the_new_host_is_not_a_folder():
    assert classify(CLOUD_SHARE).is_folder is False


def test_a_folder_share_on_the_new_host_is_a_folder():
    url = CLOUD_SHARE.replace("/:v:/", "/:f:/")

    assert classify(url).is_folder is True


def test_the_new_host_gets_the_download_rewrite():
    assert "download=1" in classify(CLOUD_SHARE).download_url


def test_the_rewrite_keeps_the_existing_query():
    assert "e=HhMYD8" in classify(CLOUD_SHARE).download_url


def test_a_lookalike_host_is_not_treated_as_microsoft():
    # cloud.microsoft.evil.com must not match a suffix check.
    assert classify("https://cloud.microsoft.evil.com/a.mp4").kind is not LinkKind.SHAREPOINT


def test_the_cloud_viewer_site_is_found():
    url = (
        "https://onedrive.cloud.microsoft/a@viewer/_layouts/15/onedrive.aspx"
        "?id=%2Fa%40owner%2FDocuments%2FHimanshu"
    )

    assert site_url(url) == "https://onedrive.cloud.microsoft/a@viewer"


def test_a_cloud_file_download_uses_the_owner_site():
    item = SharePointItem(name="video.mp4", path="/a@owner/Documents/Himanshu/video.mp4")

    generated = file_url("https://onedrive.cloud.microsoft/a@viewer/x", item)

    assert generated.startswith("https://onedrive.cloud.microsoft/a@owner/")
    assert "SourceUrl=" in generated


def test_the_cloud_folder_uses_the_list_stream_api(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        request = httpx.Request("POST", url)
        if url.endswith("/_api/contextinfo"):
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "application/json"},
                json={"FormDigestValue": "digest"},
            )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            json={
                "ListData": {
                    "Row": [
                        {
                            "FSObjType": "0",
                            "FileLeafRef": "video.mp4",
                            "FileRef": "/a@owner/Documents/Himanshu/video.mp4",
                            "FileSizeDisplay": "1234",
                            "Modified": "2026-08-12T12:00:00Z",
                        },
                        {
                            "FSObjType": "1",
                            "FileLeafRef": "Subfolder",
                            "FileRef": "/a@owner/Documents/Himanshu/Subfolder",
                        },
                    ]
                }
            },
        )

    monkeypatch.setattr("clipdesk.ingest.sharepoint.httpx.post", fake_post)
    url = (
        "https://onedrive.cloud.microsoft/a@viewer/_layouts/15/onedrive.aspx?"
        "id=%2Fa%40owner%2FDocuments%2FHimanshu&"
        "listurl=https%3A%2F%2Fonedrive.cloud.microsoft%2Fa%40owner%2FDocuments"
    )

    items = list_folder(url, cookie_header="FedAuth=value")

    assert [(item.name, item.size_bytes) for item in items] == [("video.mp4", 1234)]
    assert calls[1][1]["params"]["listFullUrl"].startswith("'")
    assert calls[1][1]["headers"]["x-requestdigest"] == "digest"


def test_a_login_redirect_reports_an_expired_session():
    with pytest.raises(SharePointError, match="session has expired"):
        list_folder("https://login.microsoftonline.com/tenant/oauth2/authorize")
