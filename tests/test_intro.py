"""The intro sequencer: the plan is the contract, and styles stay declarative."""

import pytest

from clipdesk.actions.intro import (
    BUILT_IN_STYLES,
    CATALOG_STYLES,
    _fit_text,
    IntroStyle,
    available_styles,
    fit_narration,
    import_custom_style,
    install_catalog_style,
    plan_intro,
    plan_outro,
    resolve_style,
    shot_anchors,
    shot_labels,
)
from clipdesk.models import AnalysisReport, Chapter, MediaInfo, SegmentAnalysis, SegmentKind


def report_with(*segments: SegmentAnalysis) -> AnalysisReport:
    return AnalysisReport(
        project_id="test",
        media=MediaInfo(path="x.mp4", duration_s=120.0),
        segment_analyses=list(segments),
    )


# --- the catalog -------------------------------------------------------------
def test_every_built_in_style_is_installed_and_uniquely_named(tmp_path):
    styles = available_styles(tmp_path)
    ids = [style.id for style in (*BUILT_IN_STYLES, *CATALOG_STYLES)]

    assert len(BUILT_IN_STYLES) == 18
    assert len(CATALOG_STYLES) == 26
    assert len(styles) == len(BUILT_IN_STYLES)
    assert len(ids) == len(set(ids))
    assert resolve_style(tmp_path, "prestige").title_animation == "band-reveal"


def test_styles_cover_every_title_animation():
    assert {style.title_animation for style in BUILT_IN_STYLES} == {
        "band-reveal",
        "stack-lines",
        "center-pop",
        "side-panel",
        "flash-cut",
        "split-bars",
        "lower-third",
    }


def test_styles_cover_every_backdrop():
    assert {style.backdrop for style in BUILT_IN_STYLES} == {
        "source-blur",
        "gradient",
        "dark-panel",
        "duotone",
        "stage",
        "grid",
    }


def test_styles_cover_every_shot_motion():
    used = {motion for style in BUILT_IN_STYLES for motion in style.shot_motions}

    assert used == {"punch-in", "pull-back", "whip", "drift", "hold", "tilt", "glide"}


def test_long_lower_third_title_is_sized_inside_its_allocation():
    body, size, lines = _fit_text(
        "A very long quarterly platform review with rollout details and decisions "
        "for every engineering group across the company",
        max_width=1152,
        ideal_size=92,
        min_size=48,
    )

    assert lines <= 2
    assert max(len(line) for line in body.splitlines()) * 0.62 * size <= 1152


def test_catalog_styles_install_on_demand(tmp_path):
    installed = install_catalog_style(tmp_path, "cinema-bars")

    assert installed.source == "catalog"
    assert resolve_style(tmp_path, "cinema-bars").grade == "cinematic"
    assert len(available_styles(tmp_path)) == len(BUILT_IN_STYLES) + 1


def test_unknown_style_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="No intro style"):
        resolve_style(tmp_path, "does-not-exist")


# --- imported styles stay data ------------------------------------------------
def test_custom_styles_are_declarative_and_persisted(tmp_path):
    style = import_custom_style(
        tmp_path,
        {
            "id": "My Team Open",
            "name": "My team open",
            "description": "A warm and measured internal opener.",
            "accent": "#f0a13c",
            "backdrop": "source-blur",
            "title_animation": "band-reveal",
            "shot_motions": ["drift"],
            "transition": "dissolve",
            "grade": "warm",
        },
    )

    assert style.id == "my-team-open"
    assert resolve_style(tmp_path, style.id).accent == "#f0a13c"


def test_custom_styles_reject_raw_ffmpeg_in_place_of_known_values(tmp_path):
    with pytest.raises(ValueError):
        import_custom_style(
            tmp_path,
            {
                "id": "unsafe",
                "name": "Unsafe",
                "description": "Attempts to inject a raw filter.",
                "title_animation": "drawtext=text=hacked",
            },
        )


