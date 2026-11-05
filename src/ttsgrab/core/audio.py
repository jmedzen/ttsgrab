"""Audio post-processing via ffmpeg.

The gateway honours output format and sample rate *itself* when a provider cannot. When
that happens the result carries ``transcoded`` so the caller knows the audio was not
produced natively in that format.

ffmpeg is optional at import time: if the binary is missing we raise a clear error at
use time rather than at boot, so the service still starts on a bare machine.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import tempfile
from functools import lru_cache
from pathlib import Path

from ttsgrab.core.errors import ErrorCode, GatewayError, InvalidRequest
from ttsgrab.core.types import AudioFormat


class AudioProcessingError(GatewayError):
    code = ErrorCode.INTERNAL
    http_status = 500


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


@lru_cache(maxsize=1)
def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def has_ffmpeg() -> bool:
    return ffmpeg_path() is not None


_CODEC_ARGS = {
    AudioFormat.MP3: ("mp3", lambda sr, br: ["-c:a", "libmp3lame", "-b:a", br or "64k", *sr]),
    AudioFormat.WAV: ("wav", lambda sr, br: ["-c:a", "pcm_s16le", *sr]),
    AudioFormat.OPUS: ("ogg", lambda sr, br: ["-c:a", "libopus", "-b:a", br or "32k", *sr]),
    AudioFormat.FLAC: ("flac", lambda sr, br: ["-c:a", "flac", *sr]),
}


def _rate_args(sample_rate: int | None) -> list[str]:
    return ["-ar", str(sample_rate)] if sample_rate else []


async def _run_ffmpeg(args: list[str], stdin: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin)
    if proc.returncode != 0:
        raise AudioProcessingError(f"ffmpeg failed: {stderr.decode('utf-8', 'replace')[:300]}")
    return stdout


async def transcode(
    audio: bytes,
    *,
    src_format: AudioFormat,
    dst_format: AudioFormat,
    sample_rate: int | None = None,
    bitrate: str | None = None,
) -> bytes:
    """Convert between mp3/wav/opus/flac."""
    if src_format is dst_format and sample_rate is None:
        return audio
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise AudioProcessingError(
            "ffmpeg is not installed; cannot convert audio formats. "
            "Install ffmpeg or request a format the provider supports natively."
        )
    container, codec_argv = _CODEC_ARGS[dst_format]
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        *codec_argv(_rate_args(sample_rate), bitrate),
        "-f",
        container,
        "pipe:1",
    ]
    return await _run_ffmpeg(args, audio)


async def probe_duration_ms(audio: bytes, fmt: AudioFormat) -> int | None:
    """Duration in milliseconds. Uses ffprobe when present, else a cheap parse.

    ffprobe needs a seekable input for MP3 and friends: piping an MP3 into it returns an
    empty duration because the parser must seek to the Xing/ID3 header. So we hand ffprobe
    a real temporary file for every non-WAV format and keep the fast pipe path for WAV,
    which is self-describing.
    """
    probe = ffprobe_path()
    if probe is not None:
        if fmt is AudioFormat.WAV:
            duration = await _ffprobe_duration(probe, None, audio)
        else:
            duration = await _ffprobe_via_tempfile(probe, audio, fmt)
        if duration is not None:
            return duration
    return _cheap_duration_ms(audio, fmt)


async def _ffprobe_duration(probe: str, path: str | None, audio: bytes | None = None) -> int | None:
    cmd = [
        probe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "-i",
        path if path is not None else "pipe:0",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if path is None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate(audio)
    try:
        value = float(stdout.decode().strip())
    except (ValueError, AttributeError):
        return None
    return int(value * 1000) if value > 0 else None


async def _ffprobe_via_tempfile(probe: str, audio: bytes, fmt: AudioFormat) -> int | None:
    import tempfile

    suffix = {"mp3": ".mp3", "opus": ".opus", "flac": ".flac", "wav": ".wav"}.get(fmt.value, ".bin")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(audio)
        tmp = fh.name
    try:
        return await _ffprobe_duration(probe, tmp)
    finally:
        import os

        os.unlink(tmp)


def _cheap_duration_ms(audio: bytes, fmt: AudioFormat) -> int | None:
    try:
        if fmt is AudioFormat.WAV and len(audio) > 44:
            channels = struct.unpack_from("<H", audio, 22)[0]
            rate = struct.unpack_from("<I", audio, 24)[0]
            bits = struct.unpack_from("<H", audio, 34)[0]
            if channels and rate and bits:
                return int((len(audio) - 44) / (channels * rate * bits / 8) * 1000)
        if fmt is AudioFormat.MP3:
            # 48 kbps CBR is what Edge emits; good enough for a UI estimate.
            return int(len(audio) * 8 / 48_000 * 1000)
    except struct.error:
        return None
    return None


async def concat(parts: list[bytes], fmt: AudioFormat) -> bytes:
    """Join audio segments into one file, with a short cross-segment silence."""
    if not parts:
        raise InvalidRequest("nothing to concatenate")
    if len(parts) == 1:
        return parts[0]

    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        if fmt is AudioFormat.WAV:
            # PCM WAV concat is safe: keep the first header, drop the others.
            return parts[0] + b"".join(p[44:] for p in parts[1:])
        if fmt is AudioFormat.MP3:
            # Frame-level concat; players handle it, seeking is approximate.
            return b"".join(parts)
        raise AudioProcessingError(f"ffmpeg required to concatenate {fmt.value}")

    container, _ = _CODEC_ARGS[fmt]
    with tempfile.TemporaryDirectory(prefix="ttsgrab-cat-") as tmp:
        paths: list[Path] = []
        for i, blob in enumerate(parts):
            p = Path(tmp) / f"{i:04d}.{fmt.value}"
            p.write_bytes(blob)
            paths.append(p)
        list_file = Path(tmp) / "list.txt"
        list_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in paths), encoding="utf-8")
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-f",
            container,
            "pipe:1",
        ]
        return await _run_ffmpeg(args, b"")


def peak_envelope_pcm(audio: bytes, buckets: int = 400) -> list[float]:
    """Normalised RMS envelope for 16-bit PCM WAV, for the UI waveform.

    Compressed formats return an empty list; the UI decodes with WebAudio instead.
    """
    if len(audio) <= 44 or audio[:4] != b"RIFF":
        return []
    data = memoryview(audio)[44:]
    samples = len(data) // 2
    if samples == 0:
        return []
    buckets = max(1, min(buckets, samples))
    step = max(1, samples // buckets)
    out: list[float] = []
    for start in range(0, samples - step + 1, step):
        chunk = data[start * 2 : (start + step) * 2]
        acc = 0
        count = len(chunk) // 2
        if count == 0:
            continue
        for i in range(0, count * 2, 2):
            acc += abs(struct.unpack_from("<h", chunk, i)[0])
        out.append(acc / count)
    peak = max(out) if out else 0
    if peak <= 0:
        return [0.0 for _ in out]
    return [round(v / peak, 3) for v in out]


def sniff_format(data: bytes) -> AudioFormat | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return AudioFormat.WAV
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return AudioFormat.MP3
    if data[:4] == b"OggS":
        return AudioFormat.OPUS
    if data[:4] == b"fLaC":
        return AudioFormat.FLAC
    return None
