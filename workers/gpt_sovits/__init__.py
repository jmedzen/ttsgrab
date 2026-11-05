"""GPT-SoVITS worker — MIT code *and* MIT weights, with zero-shot cloning and MPS support.

Why this one is here
--------------------
It is the only cloning model in this repo whose licence clearly permits commercial use of
both the code and the published weights (MIT). XTTS-v2 is CPML (non-commercial), F5-TTS
weights are CC-BY-NC, Fish Speech weights are NC plus a hosted-service restriction. That
single fact is why GPT-SoVITS is the default local cloning model and the others are not
included, despite their quality.

How it runs
-----------
GPT-SoVITS ships its own API server (``api_v2.py``, default port 9880) which already
implements ``/tts``. Rather than re-implement its inference path (which changes between
releases), this worker *proxies* to that server and translates our protocol. Set
``GPT_SOVITS_API_URL`` to wherever it listens.

That is a deliberate trade-off: one extra hop, but we ride upstream's supported entry
point instead of pinning internal Python APIs that break on every minor release.

Run upstream first:
    git clone https://github.com/RVC-Boss/GPT-SoVITS
    python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml

Then:
    python -m workers.gpt_sovits
"""

from __future__ import annotations

import os

import httpx

from workers._base.runtime import TTSEngine, VoiceSpec, serve


class GPTSoVITSEngine(TTSEngine):
    model_name = "gpt-sovits-v2"
    license_note = "MIT (code and weights); cloning requires the speaker's consent"

    def __init__(self) -> None:
        super().__init__()
        self.api_url = os.environ.get("GPT_SOVITS_API_URL", "http://127.0.0.1:9880")
        # Reference clips live on the upstream box; this maps our voice_id to its files.
        # Format: voice_id=ref_path[|prompt_text[|prompt_lang]], one per line.
        self._refs = self._parse_refs(os.environ.get("GPT_SOVITS_REFS", ""))
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _parse_refs(raw: str) -> dict[str, dict[str, str]]:
        refs: dict[str, dict[str, str]] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            voice_id, path = parts[0].strip(), parts[1].strip()
            refs[voice_id] = {
                "path": path,
                "prompt_text": parts[2].strip() if len(parts) > 2 else "",
                "prompt_lang": parts[3].strip() if len(parts) > 3 else "zh",
            }
        return refs

    async def load(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.api_url, timeout=180.0)
        try:
            resp = await self._client.get("/")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"GPT-SoVITS api_v2.py is not reachable at {self.api_url}: {exc}. "
                "Start it first, or set GPT_SOVITS_API_URL."
            ) from exc

    async def voices(self) -> list[VoiceSpec]:
        # Each configured reference clip is a "voice". That is exactly what zero-shot
        # cloning means: the timbre comes from the clip, not from a trained checkpoint.
        return [
            VoiceSpec(
                voice_id=vid,
                name=f"clone:{vid}",
                languages=[ref["prompt_lang"]]
                if ref["prompt_lang"] in ("zh", "en", "ja", "ko")
                else ["zh"],
                cloning=True,
                description=f"Zero-shot clone from {os.path.basename(ref['path'])}",
            )
            for vid, ref in self._refs.items()
        ]

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
        assert self._client is not None  # set in load()

        ref = self._refs.get(voice_id)
        if ref is None:
            # Allow ad-hoc cloning: the gateway uploaded a clip inline.
            if reference_wav is None:
                raise ValueError(
                    f"unknown voice {voice_id!r} and no reference_audio supplied; "
                    f"configured voices: {sorted(self._refs)}"
                )
            return await self._clone_inline(text, reference_wav, reference_transcript, speed)

        payload = {
            "text": text,
            "text_lang": ref["prompt_lang"],
            "ref_audio_path": ref["path"],
            "prompt_text": ref["prompt_text"],
            "prompt_lang": ref["prompt_lang"],
            "text_split_method": "cut5",
            "speed_factor": speed,
            "media_type": "wav",
            "streaming_mode": False,
        }
        resp = await self._client.post("/tts", json=payload)
        if resp.status_code >= 400:
            raise ValueError(f"GPT-SoVITS returned {resp.status_code}: {resp.text[:300]}")
        return resp.content, 32000

    async def _clone_inline(
        self, text: str, reference_wav: bytes, transcript: str | None, speed: float
    ) -> tuple[bytes, int]:
        """Clone from a clip uploaded through the gateway (not preconfigured)."""
        assert self._client is not None
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(reference_wav)
            path = fh.name
        try:
            payload = {
                "text": text,
                "text_lang": "zh",
                "ref_audio_path": path,
                "prompt_text": transcript or "",
                "prompt_lang": "zh",
                "text_split_method": "cut5",
                "speed_factor": speed,
                "media_type": "wav",
                "streaming_mode": False,
            }
            resp = await self._client.post("/tts", json=payload)
            if resp.status_code >= 400:
                raise ValueError(f"GPT-SoVITS returned {resp.status_code}: {resp.text[:300]}")
            return resp.content, 32000
        finally:
            os.unlink(path)


if __name__ == "__main__":
    serve(GPTSoVITSEngine())
