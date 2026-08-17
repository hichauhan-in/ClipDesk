"""Picking files out of a synced OneDrive folder.

This is the way in that needs no authentication: the OneDrive client is already
signed in and syncing, so a file it has is just a file. The risk is the opposite
one -- the browse API takes a path over HTTP, and without containment it would be
a read-anything endpoint.
"""

import os
import sys

import pytest

from clipdesk.ingest.localsource import (
    BrowseError,
    Entry,
    Root,
    browse,
    cloud_roots,
    copy_into,
    find_root,
    resolve_within,
)


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "OneDrive - Contoso"
    (base / "Recordings" / "Old").mkdir(parents=True)
    (base / "Recordings" / "standup.mp4").write_bytes(b"v" * 2048)
    (base / "Recordings" / "notes.docx").write_bytes(b"x" * 10)
    (base / "Recordings" / "Old" / "archive.mkv").write_bytes(b"v" * 512)
    (base / ".hidden").mkdir()
    (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")
    return Root("onedrive-contoso", "OneDrive - Contoso", base)


# --- containment -------------------------------------------------------------
# Everything below arrives from the browser, so this is the boundary that stops
# the picker becoming an arbitrary file reader.
@pytest.mark.parametrize(
    "escape",
    ["..", "../..", r"..\..", "Recordings/../..", "sub/../../../etc", r"..\secret.txt"],
)
def test_paths_that_climb_out_are_refused(root, escape):
    with pytest.raises(BrowseError):
        resolve_within(root, escape)


@pytest.mark.skipif(sys.platform != "win32", reason="drive-absolute paths are Windows-only")
def test_a_drive_absolute_path_is_refused(root):
    with pytest.raises(BrowseError):
        resolve_within(root, r"C:\Windows\System32")


def test_a_leading_slash_is_treated_as_relative(root):
    # It must not be read as "the root of the disk".
    assert resolve_within(root, "/Recordings") == (root.path / "Recordings").resolve()


def test_an_ordinary_path_resolves(root):
    assert resolve_within(root, "Recordings").name == "Recordings"


def test_the_root_itself_is_allowed(root):
    assert resolve_within(root, "") == root.path.resolve()


def test_backslashes_are_accepted(root):
    assert resolve_within(root, r"Recordings\Old").name == "Old"


# --- listing -----------------------------------------------------------------
def test_folders_come_before_files(root):
    names = [entry.name for entry in browse(root, "Recordings")]

    assert names == ["Old", "standup.mp4"]


def test_only_media_is_offered(root):
    # A picker full of .docx is a picker nobody can use.
    assert "notes.docx" not in [entry.name for entry in browse(root, "Recordings")]


def test_everything_is_offered_when_asked(root):
    names = [entry.name for entry in browse(root, "Recordings", media_only=False)]

    assert "notes.docx" in names


def test_hidden_folders_are_skipped(root):
    assert ".hidden" not in [entry.name for entry in browse(root)]


def test_file_size_is_reported(root):
    entry = next(item for item in browse(root, "Recordings") if item.name == "standup.mp4")

    assert entry.size_bytes == 2048


def test_paths_are_relative_to_the_root(root):
    entry = next(item for item in browse(root, "Recordings") if item.name == "standup.mp4")

    assert entry.relative == "Recordings/standup.mp4"


def test_a_missing_folder_is_reported(root):
    with pytest.raises(BrowseError):
        browse(root, "Nowhere")


def test_listing_outside_the_root_is_refused(root):
    with pytest.raises(BrowseError):
        browse(root, "..")


# --- roots -------------------------------------------------------------------
def test_a_root_is_found_by_id(root):
    assert find_root([root], "onedrive-contoso") is root


def test_an_unknown_root_is_refused(root):
    with pytest.raises(BrowseError):
        find_root([root], "somewhere-else")


def test_roots_are_deduplicated(tmp_path, monkeypatch):
    # OneDriveCommercial and OneDrive routinely hold the same path.
    monkeypatch.setenv("OneDrive", str(tmp_path))
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path))

    assert len([r for r in cloud_roots() if r.path == tmp_path]) == 1


