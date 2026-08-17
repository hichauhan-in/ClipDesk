"""Completion checks for the browser-driven Microsoft sign-in flow."""

from clipdesk.ingest.browsersignin import (
    _has_arrived,
    recalled_resolution,
    remember_resolution,
)


def test_a_login_page_has_not_arrived():
    assert _has_arrived("https://login.microsoftonline.com/tenant/oauth2/authorize") is False


def test_an_empty_onedrive_shared_route_has_not_arrived():
    assert _has_arrived("https://onedrive.cloud.microsoft/shared") is False


def test_an_opaque_onedrive_share_has_not_arrived():
    assert _has_arrived("https://onedrive.cloud.microsoft/:f:/a@tenant/S/token?e=abc") is False


def test_a_hydrated_onedrive_shared_route_has_arrived():
    url = (
        "https://onedrive.cloud.microsoft/shared?"
        "id=%2Fpersonal%2Fowner%2FDocuments%2FRecordings&"
        "listurl=https%3A%2F%2Ftenant-my.sharepoint.com%2Fpersonal%2Fowner%2FDocuments"
    )

    assert _has_arrived(url) is True

def test_an_opaque_sharepoint_share_has_not_arrived():
    assert _has_arrived("https://contoso.sharepoint.com/:f:/t/Team/opaque?e=abc") is False




def test_an_ordinary_tenant_destination_has_arrived():
    assert _has_arrived("https://tenant.sharepoint.com/sites/Team/video.mp4") is True


def test_a_signed_in_link_resolution_round_trips(tmp_path):
    original = "https://onedrive.cloud.microsoft/:f:/a@tenant/S/opaque"
    resolved = "https://onedrive.cloud.microsoft/a@owner/_layouts/15/onedrive.aspx?id=folder"

    remember_resolution(tmp_path, original, resolved)

    assert recalled_resolution(tmp_path, original) == resolved
    assert original not in (tmp_path / "link-resolutions.json").read_text(encoding="utf-8")


def test_optional_tracking_parameters_do_not_change_a_share_resolution(tmp_path):
    long_url = (
        "https://onedrive.cloud.microsoft/:f:/a@tenant/S/opaque?"
        "email=user%40example.com&e=abc&xsdata=tracking"
    )
    short_url = "https://onedrive.cloud.microsoft/:f:/a@tenant/S/opaque?e=abc"
    resolved = "https://onedrive.cloud.microsoft/shared?id=folder&listurl=library"

    remember_resolution(tmp_path, long_url, resolved)

    assert recalled_resolution(tmp_path, short_url) == resolved


def test_query_parameters_remain_significant_for_non_share_urls(tmp_path):
    first = "https://example.com/download?id=one"
    second = "https://example.com/download?id=two"

    remember_resolution(tmp_path, first, "https://example.com/file.mp4")

    assert recalled_resolution(tmp_path, second) is None


def test_an_old_link_resolution_is_ignored(tmp_path, monkeypatch):
    original = "https://onedrive.cloud.microsoft/:f:/a@tenant/S/opaque"
    resolved = "https://onedrive.cloud.microsoft/shared?id=folder"
    monkeypatch.setattr("clipdesk.ingest.browsersignin.time.time", lambda: 100.0)
    remember_resolution(tmp_path, original, resolved)
    monkeypatch.setattr("clipdesk.ingest.browsersignin.time.time", lambda: 100.0 + 15 * 86_400)

    assert recalled_resolution(tmp_path, original) is None