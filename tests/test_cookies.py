"""Getting a signed-in session out of the browser.

Reading Edge's or Chrome's cookie store stopped working: Chromium 127 wraps the
key so only the browser can unwrap it, and it fails with a DPAPI error even when
the browser is closed. So the session has to come from the user instead, and
these cover the shapes they can realistically paste.
"""

import pytest

from clipdesk.ingest.cookies import (
    CookieError,
    find_for_url,
    forget,
    parse_netscape,
    parse_pasted,
    save_for_host,
    saved_hosts,
    write_netscape,
)

CURL_BASH = (
    "curl 'https://contoso.sharepoint.com/sites/Team/video.mp4' \\\n"
    "  -H 'accept: */*' \\\n"
    "  -H 'cookie: FedAuth=abc123; rtFa=xyz789; SIMI=zzz' \\\n"
    "  -H 'user-agent: Mozilla/5.0'"
)

CURL_POWERSHELL = (
    'curl "https://contoso.sharepoint.com/sites/Team/video.mp4" '
    '-H "cookie: FedAuth=abc123; rtFa=xyz789"'
)

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".contoso.sharepoint.com\tTRUE\t/\tTRUE\t1799999999\tFedAuth\tabc123\n"
    ".contoso.sharepoint.com\tTRUE\t/\tTRUE\t1799999999\trtFa\txyz789\n"
)


# --- what the user can paste -------------------------------------------------
def test_a_bash_curl_gives_the_host_and_the_cookies():
    bundle = parse_pasted(CURL_BASH)

    assert bundle.host == "contoso.sharepoint.com"
    assert bundle.cookies["FedAuth"] == "abc123"
    assert bundle.cookies["rtFa"] == "xyz789"


def test_a_powershell_curl_works_too():
    # Windows browsers emit double quotes, which is a different shape entirely.
    bundle = parse_pasted(CURL_POWERSHELL)

    assert bundle.host == "contoso.sharepoint.com"
    assert len(bundle.cookies) == 2


def test_a_bare_cookie_header_is_accepted_with_the_link_for_context():
    bundle = parse_pasted(
        "Cookie: FedAuth=abc123; rtFa=xyz789",
        fallback_host="https://contoso.sharepoint.com/sites/Team/video.mp4",
    )

    assert bundle.host == "contoso.sharepoint.com"
    assert bundle.cookies["FedAuth"] == "abc123"


def test_a_cookies_txt_export_is_accepted():
    bundle = parse_pasted(NETSCAPE)

    assert bundle.host == "contoso.sharepoint.com"
    assert bundle.cookies["rtFa"] == "xyz789"


def test_a_cookie_value_containing_an_equals_sign_survives():
    bundle = parse_pasted(
        "cookie: token=abc==; other=1",
        fallback_host="https://example.sharepoint.com/a.mp4",
    )

    assert bundle.cookies["token"] == "abc=="


def test_nothing_pasted_is_refused():
    with pytest.raises(CookieError):
        parse_pasted("   ")


def test_text_without_cookies_is_refused():
    with pytest.raises(CookieError, match="No cookies"):
        parse_pasted("curl 'https://contoso.sharepoint.com/a.mp4' -H 'accept: */*'")


def test_cookies_without_a_host_are_refused():
    # Saving them under the wrong site would send someone's session elsewhere.
    with pytest.raises(CookieError, match="which site"):
        parse_pasted("Cookie: FedAuth=abc123")


# --- storage -----------------------------------------------------------------
def test_a_saved_jar_can_be_read_back(tmp_path):
    bundle = parse_pasted(CURL_BASH)
    path = save_for_host(tmp_path, bundle)

    assert parse_netscape(path.read_text(encoding="utf-8"))["FedAuth"][1] == "abc123"


def test_the_jar_covers_subdomains(tmp_path):
    # SharePoint serves the page and the file from different hosts in the same
    # tenant, so a cookie saved for one has to apply to the other.
    save_for_host(tmp_path, parse_pasted(CURL_BASH))
    text = (tmp_path / "cookies" / "contoso.sharepoint.com.txt").read_text(encoding="utf-8")

    assert text.splitlines()[2].startswith(".contoso.sharepoint.com\t")


