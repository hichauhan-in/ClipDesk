"""Output file management: paths are taken from the browser, so the guards
around them are the point of these tests."""

import zipfile

import pytest

from clipdesk.actions.outputs import (
    OutputError,
    bundle_outputs,
    delete_output,
    delete_source,
    rename_output,
    resolve_outputs,
    source_info,
)
from clipdesk.models import ArtifactKind
from clipdesk.store import ProjectStore


@pytest.fixture
def project(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    project = store.create("meeting.mp4", title="Meeting")
    project.output_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("notes.md", "# Notes"), ("summary.md", "# Summary"), ("clip.mp4", "x" * 64)):
        path = project.output_dir / name
        path.write_text(body, encoding="utf-8")
        project.add_artifact(ArtifactKind.NOTES, name, path)
    return project


# --- path safety -------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["../../secret.txt", "..\\..\\secret.txt", "sub/dir/notes.md", "", ".", ".."],
)
def test_paths_outside_the_output_folder_are_refused(project, name):
    with pytest.raises(OutputError):
        resolve_outputs(project, [name])


def test_delete_also_refuses_a_path(project):
    with pytest.raises(OutputError):
        delete_output(project, "../notes.md")


def test_missing_files_are_reported(project):
    with pytest.raises(OutputError, match="No such output"):
        resolve_outputs(project, ["nope.md"])


def test_an_empty_selection_is_refused(project):
    with pytest.raises(OutputError, match="No files selected"):
        resolve_outputs(project, [])


def test_resolving_returns_real_paths(project):
    paths = resolve_outputs(project, ["notes.md", "summary.md"])
    assert [path.name for path in paths] == ["notes.md", "summary.md"]


