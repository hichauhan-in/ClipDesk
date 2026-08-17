"""Pure-logic tests. No ffmpeg, no model, no network."""

from clipdesk.transcription.subtitles import parse_cue_text, parse_timestamp

SRT = """\
1
00:00:01,000 --> 00:00:04,500
Welcome everyone.

2
00:00:04,500 --> 00:00:09,000
Today we're covering the retry policy
and how it backs off.

3
00:00:09,000 --> 00:00:12,000
Alice: Any questions before we start?
"""

VTT = """\
WEBVTT

00:00:00.000 --> 00:00:03.200
<v Bob>Can everyone hear me?

00:00:03.200 --> 00:00:07.000
Yes, <i>loud and clear</i>.
"""


def test_parse_timestamp_accepts_both_separators():
    assert parse_timestamp("00:00:04,500") == 4.5
    assert parse_timestamp("00:01:02.250") == 62.25
    assert parse_timestamp("01:02.5") == 62.5


def test_srt_multiline_cue_is_joined():
    segments = parse_cue_text(SRT)
    assert len(segments) == 3
    assert segments[1].text == "Today we're covering the retry policy and how it backs off."
    assert segments[1].start == 4.5


def test_srt_speaker_prefix_is_extracted():
    segments = parse_cue_text(SRT)
    assert segments[2].speaker == "Alice"
    assert segments[2].text == "Any questions before we start?"


def test_vtt_voice_tag_and_inline_markup():
    segments = parse_cue_text(VTT)
    assert segments[0].speaker == "Bob"
    assert segments[0].text == "Can everyone hear me?"
    assert segments[1].text == "Yes, loud and clear."


def test_ids_are_sequential_and_timeline_is_monotonic():
    segments = parse_cue_text(SRT)
    assert [segment.id for segment in segments] == [0, 1, 2]
    for earlier, later in zip(segments, segments[1:]):
        assert later.start >= earlier.end - 1e-9


def test_overlapping_cues_are_pushed_forward():
    overlapping = """\
1
00:00:00,000 --> 00:00:05,000
First line.

2
00:00:02,000 --> 00:00:06,000
Overlaps the first.
"""
    segments = parse_cue_text(overlapping)
    assert segments[1].start >= segments[0].end


def test_blank_and_malformed_blocks_are_skipped():
    messy = """\
NOTE this is a comment

1
not a timestamp
ignored

2
00:00:01,000 --> 00:00:02,000
Kept.
"""
    segments = parse_cue_text(messy)
    assert len(segments) == 1
    assert segments[0].text == "Kept."
