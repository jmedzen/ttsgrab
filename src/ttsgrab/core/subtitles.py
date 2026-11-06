"""Subtitle generation (SRT / WebVTT).

Synthesis of long text already splits it into sentences and measures each chunk's
duration. Timed subtitles are therefore free — they are a re-formatting of measurements
the pipeline had to take anyway. Re-deriving them afterwards by re-analysing the joined
audio would be slower and less accurate.

Both formats are written by hand rather than via a dependency: the grammar is small, and
the only genuinely tricky parts are the timestamp format and escaping, both pinned by
tests.
"""

from __future__ import annotations

from ttsgrab.core.types import SpeechSegment

__all__ = ["MIN_CUE_MS", "render", "to_srt", "to_vtt"]


# A cue shorter than this is not readable; browsers show it for a single frame. When a
# split sentence is very short, staying on screen for its true duration produces flicker,
# so adjacent short cues are merged instead.
MIN_CUE_MS = 700


def _ts(ms: int, *, vtt: bool) -> str:
    """Format milliseconds as an SRT (comma) or WebVTT (dot) timestamp."""
    if ms < 0:
        ms = 0
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    sep = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def _merge_short(segments: list[SpeechSegment]) -> list[SpeechSegment]:
    """Merge cues below MIN_CUE_MS into their successor so nothing flickers."""
    if not segments:
        return []
    merged: list[SpeechSegment] = []
    pending: SpeechSegment | None = None
    for seg in segments:
        if pending is None:
            pending = seg
            continue
        if (pending.end_ms - pending.start_ms) < MIN_CUE_MS:
            pending = SpeechSegment(
                text=f"{pending.text} {seg.text}".strip(),
                start_ms=pending.start_ms,
                end_ms=seg.end_ms,
            )
        else:
            merged.append(pending)
            pending = seg
    if pending is not None:
        merged.append(pending)
    return merged


def _clean_text(text: str) -> str:
    # Subtitle text must not contain a blank line: it terminates the cue block.
    return " ".join(text.split())


def to_srt(segments: list[SpeechSegment]) -> str:
    """SubRip. Index starts at 1, timestamps use a comma, blocks separated by a blank line."""
    cues = _merge_short(segments)
    blocks: list[str] = []
    for i, seg in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{_ts(seg.start_ms, vtt=False)} --> {_ts(seg.end_ms, vtt=False)}\n"
            f"{_clean_text(seg.text)}"
        )
    # SRT conventionally ends with a trailing newline after the final cue.
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(segments: list[SpeechSegment]) -> str:
    """WebVTT. Requires the `WEBVTT` header and dot-separated timestamps."""
    cues = _merge_short(segments)
    blocks = ["WEBVTT", ""]
    for i, seg in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n{_ts(seg.start_ms, vtt=True)} --> {_ts(seg.end_ms, vtt=True)}\n"
            f"{_clean_text(seg.text)}"
        )
        blocks.append("")
    return "\n".join(blocks)


def restore_original_text(
    segments: list[SpeechSegment], original_chunks: list[str]
) -> list[SpeechSegment]:
    """Put the caller's own characters back into the cue text.

    The pipeline normalises Traditional to Simplified before synthesis, so the measured
    segment text is Simplified. Emitting that as subtitles would caption a Mandarin audio
    track in characters the caller never wrote — visibly wrong for a zh-TW project.

    Simplified→Traditional cannot recover the exact originals (one-to-many), so the
    splitter's own output for the *original* text is used positionally. If the two splits
    disagree in count — which would mean normalisation changed sentence boundaries — the
    measured text is kept, because a wrong pairing is worse than wrong glyphs.
    """
    if len(original_chunks) != len(segments):
        return segments
    return [
        seg.model_copy(update={"text": orig})
        for seg, orig in zip(segments, original_chunks, strict=True)
    ]


def render(segments: list[SpeechSegment], fmt: str) -> str:
    if fmt == "srt":
        return to_srt(segments)
    if fmt == "vtt":
        return to_vtt(segments)
    raise ValueError(f"unknown subtitle format {fmt!r}")
