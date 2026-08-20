"""Soundtrack catalog and local cache behavior."""

from io import BytesIO

import pytest

from clipdesk.media import soundtrack


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_catalog_uses_real_cc0_tracks_instead_of_generated_tones(tmp_path):
    library = soundtrack.list_audio(tmp_path)

    assert len(library["built_in"]) == 15
    assert {item["id"] for item in library["built_in"]} >= {
        "elevate", "inspiration", "backbeat"
    }
    assert not {item["id"] for item in library["built_in"]} & {
        "baltic-levity", "coy-koi", "fancy-family", "busybody", "travelers-notebook",
        "dreams-of-vain", "neptunian-princess", "rural-industry", "gothamlicious",
    }
    assert {item["license"] for item in library["built_in"]} == {"CC0-1.0"}
    assert all(item["source_url"].endswith(".mp3") for item in library["built_in"])
    assert not hasattr(soundtrack, "_THEME_EXPRESSIONS")


def test_built_in_track_downloads_once_and_is_cached(tmp_path, monkeypatch):
    calls = []

    def fake_open(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse(b"real mp3 bytes")

    monkeypatch.setattr(soundtrack, "urlopen", fake_open)

    first = soundtrack.resolve_audio(tmp_path, "elevate")
    second = soundtrack.resolve_audio(tmp_path, "elevate")

    assert first == second
    assert first.read_bytes() == b"real mp3 bytes"
    assert len(calls) == 1


def test_unknown_soundtrack_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="No intro/outro soundtrack"):
        soundtrack.resolve_audio(tmp_path, "old-hum")


def test_none_soundtrack_generates_silence_without_a_source(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        soundtrack,
        "run",
        lambda _ffmpeg, args, **_kwargs: captured.update(args=args),
    )

    soundtrack.prepare_soundtrack(
        "none", 8.0, tmp_path / "silence.wav", "ffmpeg"
    )

    assert any("anullsrc=" in value for value in captured["args"])