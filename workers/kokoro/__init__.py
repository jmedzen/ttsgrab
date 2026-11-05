"""Kokoro worker — Apache-2.0 weights, so it is the only local model here that is
unambiguously fine to ship and redistribute.

Licence reality check
---------------------
* Code: Apache-2.0 for the ``kokoro`` pip package and the model card repo.
* Weights: ``hexgrad/Kokoro-82M`` is Apache-2.0. This matters — several other "open" TTS
  models ship non-commercial weights, which is why they are not in this repo.
* Voicepacks in ``hexgrad/Kokoro-82M`` carry mixed provenance; the ``af_*``/``am_*``
  English packs and ``zf_*``/``zm_*`` Mandarin packs are the ones used here.

Honest quality note
-------------------
Kokoro's Mandarin is graded poorly by its own authors ("D" in the model card). It is
included because it is fast on CPU and unrestricted; for zh-TW production use Edge or
Google. The gateway surfaces this through the catalogue's quality field rather than
pretending otherwise.

Run:
    pip install "kokoro>=0.9" soundfile
    python -m workers.kokoro
"""

from __future__ import annotations

import os
from typing import ClassVar

from workers._base.runtime import TTSEngine, VoiceSpec, floats_to_wav, serve


class KokoroEngine(TTSEngine):
    model_name = "kokoro-82m"
    license_note = "Apache-2.0 (code and weights)"

    # Voice → language. Kept explicit rather than derived so the catalogue is honest.
    VOICES: ClassVar[dict[str, tuple[str, str, list[str]]]] = {
        # voice_id: (display name, gender, languages)
        "af_heart": ("Heart", "female", ["en-US"]),
        "af_bella": ("Bella", "female", ["en-US"]),
        "af_nicole": ("Nicole", "female", ["en-US"]),
        "am_michael": ("Michael", "male", ["en-US"]),
        "am_puck": ("Puck", "male", ["en-US"]),
        "bf_emma": ("Emma", "female", ["en-GB"]),
        "bm_george": ("George", "male", ["en-GB"]),
        "zf_xiaobei": ("Xiaobei", "female", ["zh-CN"]),
        "zf_xiaoni": ("Xiaoni", "female", ["zh-CN"]),
        "zf_xiaoxiao": ("Xiaoxiao", "female", ["zh-CN"]),
        "zm_yunjian": ("Yunjian", "male", ["zh-CN"]),
        "zm_yunxi": ("Yunxi", "male", ["zh-CN"]),
    }

    def __init__(self) -> None:
        super().__init__()
        self._pipeline = None
        self._sample_rate = 24000

    async def load(self) -> None:
        # Imported here so the container can start (and report /health) before the
        # model is downloaded. Lazy loading is what makes first-boot download progress
        # observable instead of a silent 3-minute hang.
        import asyncio

        def _load():
            from kokoro import KPipeline

            repo = os.environ.get("KOKORO_REPO", "hexgrad/Kokoro-82M")
            # Lang codes: 'a' American English, 'b' British, 'z' Mandarin.
            pipelines = {
                "a": KPipeline(lang_code="a", repo_id=repo),
                "b": KPipeline(lang_code="b", repo_id=repo),
                "z": KPipeline(lang_code="z", repo_id=repo),
            }
            return pipelines

        self._pipeline = await asyncio.get_running_loop().run_in_executor(None, _load)

    async def voices(self) -> list[VoiceSpec]:
        return [
            VoiceSpec(
                voice_id=vid,
                name=name,
                languages=langs,
                gender=gender,
                description="Kokoro 82M — fast, Apache-2.0; Mandarin quality is limited.",
            )
            for vid, (name, gender, langs) in self.VOICES.items()
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
        if voice_id not in self.VOICES:
            raise ValueError(f"unknown kokoro voice {voice_id!r}")
        if reference_wav is not None:
            raise ValueError("kokoro does not support voice cloning")

        lang_key = "z" if voice_id.startswith("z") else voice_id[0]
        pipeline = self._pipeline[lang_key]

        import asyncio

        def _run():
            import numpy as np

            chunks = []
            generator = pipeline(text, voice=voice_id, speed=speed)
            for _gs, _ps, audio in generator:
                if audio is None:
                    continue
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                chunks.append(np.asarray(audio, dtype=np.float32))
            if not chunks:
                raise ValueError("kokoro produced no audio")
            return np.concatenate(chunks)

        samples = await asyncio.get_running_loop().run_in_executor(None, _run)

        # Kokoro has no native gain control; apply it ourselves rather than dropping it.
        if volume != 1.0:
            samples = samples * float(volume)

        return floats_to_wav(samples, self._sample_rate), self._sample_rate


if __name__ == "__main__":
    serve(KokoroEngine())
