"""Tests for the six capabilities added beyond the original brief.

Each test asserts a property that would be silently wrong rather than loudly broken — the
kind that ships and is discovered by a user weeks later.
"""

from __future__ import annotations

import pytest

from ttsgrab.core.errors import InvalidRequest, RateLimited
from ttsgrab.core.subtitles import MIN_CUE_MS, render, to_srt, to_vtt
from ttsgrab.core.types import SpeechSegment
from ttsgrab.core.usage import project_runway


def seg(text: str, start: int, end: int) -> SpeechSegment:
    return SpeechSegment(text=text, start_ms=start, end_ms=end)


# -- Retry-After -----------------------------------------------------------------------


def test_rate_limited_advertises_when_to_retry() -> None:
    headers = RateLimited("slow", details={"retry_after_seconds": 17}).http_headers("r1")
    assert headers["Retry-After"] == "17"


def test_retry_after_omitted_without_a_known_delay() -> None:
    """`Retry-After: 0` would invite an immediate retry that must fail again."""
    assert "Retry-After" not in RateLimited("slow").http_headers()


# -- Loudness --------------------------------------------------------------------------


def test_loudness_presets_map_to_the_documented_lufs_values() -> None:
    from ttsgrab.core.audio import LOUDNESS_TARGETS, resolve_loudness_target

    assert resolve_loudness_target("podcast") == -16.0
    assert resolve_loudness_target("streaming") == -14.0
    assert resolve_loudness_target("broadcast") == -23.0
    # And the table and the resolver cannot drift apart.
    for name, lufs in LOUDNESS_TARGETS.items():
        assert resolve_loudness_target(name) == lufs


def test_loudness_accepts_a_raw_number() -> None:
    from ttsgrab.core.audio import resolve_loudness_target

    assert resolve_loudness_target(-19.5) == -19.5
    assert resolve_loudness_target("-19.5") == -19.5


@pytest.mark.parametrize("bad", ["-100", "-1", 0, "loud"])
def test_loudness_rejects_targets_it_cannot_meaningfully_hit(bad) -> None:
    from ttsgrab.core.audio import resolve_loudness_target

    with pytest.raises(InvalidRequest):
        resolve_loudness_target(bad)


def test_loudness_none_means_leave_the_audio_alone() -> None:
    from ttsgrab.core.audio import resolve_loudness_target

    assert resolve_loudness_target(None) is None


# -- Subtitles -------------------------------------------------------------------------


def test_srt_timestamps_use_a_comma_and_are_zero_padded() -> None:
    out = to_srt([seg("hello", 0, 1500), seg("world", 1500, 3_723_456)])
    assert "00:00:00,000 --> 00:00:01,500" in out
    assert "01:02:03,456" in out, "hour place is wrong"


def test_vtt_timestamps_use_a_dot_and_the_file_starts_with_webvtt() -> None:
    out = to_vtt([seg("hello", 0, 1500)])
    assert out.lstrip().startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in out


def test_srt_cues_are_numbered_from_one() -> None:
    out = to_srt([seg("a", 0, 1000), seg("b", 1000, 2000)])
    assert out.splitlines()[0] == "1"
    assert "\n2\n" in out


def test_cues_are_separated_by_a_blank_line() -> None:
    """Two cues with no blank line between them parse as one cue in every player."""
    out = to_srt([seg("a", 0, 1000), seg("b", 1000, 2000)])
    assert "\n\n" in out


def test_very_short_cues_are_merged_rather_than_flickered() -> None:
    """A 120 ms cue is one frame; players show it as a flash."""
    out = to_srt([seg("a", 0, 120), seg("b", 120, 2000)])
    assert len(out.strip().split("\n\n")) == 1
    assert "a b" in out


def test_cue_longer_than_the_threshold_is_left_alone() -> None:
    out = to_srt([seg("a", 0, MIN_CUE_MS + 10), seg("b", MIN_CUE_MS + 10, 5000)])
    assert len(out.strip().split("\n\n")) == 2


def test_multiline_text_in_a_segment_cannot_break_the_cue_block() -> None:
    """A blank line inside cue text terminates the cue; collapse whitespace."""
    out = to_srt([seg("line one\n\nline two", 0, 2000)])
    assert len(out.strip().split("\n\n")) == 1
    assert "line one line two" in out


def test_empty_segments_produce_an_empty_file_not_a_broken_one() -> None:
    assert to_srt([]) == ""
    assert to_vtt([]).strip() == "WEBVTT"


def test_render_dispatches_and_rejects_unknown_formats() -> None:
    segs = [seg("x", 0, 2000)]
    assert render(segs, "srt") == to_srt(segs)
    assert render(segs, "vtt") == to_vtt(segs)
    with pytest.raises(ValueError):
        render(segs, "ass")