def test_custom_styles_reject_unknown_command_fields(tmp_path):
    with pytest.raises(ValueError):
        import_custom_style(
            tmp_path,
            {
                "id": "unsafe-command",
                "name": "Unsafe command",
                "description": "Attempts to add an unsupported command field.",
                "ffmpeg_args": ["-filter_complex", "evil"],
            },
        )


def test_custom_styles_reject_a_non_colour_accent(tmp_path):
    with pytest.raises(ValueError):
        import_custom_style(
            tmp_path,
            {
                "id": "bad-accent",
                "name": "Bad accent",
                "description": "Accent is not a hex colour.",
                "accent": "white; rm -rf",
            },
        )


def test_custom_styles_cannot_replace_a_bundled_style(tmp_path):
    with pytest.raises(ValueError, match="cannot replace"):
        import_custom_style(
            tmp_path,
            {"id": "prestige", "name": "Fake", "description": "Shadows a bundled style."},
        )


# --- the plan ----------------------------------------------------------------
@pytest.mark.parametrize("style", BUILT_IN_STYLES, ids=lambda item: item.id)
def test_every_style_lands_on_the_requested_runtime(style):
    plan = plan_intro(
        style,
        total_seconds=style.default_duration_seconds,
        shot_count=style.default_shots,
        source_duration=120.0,
        subtitle="A short overview of the recording",
    )

    assert plan.total_seconds == pytest.approx(style.default_duration_seconds, abs=0.05)


@pytest.mark.parametrize("seconds", [5.0, 7.0, 10.0, 18.0, 30.0, 60.0])
def test_any_requested_length_is_honoured(seconds):
    plan = plan_intro(
        BUILT_IN_STYLES[0],
        total_seconds=seconds,
        shot_count=6,
        source_duration=200.0,
        subtitle="An overview line",
    )

    assert plan.total_seconds == pytest.approx(seconds, abs=0.05)
    assert all(scene.duration > 0 for scene in plan.scenes)


def test_the_plan_is_a_sequence_not_a_pile_of_clips():
    plan = plan_intro(
        BUILT_IN_STYLES[0],
        total_seconds=16.0,
        shot_count=5,
        source_duration=120.0,
        subtitle="An overview line",
    )
    kinds = [scene.kind for scene in plan.scenes]

    assert kinds[0] == "hook"
    assert kinds[1] == "title"
    assert kinds[-1] == "end-card"
    assert kinds.count("shot") == 5


def test_shots_are_not_all_the_same_length():
    plan = plan_intro(
        BUILT_IN_STYLES[0], total_seconds=16.0, shot_count=5, source_duration=120.0
    )
    durations = {round(scene.duration, 2) for scene in plan.shots}

    assert len(durations) > 1


def test_a_short_intro_drops_shots_rather_than_flashing_frames():
    plan = plan_intro(
        BUILT_IN_STYLES[0], total_seconds=5.0, shot_count=8, source_duration=120.0
    )

    assert all(scene.duration >= 0.5 for scene in plan.shots)
    assert plan.total_seconds == pytest.approx(5.0, abs=0.05)


def test_the_kicker_only_appears_when_there_is_a_line_to_show():
    without = plan_intro(
        BUILT_IN_STYLES[0], total_seconds=16.0, shot_count=4, source_duration=120.0
    )
    with_line = plan_intro(
        BUILT_IN_STYLES[0],
        total_seconds=16.0,
        shot_count=4,
        source_duration=120.0,
        subtitle="Why the rollout stalled",
    )

    assert "kicker" not in [scene.kind for scene in without.scenes]
    assert "kicker" in [scene.kind for scene in with_line.scenes]


def test_shot_spans_stay_inside_the_source():
    plan = plan_intro(
        BUILT_IN_STYLES[0], total_seconds=20.0, shot_count=6, source_duration=30.0
    )

    for scene in plan.scenes:
        if scene.span is None:
            continue
        start, end = scene.span
        assert 0.0 <= start < end <= 30.0 + 1e-6


