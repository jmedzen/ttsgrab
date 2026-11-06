"""Embed provenance in the audio file itself.

Why: an mp3 that leaves this system — pasted into a video editor, sent to a client,
committed to a repo — carries no record of where it came from. Six months later nobody can
answer "which engine synthesised this, with which voice, under what terms?" without
consulting the gateway, which by then may have no such history.

Tags are written with `ffmpeg -c copy`, which rewrites only the container header. The audio
stream is bit-identical to the input; nothing is re-encoded.

Only mp3 and flac are supported, because those are the formats whose tagging ffmpeg handles
reliably through a stream copy. Opus-in-Ogg and WAV are skipped rather than half-supported:
a tag that silently fails to appear is worse than a documented absence.
"""

from __future__ import annotations

import asyncio
import logging

from ttsgrab.core.types import AudioFormat

__all__ = ["PROVENANCE_TAGS", "embed_provenance", "supports_provenance"]

_log = logging.getLogger(__name__)

# The tag names are chosen to be visible in ordinary players, not only in tooling:
# `artist` and `album` are what a file browser shows. `comment` carries the machine-
# readable line so the technical facts are still greppable.
PROVENANCE_TAGS = "ttsgrab"

_TAGGABLE = {AudioFormat.MP3, AudioFormat.FLAC}


def supports_provenance(fmt: AudioFormat) -> bool:
    return fmt in _TAGGABLE


def _comment(payload: dict[str, str]) -> str:
    """One line, key=value pairs, so `ffprobe` and `grep` both work on it."""
    ordered = ("provider", "voice", "tier", "policy", "cost_micro_usd", "chars")
    parts = [f"{k}={payload[k]}" for k in ordered if payload.get(k)]
    return " ".join(parts)


async def embed_provenance(audio: bytes, *, fmt: AudioFormat, payload: dict[str, str]) -> bytes:
    """Return the audio with provenance tags added, or the input unchanged on failure.

    Failure is non-fatal by design: slightly worse tagging is not worth failing a synthesis
    the caller already paid for. The reason is logged instead.
    """
    if not supports_provenance(fmt):
        return audio
    from ttsgrab.core.audio import ffmpeg_path

    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        return audio
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-metadata",
        f"artist={payload.get('provider', '')}",
        "-metadata",
        f"album={PROVENANCE_TAGS}",
        "-metadata",
        f"title={payload.get('title', '')[:200]}",
        "-metadata",
        f"comment={_comment(payload)}",
        "-metadata",
        f"software={PROVENANCE_TAGS}",
        "-f",
        "mp3" if fmt is AudioFormat.MP3 else "flac",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(audio)
    except OSError as exc:  # pragma: no cover - ffmpeg vanished mid-flight
        _log.warning("could not tag audio: %s", exc)
        return audio
    if proc.returncode != 0 or not out:
        _log.warning(
            "could not tag audio (%s): %s",
            fmt.value,
            err.decode("utf-8", "replace").strip()[:200],
        )
        return audio
    return out
