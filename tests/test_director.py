"""The prompt director routes one instruction to exactly one capability."""

from types import SimpleNamespace

import pytest

from clipdesk.actions.director import PromptContext, plan_prompt
from clipdesk.actions.exports_media import export_args, export_options, plan_export, render_export
from clipdesk.store import ProjectStore

CONTEXT = PromptContext(
    duration_s=3600.0,
    media_names=("Pre.mp4", "Post.mp4"),
    output_names=("cleaned.mp4", "final.mp4"),
    style_ids=("prestige", "newsroom", "neon-pulse", "noir-cut", "keynote"),
    title="Comms review",
    source_filename="talk.mp4",
    has_analysis=True,
)


@pytest.mark.parametrize(
    ("prompt", "intent"),
    [
        ("create a cinematic intro that runs 12 seconds", "intro"),
        ('make an outro saying "Thanks for watching"', "outro"),
        ("clean up the recording and remove the pauses", "clean"),
        ("clip from 04:10 to 06:00", "clip"),
        ("find 3 highlights", "clip"),
        ("export cleaned.mp4 as a small mp4", "export"),
        ("just the audio as mp3", "export"),
        ("attach intro Pre.mp4 and outro Post.mp4", "assemble"),
        # Trims belong to the edit program, which can compose them with effects.
        ("trim the first 30 seconds", "effects"),
        ("drop the last 2 minutes", "effects"),
        ("make it sepia and add a vignette", "effects"),
    ],
)
def test_each_instruction_routes_to_one_capability(prompt, intent):
    assert plan_prompt(prompt, CONTEXT).intent == intent


def test_intro_length_and_moments_are_read_from_the_words():
    plan = plan_prompt('build a social intro of 9 seconds with 6 moments titled "Launch"', CONTEXT)

    assert plan.params["duration_seconds"] == 9
    assert plan.params["shot_count"] == 6
    assert plan.params["title"] == "Launch"
    assert plan.params["style_id"] == "neon-pulse"


def test_intro_and_outro_files_land_in_separate_slots():
    plan = plan_prompt("attach intro Pre.mp4 and outro Post.mp4", CONTEXT)

    assert plan.params["header_asset"] == "Pre.mp4"
    assert plan.params["footer_asset"] == "Post.mp4"


def test_trimming_the_last_minutes_is_measured_from_the_end():
    plan = plan_prompt("drop the last 2 minutes", CONTEXT)

    assert plan.intent == "effects"
    assert plan.params["trim_start"] == 0.0
    assert plan.params["trim_end"] == pytest.approx(3600.0 - 120.0)


def test_an_explicit_span_becomes_a_single_clip():
    plan = plan_prompt("clip from 04:10 to 06:00", CONTEXT)

    assert plan.params["mode"] == "span"
    assert plan.params["start"] == pytest.approx(250.0)
    assert plan.params["end"] == pytest.approx(360.0)


def test_cleaning_and_removing_the_intro_is_a_clean_cut_not_an_intro_build():
    plan = plan_prompt("clean up the recording and remove the intro", CONTEXT)

    assert plan.intent == "clean"
    assert plan.params["remove_intro"] is True


def test_mov_matches_as_a_format_but_not_inside_remove():
    assert plan_prompt("convert to mov", CONTEXT).params["format"] == "mov"
    assert plan_prompt("compress and remove the noise", CONTEXT).params["format"] == "mp4"


def test_after_timecode_sets_the_clip_start():
    plan = plan_prompt("clip after 10:00", CONTEXT)

    assert plan.params["start"] == pytest.approx(600.0)


def test_naming_an_unknown_file_is_refused_with_the_available_names():
    with pytest.raises(ValueError, match="Pre.mp4"):
        plan_prompt("attach intro missing.mp4", CONTEXT)


def test_a_topic_search_needs_the_analysis():
    without = PromptContext(duration_s=600.0, has_analysis=False)

    with pytest.raises(ValueError, match="explicit range"):
        plan_prompt('clip the part about "retries"', without)


def test_an_empty_instruction_is_refused():
    with pytest.raises(ValueError, match="Describe"):
        plan_prompt("   ", CONTEXT)


def test_planning_never_emits_ffmpeg_arguments():
    for prompt in (
        "create an intro",
        "clean it up",
        "export as gif",
        "trim the first 10 seconds",
        "make it sepia",
    ):
        plan = plan_prompt(prompt, CONTEXT)
        rendered = repr(plan.to_dict())
        assert "-filter_complex" not in rendered
        assert "ffmpeg" not in rendered.lower()


# --- exports -----------------------------------------------------------------
def test_export_names_stay_inside_the_chosen_container():
    plan = plan_export("cleaned.mp4", "webm", "small")

    assert plan.output_name.endswith(".webm")
    assert plan.audio_only is False


def test_audio_containers_drop_the_video_stream():
    plan = plan_export("cleaned.mp4", "mp3", "high")

    assert plan.audio_only is True
    assert "-vn" in export_args(plan)


def test_unsupported_format_or_quality_is_refused():
    with pytest.raises(ValueError, match="format"):
        plan_export("a.mp4", "avi", "high")
    with pytest.raises(ValueError, match="quality"):
        plan_export("a.mp4", "mp4", "ultra")


def test_every_offered_option_can_actually_be_planned():
    options = export_options()

    for container in options["formats"]:
        for quality in options["qualities"]:
            plan = plan_export("clip.mp4", container["id"], quality["id"])
            assert export_args(plan)


def test_video_quality_names_refer_to_height():
    args = export_args(plan_export("clip.mp4", "mp4", "balanced"))
    scale = args[args.index("-vf") + 1]

    assert "min(720,ih)" in scale


def test_extracting_audio_from_a_silent_video_is_rejected_early(tmp_path, monkeypatch):
    from clipdesk.actions import exports_media

    project = ProjectStore(tmp_path / "workspace").create("silent.mp4")
    source = project.source_dir / "silent.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        exports_media,
        "probe",
        lambda *_args: SimpleNamespace(has_audio=False, duration_s=2.0),
    )

    with pytest.raises(ValueError, match="no audio track"):
        render_export(
            project,
            plan_export("silent.mp4", "mp3", "balanced"),
            source=source,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
        )