def test_a_folder_that_does_not_exist_is_not_offered(monkeypatch, tmp_path):
    monkeypatch.setenv("OneDrive", str(tmp_path / "gone"))

    assert all(root.path != tmp_path / "gone" for root in cloud_roots())


def test_an_extra_folder_can_be_added(tmp_path):
    assert any(root.path == tmp_path for root in cloud_roots(extra=[tmp_path]))


# --- copying -----------------------------------------------------------------
def test_a_file_is_copied_with_progress(root, tmp_path):
    seen = []
    written = copy_into(
        root.path / "Recordings" / "standup.mp4",
        tmp_path / "out" / "standup.mp4",
        lambda fraction, message: seen.append(fraction),
    )

    assert written == 2048
    assert (tmp_path / "out" / "standup.mp4").read_bytes() == b"v" * 2048
    assert seen and seen[-1] == 1.0


def test_copying_something_missing_is_reported(root, tmp_path):
    with pytest.raises(BrowseError):
        copy_into(root.path / "gone.mp4", tmp_path / "x.mp4", lambda *_: None)


def test_a_failed_copy_leaves_no_stub_behind(root, tmp_path, monkeypatch):
    destination = tmp_path / "out" / "standup.mp4"

    def explode(*_args, **_kwargs):
        raise OSError("the cloud file could not be fetched")

    monkeypatch.setattr(os, "fsync", explode, raising=False)
    source = root.path / "Recordings" / "standup.mp4"
    real_open = type(source).open

    def failing_open(self, *args, **kwargs):
        if self == source:
            raise OSError("the cloud file could not be fetched")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(source), "open", failing_open)

    with pytest.raises(BrowseError, match="online"):
        copy_into(source, destination, lambda *_: None)
    assert not destination.exists()


# --- what the UI shows -------------------------------------------------------
def test_an_entry_serialises_for_the_browser():
    payload = Entry("a.mp4", "Recordings/a.mp4", False, size_bytes=10, cloud_only=True).to_dict()

    assert payload["cloud_only"] is True
    assert payload["path"] == "Recordings/a.mp4"


def test_a_root_reports_whether_it_is_present(root):
    assert root.to_dict()["available"] is True


# --- searching ----------------------------------------------------------------
# Clicking down through Initiatives/FY26/Content Creation FY26/WDAC to reach three
# files is not a way to find a recording, and a folder someone has just shared
# could be anywhere.
def test_a_file_is_found_by_name(root):
    from clipdesk.ingest.localsource import search

    found = search(root, "standup")

    assert [entry.name for entry in found] == ["standup.mp4"]


def test_search_reaches_nested_folders(root):
    from clipdesk.ingest.localsource import search

    assert [entry.relative for entry in search(root, "archive")] == ["Recordings/Old/archive.mkv"]


def test_search_ignores_case(root):
    from clipdesk.ingest.localsource import search

    assert len(search(root, "STANDUP")) == 1


def test_search_only_returns_media(root):
    from clipdesk.ingest.localsource import search

    assert search(root, "notes") == []


def test_a_one_character_search_is_refused(root):
    # It would walk the whole disk to return everything.
    from clipdesk.ingest.localsource import search

    with pytest.raises(BrowseError):
        search(root, "a")


def test_search_results_are_capped(root):
    from clipdesk.ingest.localsource import search

    folder = root.path / "Many"
    folder.mkdir()
    for index in range(12):
        (folder / f"clip{index}.mp4").write_bytes(b"v")

    assert len(search(root, "clip", limit=5)) == 5


def test_search_finds_nothing_gracefully(root):
    from clipdesk.ingest.localsource import search

    assert search(root, "nothing-like-this") == []
