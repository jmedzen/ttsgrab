"""Shared worker runtime.

Every model worker (Kokoro, GPT-SoVITS, CosyVoice2) is a thin subclass of this. The point
is that the *protocol* is defined once here, so the gateway only ever talks to three
endpoints and a new model is genuinely just "add a class that loads a model".

    GET  /health     -> {"status":"ok","model":..,"device":..,"loaded":bool}
    GET  /voices     -> {"voices":[{voice_id,name,languages,gender,cloning,...}]}
    POST /synthesize -> audio bytes
                        {text, voice_id, format, sample_rate, speed, pitch, volume,
                         reference_audio (base64 WAV, optional)}

Design notes
------------
* Device is auto-detected: ``cuda`` → ``mps`` → ``cpu``. A worker never crashes just
  because the GPU it expected is not there; it degrades and says so in ``/health``.
* Model loading is lazy and single-flight: the first request loads, concurrent first
  requests wait rather than loading the model twice.
* Synthesis is serialised behind a lock (and a queue cap) because most of these models
  are not safe to run concurrently, and VRAM is the constraint, not CPU.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

logger = logging.getLogger("worker")


# --------------------------------------------------------------------------------------
# device selection
# --------------------------------------------------------------------------------------


def detect_device(prefer: str | None = None) -> str:
    """Pick a torch device: explicit env wins, then cuda, then mps, then cpu."""
    prefer = prefer or os.environ.get("TTSGAB_DEVICE") or os.environ.get("TTSGRAB_DEVICE") or ""
    if prefer:
        return prefer
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


# --------------------------------------------------------------------------------------
# WAV helpers: workers always return WAV, the gateway transcodes if it needs mp3/opus
# --------------------------------------------------------------------------------------


def pcm_bytes_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw signed-16-bit little-endian PCM in a RIFF/WAVE container."""
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def floats_to_wav(samples, sample_rate: int) -> bytes:
    """Convert a float32 numpy array in [-1, 1] into a 16-bit WAV."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    return pcm_bytes_to_wav(pcm, sample_rate=sample_rate)


def wav_duration_ms(data: bytes) -> int | None:
    if len(data) < 44 or data[:4] != b"RIFF":
        return None
    try:
        channels = struct.unpack_from("<H", data, 22)[0]
        rate = struct.unpack_from("<I", data, 24)[0]
        bits = struct.unpack_from("<H", data, 34)[0]
        if channels and rate and bits:
            return int((len(data) - 44) / (channels * rate * bits / 8) * 1000)
    except struct.error:
        return None
    return None


# --------------------------------------------------------------------------------------
# protocol models
# --------------------------------------------------------------------------------------


class VoiceSpec(BaseModel):
    voice_id: str
    name: str
    languages: list[str] = Field(default_factory=list)
    gender: str = "unknown"
    styles: list[str] = Field(default_factory=list)
    cloning: bool = False
    description: str | None = None


class SynthesizeBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    voice_id: str
    format: str = "wav"
    sample_rate: int | None = None
    speed: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0
    style: str | None = None
    # Base64 WAV of the reference clip, or a reference id the worker can resolve.
    reference_audio: str | None = None
    reference_transcript: str | None = None


# --------------------------------------------------------------------------------------
# the base engine
# --------------------------------------------------------------------------------------


@dataclass
class EngineInfo:
    model: str
    version: str = ""
    license_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class TTSEngine(abc.ABC):
    """What a model wrapper must implement. Deliberately tiny."""

    model_name: str = "unnamed"
    license_note: str = ""

    def __init__(self) -> None:
        self.device = detect_device()
        self._loaded = False
        self._load_lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            logger.info("loading %s on %s", self.model_name, self.device)
            started = time.time()
            await self.load()
            self._loaded = True
            logger.info("%s loaded in %.1fs", self.model_name, time.time() - started)

    @abc.abstractmethod
    async def load(self) -> None: ...

    @abc.abstractmethod
    async def voices(self) -> list[VoiceSpec]: ...

    @abc.abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        speed: float = 1.0,
        pitch: float = 0.0,
        volume: float = 1.0,
        style: str | None = None,
        reference_wav: bytes | None = None,
        reference_transcript: str | None = None,
    ) -> tuple[bytes, int]:
        """Return ``(wav_bytes, sample_rate)``."""
        ...

    def info(self) -> EngineInfo:
        return EngineInfo(
            model=self.model_name, license_note=self.license_note, extra={"device": self.device}
        )


def build_app(engine: TTSEngine, *, max_concurrency: int = 1) -> FastAPI:
    """Wrap an engine in the worker HTTP protocol."""
    app = FastAPI(title=f"ttsgrab worker: {engine.model_name}", version="0.1.0")
    semaphore = asyncio.Semaphore(max_concurrency)
    stats = {"requests": 0, "failures": 0, "total_ms": 0}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model": engine.model_name,
            "device": engine.device,
            "loaded": engine.loaded,
            "stats": stats,
        }

    @app.get("/voices")
    async def voices() -> dict[str, Any]:
        await engine.ensure_loaded()
        return {"voices": [v.model_dump() for v in await engine.voices()]}

    @app.post("/synthesize")
    async def synthesize(body: SynthesizeBody) -> Response:
        await engine.ensure_loaded()

        # A worker is reachable directly on the LAN, so it cannot assume the gateway already
        # validated the request. Checking here turns a confusing model-level failure into a
        # clear 4xx.
        known = {v.voice_id: v for v in await engine.voices()}
        spec = known.get(body.voice_id)
        if spec is None:
            raise HTTPException(404, f"unknown voice_id {body.voice_id!r}")
        if spec.cloning and not body.reference_audio:
            raise HTTPException(
                422,
                f"voice {body.voice_id!r} is a cloning voice and needs reference_audio "
                "(base64 WAV of the speaker to imitate)",
            )

        reference_wav = None
        if body.reference_audio:
            try:
                reference_wav = base64.b64decode(body.reference_audio)
            except Exception as exc:
                raise HTTPException(400, f"reference_audio is not valid base64: {exc}") from exc

        started = time.perf_counter()
        async with semaphore:
            try:
                audio, rate = await engine.synthesize(
                    body.text,
                    body.voice_id,
                    speed=body.speed,
                    pitch=body.pitch,
                    volume=body.volume,
                    style=body.style,
                    reference_wav=reference_wav,
                    reference_transcript=body.reference_transcript,
                )
            except ValueError as exc:
                stats["failures"] += 1
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                stats["failures"] += 1
                logger.exception("synthesis failed")
                raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

        elapsed = int((time.perf_counter() - started) * 1000)
        stats["requests"] += 1
        stats["total_ms"] += elapsed
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "X-Worker-Model": engine.model_name,
                "X-Worker-Device": engine.device,
                "X-Worker-Sample-Rate": str(rate),
                "X-Worker-Duration-Ms": str(wav_duration_ms(audio) or 0),
                "X-Worker-Latency-Ms": str(elapsed),
            },
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def serve(engine: TTSEngine, port: int | None = None) -> None:
    """Entry point used by every ``workers/<model>/__main__.py``."""
    import uvicorn

    port = port or int(os.environ.get("WORKER_PORT", "8880"))
    app = build_app(engine, max_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "1")))
    logging.basicConfig(level=os.environ.get("WORKER_LOG_LEVEL", "INFO"))
    uvicorn.run(app, host=os.environ.get("WORKER_HOST", "0.0.0.0"), port=port)