def test_motions_rotate_through_the_style_list():
    style = next(item for item in BUILT_IN_STYLES if len(item.shot_motions) > 1)
    plan = plan_intro(style, total_seconds=18.0, shot_count=4, source_duration=120.0)

    assert len({scene.motion for scene in plan.shots}) > 1


def test_the_plan_can_describe_itself():
    plan = plan_intro(
        BUILT_IN_STYLES[0], total_seconds=14.0, shot_count=4, source_duration=120.0
    )
    described = plan.describe()

    assert len(described) == len(plan.scenes)
    assert any(line.startswith("Title reveal") for line in described)


@pytest.mark.parametrize("seconds", [5.0, 8.0, 15.0, 30.0])
def test_outro_is_title_cards_only_and_honours_runtime(seconds):
    plan = plan_outro(
        BUILT_IN_STYLES[0], total_seconds=seconds, source_duration=120.0,
        include_final_message=True,
    )

    assert [scene.kind for scene in plan.scenes] == ["title", "end-card"]
    assert plan.shots == ()
    assert plan.total_seconds == pytest.approx(seconds, abs=0.001)
    assert len({scene.span for scene in plan.scenes}) == 1


def test_outro_without_an_optional_final_message_uses_one_title_card():
    plan = plan_outro(
        BUILT_IN_STYLES[0], total_seconds=8.0, source_duration=120.0
    )

    assert [scene.kind for scene in plan.scenes] == ["title"]
    assert plan.total_seconds == pytest.approx(8.0)


def test_outro_rejects_an_unknown_source_duration():
    with pytest.raises(ValueError, match="source duration"):
        plan_outro(BUILT_IN_STYLES[0], total_seconds=8.0, source_duration=0.0)


# --- shot selection -----------------------------------------------------------
def test_anchors_prefer_the_high_value_moments():
    report = report_with(
        SegmentAnalysis(segment_id=0, start=0, end=20, kind=SegmentKind.ON_TOPIC, importance=0.1),
        SegmentAnalysis(segment_id=1, start=60, end=80, kind=SegmentKind.ON_TOPIC, importance=0.9),
    )

    anchors = shot_anchors(120.0, 2, report)

    assert any(abs(anchor - 70.0) < 1.0 for anchor in anchors)


def test_anchors_work_without_any_analysis():
    anchors = shot_anchors(100.0, 4, None)

    assert len(anchors) == 4
    assert anchors == sorted(anchors)


def test_plain_video_shots_get_no_invented_labels():
    assert shot_labels([(0.0, 2.0), (4.0, 6.0)], None) == ["", ""]


def test_shot_labels_follow_the_chapters():
    report = AnalysisReport(
        project_id="test",
        media=MediaInfo(path="x.mp4", duration_s=120.0),
        chapters=[Chapter(index=0, title="Root cause", start=0.0, end=60.0)],
    )

    assert shot_labels([(5.0, 9.0)], report) == ["Root cause"]


# --- narration ----------------------------------------------------------------
def test_narration_is_fitted_to_the_intro_length():
    overview = " ".join(f"word{index}" for index in range(100))

    assert len(fit_narration(overview, 5).split()) <= 10
    assert len(fit_narration(overview, 20).split()) <= 42


def test_narration_falls_back_when_there_is_nothing_to_say():
    assert fit_narration("   ", 10)


# --- style guardrails ---------------------------------------------------------
def test_a_style_cannot_ask_for_an_absurd_transition_length():
    with pytest.raises(ValueError):
        IntroStyle(
            id="too-slow",
            name="Too slow",
            description="Transition longer than allowed.",
            transition_seconds=9.0,
        )


def test_catalog_ids_never_collide_with_built_ins():
    built_in = {style.id for style in BUILT_IN_STYLES}

    assert not built_in & {style.id for style in CATALOG_STYLES}