def test_stale_job_snapshots_merge_artifacts_instead_of_losing_outputs(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    created = store.create("meeting.mp4")
    first = store.get(created.id)
    second = store.get(created.id)
    intro = created.output_path("intro.mp4")
    outro = created.output_path("outro.mp4")
    intro.write_bytes(b"intro")
    outro.write_bytes(b"outro")

    first.add_artifact(ArtifactKind.INTRO, "Intro", intro)
    second.add_artifact(ArtifactKind.OUTRO, "Outro", outro)

    reloaded = store.require(created.id)
    assert [(item["kind"], item["filename"]) for item in reloaded.meta.artifacts] == [
        ("intro", "intro.mp4"),
        ("outro", "outro.mp4"),
    ]


def test_stale_removal_preserves_artifacts_added_after_the_snapshot(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    created = store.create("meeting.mp4")
    notes = created.output_path("notes.md")
    notes.write_text("notes", encoding="utf-8")
    created.add_artifact(ArtifactKind.NOTES, "Notes", notes)
    stale = store.require(created.id)
    intro = created.output_path("intro.mp4")
    intro.write_bytes(b"intro")
    created.add_artifact(ArtifactKind.INTRO, "Intro", intro)

    assert stale.remove_artifact("notes-notes") is True

    reloaded = store.require(created.id)
    assert [(item["kind"], item["filename"]) for item in reloaded.meta.artifacts] == [
        ("intro", "intro.mp4")
    ]


def test_stale_job_snapshots_accumulate_token_usage(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    created = store.create("meeting.mp4")
    first = store.require(created.id)
    second = store.require(created.id)
    usage = {
        "calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "measured": True,
        "by_task": {"notes": {"calls": 1, "prompt": 10, "completion": 5}},
        "by_model": {"model": {"calls": 1, "prompt": 10, "completion": 5}},
        "models": ["model"],
    }

    first.record_tokens(usage)
    second.record_tokens(usage)

    assert store.require(created.id).meta.tokens["total_tokens"] == 30


# --- delete ------------------------------------------------------------------
def test_delete_removes_the_file_and_the_artifact(project):
    assert delete_output(project, "notes.md") is True
    assert not (project.output_dir / "notes.md").exists()
    assert all(entry["filename"] != "notes.md" for entry in project.meta.artifacts)


def test_deleting_something_already_gone_is_not_an_error(project):
    assert delete_output(project, "ghost.md") is False


def test_delete_leaves_other_files_alone(project):
    delete_output(project, "notes.md")
    assert (project.output_dir / "summary.md").exists()
    assert len(project.meta.artifacts) == 2


# --- rename ------------------------------------------------------------------
def test_rename_moves_file_and_updates_artifact(project):
    renamed = rename_output(project, "notes.md", "meeting-notes")

    assert renamed.name == "meeting-notes.md"
    assert renamed.is_file()
    assert not (project.output_dir / "notes.md").exists()
    assert any(entry["filename"] == "meeting-notes.md" for entry in project.meta.artifacts)


def test_rename_preserves_the_file_type(project):
    with pytest.raises(OutputError, match="file type must stay .md"):
        rename_output(project, "notes.md", "notes.mp4")


def test_rename_refuses_an_existing_output(project):
    with pytest.raises(OutputError, match="already exists"):
        rename_output(project, "notes.md", "summary.md")


def test_rename_refuses_paths(project):
    with pytest.raises(OutputError, match="Invalid output name"):
        rename_output(project, "notes.md", "../renamed.md")


# --- bundle ------------------------------------------------------------------
def test_bundle_contains_the_selected_files(project):
    archive = bundle_outputs(project, ["notes.md", "clip.mp4"], "Meeting")
    with zipfile.ZipFile(archive) as zf:
        assert sorted(zf.namelist()) == ["clip.mp4", "notes.md"]


def test_bundle_stores_compressed_media_but_deflates_text(project):
    archive = bundle_outputs(project, ["notes.md", "clip.mp4"], "Mixed")

    with zipfile.ZipFile(archive) as zf:
        assert zf.getinfo("clip.mp4").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("notes.md").compress_type == zipfile.ZIP_DEFLATED


def test_bundle_is_named_after_the_project(project):
    assert bundle_outputs(project, ["notes.md"], "Meeting").name == "Meeting.zip"


def test_bundle_falls_back_to_a_default_name(project):
    assert bundle_outputs(project, ["notes.md"]).name == "clipdesk-selection.zip"


def test_bundle_is_listed_as_an_artifact(project):
    archive = bundle_outputs(project, ["notes.md"], "Meeting")
    filenames = [entry["filename"] for entry in project.meta.artifacts]
    assert archive.name in filenames


def test_rebundling_does_not_nest_the_previous_archive(project):
    first = bundle_outputs(project, ["notes.md"], "Meeting")
    second = bundle_outputs(project, ["notes.md", "summary.md"], "Meeting")
    assert first == second
    with zipfile.ZipFile(second) as zf:
        assert "Meeting.zip" not in zf.namelist()
        assert sorted(zf.namelist()) == ["notes.md", "summary.md"]


def test_bundling_only_the_previous_bundle_is_refused(project):
    bundle_outputs(project, ["notes.md"], "Meeting")
    with pytest.raises(OutputError, match="No files selected"):
        bundle_outputs(project, ["Meeting.zip"], "Meeting")


# --- the original recording ---------------------------------------------------
# Importing from a link leaves a file the user never chose to download, so it has
# to be visible and removable without taking the derived work with it.
def test_source_is_reported_with_its_size(project):
    project.source_path.parent.mkdir(parents=True, exist_ok=True)
    project.source_path.write_bytes(b"x" * 4096)

    info = source_info(project)

    assert info["exists"] is True
    assert info["size_bytes"] == 4096
    assert info["filename"] == "meeting.mp4"


def test_a_missing_source_is_reported_rather_than_raising(project):
    info = source_info(project)

    assert info["exists"] is False
    assert info["size_bytes"] == 0


def test_an_uploaded_source_is_not_marked_as_imported(project):
    assert source_info(project)["imported"] is False


def test_a_downloaded_source_carries_the_link_it_came_from(project):
    project.meta.source_url = "https://example.invalid/recording.mp4"
    project.save()

    info = source_info(project)

    assert info["imported"] is True
    assert info["source_url"] == "https://example.invalid/recording.mp4"


def test_deleting_the_source_keeps_the_outputs(project):
    project.source_path.parent.mkdir(parents=True, exist_ok=True)
    project.source_path.write_bytes(b"x" * 4096)

    assert delete_source(project) is True

    assert not project.source_path.exists()
    assert (project.output_dir / "notes.md").exists()
    assert (project.output_dir / "clip.mp4").exists()


def test_deleting_a_source_that_is_already_gone_is_not_an_error(project):
    assert delete_source(project) is False


def test_deleting_the_source_clears_the_recorded_size(project):
    project.source_path.parent.mkdir(parents=True, exist_ok=True)
    project.source_path.write_bytes(b"x" * 4096)
    project.meta.size_bytes = 4096

    delete_source(project)

    assert project.meta.size_bytes == 0
