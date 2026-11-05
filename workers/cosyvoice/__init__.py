"""CosyVoice2 worker — Apache-2.0 weights, good Mandarin, zero-shot cloning.

Licence: the CosyVoice2-0.5B weights are Apache-2.0 (per the FunAudioLLM/CosyVoice model
card). This makes it the best licence-safe option for *Mandarin* cloning, and the reason
it is here instead of XTTS-v2 (CPML, non-commercial).

Platform note: CosyVoice has no Metal/MPS backend — it is CUDA or CPU only. On an Apple
Silicon box it therefore runs on CPU and is slow (expect several times real time). That is
reported in ``/health`` so the gateway can route around it rather than leaving you
wondering why it is slow. Run it on the NVIDIA box for real use.

Run:
    pip install -e ".[cosyvoice]"   # or follow upstream's install notes
    python -m workers.cosyvoice --port 8882
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from workers._base.runtime import TTSEngine, VoiceSpec, floats_to_wav, serve

# Built-in speakers shipped with CosyVoice2-0.5B. Zero-shot cloning uses a reference clip
# instead, which is exposed as the "clone" path.
BUILTIN_SPEAKERS: dict[str, tuple[str, str]] = {
    "中文女": ("Mandarin female", "female"),
    "中文男": ("Mandarin male", "male"),
    "英文女": ("English female", "female"),
    "英文男": ("English male", "male"),
    "日语男": ("Japanese male", "male"),
    "粤语女": ("Cantonese female", "female"),
    "韩语女": ("Korean female", "female"),
}


class CosyVoiceEngine(TTSEngine):
    model_name = "cosyvoice2-0.5b"
    license_note = "Apache-2.0 (weights); no MPS backend — CUDA or CPU only"

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._sample_rate = 24000
        self.repo_dir = os.environ.get("COSYVOICE_DIR", "")

    async def load(self) -> None:
        import asyncio

        if self.device == "mps":
            # CosyVoice has no Metal kernels; silently pretending otherwise would just
            # produce a crash deep inside torch. Fall back and say so.
            self.device = "cpu"

        def _load():
            if self.repo_dir:
                # Upstream publishes no wheel; it is used as a source checkout.
                sys.path.insert(0, str(Path(self.repo_dir)))
            try:
                from cosyvoice.cli.cosyvoice import CosyVoice2
            except ImportError as exc:
                raise RuntimeError(
                    "CosyVoice is not installed. It has no PyPI release: clone "
                    "https://github.com/FunAudioLLM/CosyVoice and set COSYVOICE_DIR."
                ) from exc

            model_dir = os.environ.get("COSYVOICE_MODEL_DIR", "pretrained_models/CosyVoice2-0.5B")
            if not Path(model_dir).exists() and self.repo_dir:
                model_dir = str(Path(self.repo_dir) / model_dir)
            return CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)

        self._model = await asyncio.get_running_loop().run_in_executor(None, _load)

    async def voices(self) -> list[VoiceSpec]:
        voices = [
            VoiceSpec(
                voice_id=sid,
                name=name,
                languages=["zh-CN"]
                if sid.startswith(("中文", "粤语"))
                else (
                    ["en-US"]
                    if sid.startswith("英文")
                    else (["ja-JP"] if sid.startswith("日语") else ["ko-KR"])
                ),
                gender=gender,
                description="CosyVoice2 built-in speaker (no reference clip needed)",
            )
            for sid, (name, gender) in BUILTIN_SPEAKERS.items()
        ]
        voices.append(
            VoiceSpec(
                voice_id="clone",
                name="Zero-shot clone",
                languages=["zh-CN", "en-US"],
                cloning=True,
                description="Supply reference_audio + its transcript to clone a voice",
            )
        )
        return voices

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
        if voice_id == "clone" and reference_wav is None:
            raise ValueError("voice 'clone' requires reference_audio")

        import asyncio
        import tempfile

        tmp_path: str | None = None
        try:
            if voice_id == "clone" and reference_wav is not None:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                    fh.write(reference_wav)
                    tmp_path = fh.name

            import numpy as np

            def _run():
                if voice_id == "clone":
                    assert tmp_path is not None
                    chunks = self._model.inference_zero_shot(
                        text, reference_transcript or "", tmp_path, stream=False
                    )
                else:
                    chunks = self._model.inference_sft(text, voice_id, stream=False)
                out = []
                for item in chunks:
                    audio = item["tts_speech"]
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    out.append(np.asarray(audio, dtype=np.float32).reshape(-1))
                if not out:
                    raise ValueError("cosyvoice produced no audio")
                return np.concatenate(out)

            samples = await asyncio.get_running_loop().run_in_executor(None, _run)
        finally:
            if tmp_path:
                os.unlink(tmp_path)

        if volume != 1.0:
            samples = samples * float(volume)
        return floats_to_wav(samples, self._sample_rate), self._sample_rate


if __name__ == "__main__":
    serve(CosyVoiceEngine())
