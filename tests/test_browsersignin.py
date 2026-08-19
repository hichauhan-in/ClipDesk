"""Completion checks for the browser-driven Microsoft sign-in flow."""

import pytest

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

def test_the_port_is_read_from_the_browsers_own_file(tmp_path):
    """Chromium writes this; it is how a browser we launched earlier is found."""
    from clipdesk.ingest.browsersignin import _active_debug_port

    (tmp_path / "DevToolsActivePort").write_text("54321\n/devtools/browser/abc\n", encoding="utf-8")

    assert _active_debug_port(tmp_path) == 54321
    assert _active_debug_port(tmp_path / "missing") == 0


def test_an_already_open_window_is_reused_rather_than_relaunched(tmp_path, monkeypatch):
    """A second launch on one profile is handed to the first browser and exits.

    The port on the command line then never opens, so ClipDesk reported that it
    could not talk to a browser that had in fact opened the page.
    """
    from clipdesk.ingest import browsersignin as mod

    monkeypatch.setattr(mod, "find_browser", lambda: ("Microsoft Edge", "msedge.exe"))
    monkeypatch.setattr(mod, "_active_debug_port", lambda _profile: 9222)
    monkeypatch.setattr(mod, "_live_debugger", lambda _port: True)

    opened = []
    monkeypatch.setattr(mod, "_open_tab", lambda port, url: opened.append((port, url)))

    def _no_launch(*_args, **_kwargs):
        raise AssertionError("a second browser must not be launched")

    monkeypatch.setattr(mod.subprocess, "Popen", _no_launch)
    monkeypatch.setattr(
        mod, "_wait_for_debugger", lambda port, _deadline, _profile: (port, {"ok": True})
    )
    monkeypatch.setattr(mod, "_page_targets", lambda _port: [])

    with pytest.raises(mod.SignInError, match="Timed out"):
        mod.sign_in(
            "https://contoso-my.sharepoint.com/:v:/g/personal/x/abc",
            tmp_path,
            lambda _f, _m: None,
            timeout_s=0.2,
        )

    assert opened and opened[0][0] == 9222


def test_a_dead_launcher_does_not_mean_a_closed_window(tmp_path, monkeypatch):
    """Edge hands the URL to an instance already on this profile, then exits.

    The window stays open and signed in, but the process object reports exited,
    which used to abort a sign-in that had in fact already succeeded.
    """
    from clipdesk.ingest import browsersignin as mod

    monkeypatch.setattr(mod, "find_browser", lambda: ("Microsoft Edge", "msedge.exe"))
    monkeypatch.setattr(mod, "_active_debug_port", lambda _profile: 0)
    monkeypatch.setattr(mod, "_live_debugger", lambda _port: True)
    monkeypatch.setattr(
        mod, "_wait_for_debugger", lambda port, _d, _p: (port, {"webSocketDebuggerUrl": "ws://x"})
    )
    monkeypatch.setattr(mod, "_bring_to_foreground", lambda _pid: True)

    class _FakeBrowser:
        pid = 1234

        def poll(self):
            return 0  # the launcher is gone; the browser is not

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: _FakeBrowser())

    arrived = "https://onedrive.cloud.microsoft/a@t/_layouts/15/stream.aspx?id=%2Fx"
    monkeypatch.setattr(mod, "_page_targets", lambda _port: [{"id": "1", "url": arrived}])
    monkeypatch.setattr(mod, "_read_cookies", lambda _ws: None)
    monkeypatch.setattr(mod.asyncio, "run", lambda _coro: [{"name": "FedAuth", "value": "v",
                                                            "domain": "onedrive.cloud.microsoft"}])

    result = mod.sign_in(arrived, tmp_path, lambda _f, _m: None, timeout_s=5)

    assert result.final_url == arrived


def test_a_settled_page_is_accepted_even_if_its_address_is_unfamiliar(tmp_path, monkeypatch):
    """OneDrive keeps changing what a share resolves to.

    A folder link landed on an address no rule anticipated, so the wait never
    ended and the job was eventually reported as failed. Once the tab has been
    off the sign-in pages for a while, the session cookie decides instead.
    """
    from clipdesk.ingest import browsersignin as mod

    monkeypatch.setattr(mod, "_SETTLE_GRACE_S", 0.0)
    monkeypatch.setattr(mod, "find_browser", lambda: ("Microsoft Edge", "msedge.exe"))
    monkeypatch.setattr(mod, "_active_debug_port", lambda _profile: 0)
    monkeypatch.setattr(mod, "_live_debugger", lambda _port: True)
    monkeypatch.setattr(
        mod, "_wait_for_debugger", lambda port, _d, _p: (port, {"webSocketDebuggerUrl": "ws://x"})
    )
    monkeypatch.setattr(mod, "_bring_to_foreground", lambda _pid: True)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: _FakeBrowser())

    # The opaque folder form, which _has_arrived deliberately refuses.
    landed = "https://onedrive.cloud.microsoft/:f:/a@dq3rs9k2/S/IgCsz123?e=34NGVp"
    assert mod._has_arrived(landed) is False

    monkeypatch.setattr(mod, "_page_targets", lambda _port: [{"id": "1", "url": landed}])
    monkeypatch.setattr(
        mod.asyncio,
        "run",
        lambda _coro: [
            {"name": "FedAuth", "value": "v", "domain": "dq3rs9k2-my.sharepoint.com"},
        ],
    )
    monkeypatch.setattr(mod, "_read_cookies", lambda _ws: None)

    result = mod.sign_in(landed, tmp_path, lambda _f, _m: None, timeout_s=5)

    # The tenant host is taken from the cookies, since the address never names it.
    assert "dq3rs9k2-my.sharepoint.com" in result.by_host