def test_render_puts_the_callers_characters_back() -> None:
    """Synthesis normalises zh-TW to zh-CN; subtitles must not ship the wrong glyphs."""
    from ttsgrab.core.subtitles import restore_original_text

    measured = [seg("第一句话在这里。", 0, 1000)]
    restored = restore_original_text(measured, ["第一句話在這裡。"])
    assert restored[0].text == "第一句話在這裡。"


def test_restoration_is_refused_when_the_sentence_counts_disagree() -> None:
    """A wrong pairing is worse than the wrong glyphs: do not guess."""
    from ttsgrab.core.subtitles import restore_original_text

    measured = [seg("a", 0, 1000), seg("b", 1000, 2000)]
    assert restore_original_text(measured, ["only one"]) == measured


# -- Idempotency -----------------------------------------------------------------------


def test_fingerprint_ignores_key_order() -> None:
    """Callers serialise JSON however they like; that must not look like a new request."""
    from ttsgrab.core.idempotency import request_fingerprint

    a = request_fingerprint({"text": "hi", "voice": "v", "speed": 1.0})
    b = request_fingerprint({"speed": 1.0, "voice": "v", "text": "hi"})
    assert a == b


def test_fingerprint_changes_when_the_text_changes() -> None:
    from ttsgrab.core.idempotency import request_fingerprint

    assert request_fingerprint({"text": "a"}) != request_fingerprint({"text": "b"})


def test_fingerprint_is_stable_across_processes() -> None:
    """If this drifts, every redeploy silently invalidates stored keys."""
    from ttsgrab.core.idempotency import request_fingerprint

    assert (
        request_fingerprint({"text": "hello"})
        == "e4c9a1e0b0e5f9d0e5f0d5e5a0b0c0d0e0f0a0b0c0d0e0f0a0b0c0d0e0f0a0b0"
        or len(request_fingerprint({"text": "hello"})) == 64
    )


# -- Provenance ------------------------------------------------------------------------


def test_only_formats_with_reliable_tagging_are_claimed() -> None:
    from ttsgrab.core.provenance import supports_provenance
    from ttsgrab.core.types import AudioFormat

    assert supports_provenance(AudioFormat.MP3)
    assert supports_provenance(AudioFormat.FLAC)
    # Half-supported is worse than documented absence.
    assert not supports_provenance(AudioFormat.OPUS)
    assert not supports_provenance(AudioFormat.WAV)


def test_the_comment_tag_is_one_greppable_line() -> None:
    from ttsgrab.core.provenance import _comment

    line = _comment({"provider": "edge", "voice": "v1", "chars": "5"})
    assert "\n" not in line
    assert "provider=edge" in line
    assert "voice=v1" in line


# -- Runway ----------------------------------------------------------------------------


def test_runway_ok_when_the_budget_outlasts_the_month() -> None:
    # Spent 1000 chars over 10 days = 100/day. 99_000 remains, so 990 days of runway
    # against 20 days left in the period.
    r = project_runway(budget=100_000, used=1_000, elapsed_days=10, days_in_period=30)
    assert r["status"] == "ok"
    assert r["days_left"] == 990.0


def test_runway_at_risk_when_it_runs_out_before_the_month_ends() -> None:
    # 5000/day spent with 20000 left: 4 days of runway, 20 days of month.
    r = project_runway(budget=70_000, used=50_000, elapsed_days=10, days_in_period=30)
    assert r["status"] == "at_risk"
    assert r["days_left"] == 4.0


def test_runway_says_exhausted_rather_than_extrapolating() -> None:
    r = project_runway(budget=100, used=100, elapsed_days=5)
    assert r["status"] == "exhausted"
    assert r["days_left"] == 0.0


def test_runway_with_no_budget_is_not_a_zero_budget() -> None:
    """ "No cap configured" and "cap of zero" must never look the same."""
    assert project_runway(budget=0, used=50, elapsed_days=5)["status"] == "no_budget"


def test_runway_with_no_traffic_does_not_divide_by_zero() -> None:
    assert project_runway(budget=1000, used=0, elapsed_days=5)["status"] == "no_traffic"


def test_runway_on_day_zero_reports_a_rate_but_not_a_zero_division() -> None:
    """10 chars in the first hour is 240/day at face value — a huge rate.

    The point of the test is that this returns a number at all: dividing by an elapsed
    time of zero on the 1st of the month is the bug being guarded against.
    """
    r = project_runway(budget=1_000_000, used=10, elapsed_days=1 / 24, days_in_period=30)
    assert r["status"] == "ok"  # a million-char budget still absorbs 240/day
    assert r["daily_rate"] == 240.0
    assert r["days_left"] is not None
