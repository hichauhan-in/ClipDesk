"""Windowing and the tolerant JSON parsing that keeps analysis alive when a
model ignores "JSON only"."""

import pytest

from clipdesk.analysis.windows import build_windows, format_timestamp, parse_timestamp
from clipdesk.llm.base import LLMError, extract_json
from clipdesk.models import TranscriptSegment


def segments(count, words=12):
    return [
        TranscriptSegment(id=i, start=i * 5.0, end=i * 5.0 + 5.0, text=" ".join(["word"] * words))
        for i in range(count)
    ]


# --- windows -----------------------------------------------------------------
def test_empty_transcript_produces_no_windows():
    assert build_windows([]) == []


def test_a_short_transcript_fits_in_one_window():
    windows = build_windows(segments(4), window_chars=9000)
    assert len(windows) == 1
    assert windows[0].last_id == 3


def test_long_transcripts_are_split():
    windows = build_windows(segments(200), window_chars=1200, overlap_chars=0)
    assert len(windows) > 1
    assert windows[0].index == 0


def test_windows_cover_every_segment():
    source = segments(120)
    windows = build_windows(source, window_chars=900, overlap_chars=200)
    covered = {segment.id for window in windows for segment in window.segments}
    assert covered == {segment.id for segment in source}


def test_consecutive_windows_overlap():
    windows = build_windows(segments(120), window_chars=900, overlap_chars=300)
    first, second = windows[0], windows[1]
    assert second.first_id <= first.last_id


def test_rendered_window_carries_ids_and_timestamps():
    rendered = build_windows(segments(3))[0].render()
    assert rendered.startswith("[0] 00:00 ")
    assert "[2] 00:10 " in rendered


# --- timestamps --------------------------------------------------------------
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00"), (65, "01:05"), (3600, "01:00:00"), (3725, "01:02:05")],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(83.5, 83.5), ("83.5", 83.5), ("01:23", 83.0), ("00:01:23.5", 83.5), ("", 0.0), ("junk", 0.0)],
)
def test_parse_timestamp_is_forgiving(value, expected):
    assert parse_timestamp(value) == pytest.approx(expected)


# --- JSON extraction ---------------------------------------------------------
def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_json_inside_a_code_fence():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```\nHope that helps.') == {"a": 1}


def test_json_with_surrounding_prose():
    assert extract_json('Here you go: {"a": [1, 2]} — let me know.') == {"a": [1, 2]}


def test_trailing_commas_are_repaired():
    assert extract_json('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    assert extract_json('prefix {"a": "a } brace", "b": 2} suffix') == {
        "a": "a } brace",
        "b": 2,
    }


def test_a_bare_array_is_accepted():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_unparseable_output_raises():
    with pytest.raises(LLMError):
        extract_json("I'm afraid I can't help with that.")
