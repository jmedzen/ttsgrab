# Worker packages.

Each subdirectory is an independent service that speaks the three-endpoint protocol in
`_base/runtime.py`. They are *not* installed as part of the `ttsgrab` Python package —
they have their own dependencies and their own containers, because a model worker needs
torch, and the gateway must never need torch.

| Worker | Licence (code / weights) | Cloning | GPU | Mandarin | Verdict |
|---|---|---|---|---|---|
| `kokoro` | Apache-2.0 / Apache-2.0 | no | optional | weak | safe default, fast on CPU |
| `cosyvoice` | Apache-2.0 / Apache-2.0 | yes | CUDA (no MPS) | strong | best licence-safe Mandarin clone |
| `gpt_sovits` | MIT / MIT | yes | CUDA or MPS | strong | best Apple-Silicon cloning |
| `piper` (see docs) | GPL-3.0 fork / mixed per-voice | no | no | zh-CN only | **not shipped** — licence mix |
| `xtts_v2` (documented) | CPML / CPML | yes | CUDA/MPS | medium | **not shipped** — non-commercial |
| `f5_tts` (documented) | MIT / CC-BY-NC | yes | CUDA | strong | **not shipped** — NC weights |
| `fish_speech` (documented) | Apache-2.0-ish / CC-BY-NC-SA | yes | CUDA | strong | **not shipped** — NC + hosted clause |

The three excluded models are documented in `docs/adding-a-provider.md` rather than
shipped, because "we found it on GitHub" is not a licence. If you need them for personal
non-commercial use, the base class makes them a 60-line addition.

## Running one

```bash
# CPU / Apple Silicon
cosyvoice  python -m workers.cosyvoice        # :8880 — will be slow on CPU
gpt_sovits python -m workers.gpt_sovits       # :8880 — needs api_v2.py upstream
kokoro     python -m workers.kokoro           # :8880 — fast everywhere

# then register it with the gateway
# Worker registration is admin-only: a scoped API key is deliberately not enough, because
# a key that could register workers could point the gateway at a host of its choosing.
curl -X POST http://gateway:8000/v1/workers \
  -H "Authorization: Bearer $TTSGRAB_SECURITY__ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gpu-box","base_url":"http://192.168.1.42:8880","kind":"tts","model_hint":"cosyvoice2"}'
```

Each worker honours `WORKER_PORT`, `WORKER_HOST`, `WORKER_CONCURRENCY`, `WORKER_LOG_LEVEL`
and `TTSGRAB_DEVICE` (`cuda` / `mps` / `cpu`).

## Protocol contract

```
GET  /health     -> 200 {"status":"ok","model":"...","device":"cuda","loaded":true}
GET  /voices     -> 200 {"voices":[{"voice_id":"...","name":"...","languages":["zh-CN"],
                                     "gender":"female","cloning":false}]}
POST /synthesize -> 200 audio/wav bytes
                    {"text","voice_id","format","sample_rate","speed","pitch",
                     "volume","style","reference_audio","reference_transcript"}
```

Anything that answers those three endpoints can be registered — including a stock OpenAI
compatible server, by registering it with `"kind": "openai_compat"`.
