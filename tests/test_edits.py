"""The constrained prompt-edit grammar: safe operations in, no commands out."""

import pytest

from clipdesk.actions.edits import parse_edit_prompt


def test_text_overlay_prompt_is_parsed_without_executable_commands():
    plan = parse_edit_prompt(
        'add text "Confidential" bottom right text size 48 from 00:10 to 00:20',
        60.0,
    )

    assert plan.text == "Confidential"
    assert plan.position == "bottom-right"
    assert plan.font_size == 48
    assert plan.start == 10.0
    assert plan.end == 20.0


def test_lightweight_effects_can_be_combined():
    plan = parse_edit_prompt("make it grayscale, add soft blur and fade in for 2s", 30.0)

    assert plan.grayscale is True
    assert plan.blur is True
    assert plan.fade_in == 2.0


def test_color_geometry_and_audio_edits_can_be_combined():
    plan = parse_edit_prompt(
        "make it sepia, add vignette, increase contrast, rotate 90 degrees and volume to 35%",
        30.0,
    )

    assert plan.sepia is True
    assert plan.vignette is True
    assert plan.contrast == pytest.approx(1.15)
    assert plan.rotate == 90
    assert plan.volume == pytest.approx(0.35)


def test_prompt_can_remove_source_audio():
    plan = parse_edit_prompt("mute audio, sharpen and mirror the video", 30.0)

    assert plan.mute is True
    assert plan.sharpen is True
    assert plan.flip == "horizontal"


def test_unsupported_freeform_edits_are_rejected_instead_of_guessed():
    with pytest.raises(ValueError, match="not supported"):
        parse_edit_prompt("replace the presenter with an animated avatar", 30.0)


def test_overlay_text_must_be_quoted():
    with pytest.raises(ValueError, match="in quotes"):
        parse_edit_prompt("add text Confidential bottom right", 30.0)


def test_edit_ranges_must_fit_inside_the_video():
    with pytest.raises(ValueError, match="outside this video"):
        parse_edit_prompt('text "Late" from 00:50 to 01:10', 60.0)


def test_prompt_can_name_an_imported_intro():
    plan = parse_edit_prompt("add intro Post.mp4", 60.0, ("Post.mp4", "Outro.mp4"))

    assert plan.intro_asset == "Post.mp4"
    assert plan.to_dict()["operations"] == ["Add intro: Post.mp4"]


def test_prompt_rejects_unknown_imported_media():
    with pytest.raises(ValueError, match="Available media"):
        parse_edit_prompt("add intro Missing.mp4", 60.0, ("Post.mp4",))