def test_a_saved_session_is_found_for_a_matching_link(tmp_path):
    save_for_host(tmp_path, parse_pasted(CURL_BASH))

    found = find_for_url(tmp_path, "https://contoso.sharepoint.com/sites/Other/x.mp4")

    assert found is not None


def test_a_saved_session_is_found_for_a_sibling_host(tmp_path):
    save_for_host(tmp_path, parse_pasted(CURL_BASH))

    found = find_for_url(tmp_path, "https://media.contoso.sharepoint.com/x.mp4")

    assert found is not None


def test_an_unrelated_link_finds_nothing(tmp_path):
    save_for_host(tmp_path, parse_pasted(CURL_BASH))

    assert find_for_url(tmp_path, "https://fabrikam.example.com/x.mp4") is None


def test_saved_sessions_are_listed(tmp_path):
    save_for_host(tmp_path, parse_pasted(CURL_BASH))

    assert [entry["host"] for entry in saved_hosts(tmp_path)] == ["contoso.sharepoint.com"]


def test_a_session_can_be_forgotten(tmp_path):
    save_for_host(tmp_path, parse_pasted(CURL_BASH))

    assert forget(tmp_path, "contoso.sharepoint.com") is True
    assert saved_hosts(tmp_path) == []


def test_forgetting_something_absent_is_not_an_error(tmp_path):
    assert forget(tmp_path, "nothing.example.com") is False


def test_an_empty_bundle_is_refused(tmp_path):
    from clipdesk.ingest.cookies import CookieBundle

    with pytest.raises(CookieError):
        write_netscape(CookieBundle("example.com", {}), tmp_path / "x.txt")


# --- the cmd flavour ---------------------------------------------------------
# Edge on Windows offers three "Copy as cURL" forms. The cmd one escapes every
# quote with a caret, which defeats quote matching entirely; a real paste of it
# produced a cookie jar full of header fragments that the downloader then
# rejected line by line.
CURL_CMD = (
    'curl ^"https://contoso.sharepoint.com/teams/T/_layouts/15/stream.aspx?id=x^" ^\n'
    '  -H ^"Referer: https://contoso.sharepoint.com/teams/T/SitePages/a.aspx?source=^%^3A^" ^\n'
    '  -H ^"Upgrade-Insecure-Requests: 1^" ^\n'
    '  -H ^"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0^" ^\n'
    '  -H ^"cookie: FedAuth=77u/PD94bWw; rtFa=abcXYZ==; EdgeAccessCookie=q1w2e3^" ^\n'
    '  -H ^"sec-ch-ua: ^\\^"Not A?Brand^\\^";v=^\\^"8^\\^"^"'
)


def test_the_caret_escaped_cmd_form_is_parsed():
    bundle = parse_pasted(CURL_CMD)

    assert bundle.host == "contoso.sharepoint.com"
    assert bundle.cookies["FedAuth"] == "77u/PD94bWw"


def test_the_cmd_form_yields_only_real_cookies():
    # The failure this guards was a jar containing "-H ^\"Referer" and friends.
    assert set(parse_pasted(CURL_CMD).cookies) == {"FedAuth", "rtFa", "EdgeAccessCookie"}


def test_a_percent_escape_does_not_leak_a_caret_into_the_host():
    assert "^" not in parse_pasted(CURL_CMD).host


# --- refusing junk -----------------------------------------------------------
def test_a_curl_command_without_cookies_is_refused_not_scraped():
    with pytest.raises(CookieError, match="No cookies"):
        parse_pasted('curl ^"https://x.sharepoint.com/a.mp4^" ^\n  -H ^"Accept: */*^"')


def test_a_header_name_is_never_mistaken_for_a_cookie():
    with pytest.raises(CookieError):
        parse_pasted("curl 'https://x.sharepoint.com/a.mp4' -H 'Referer: https://x/y?a=b'")


@pytest.mark.parametrize("junk", ["Referer: https://x/y=1", "sec-ch-ua: v=8", "  -H x=1"])
def test_names_that_are_not_tokens_are_dropped(junk):
    from clipdesk.ingest.cookies import parse_cookie_header

    assert parse_cookie_header(junk) == {}


def test_cookie_attributes_are_not_treated_as_cookies():
    from clipdesk.ingest.cookies import parse_cookie_header

    parsed = parse_cookie_header("FedAuth=abc; Path=/; Secure; SameSite=None")

    assert set(parsed) == {"FedAuth"}
