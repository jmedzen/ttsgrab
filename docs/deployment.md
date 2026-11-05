# Deployment

Three shapes, in increasing order of effort. Pick the first one that fits.

| Shape | Hardware | Command |
|---|---|---|
| Single box, cloud + Edge | anything, 1 GB RAM | `make up` |
| Single box with GPU | NVIDIA GPU, 8 GB+ VRAM | `make up-gpu` |
| Gateway + GPU on different machines | two boxes | `docker compose -f docker-compose.distributed.yml up -d` on the GPU box |

---

## Single box

```bash
cp .env.example .env
make env                     # generates an admin token
docker compose --profile core up -d
```

`docker compose` with no profile starts `core` implicitly, so `docker compose up -d` works
too. The gateway publishes on `127.0.0.1:8000` — deliberately, so a default install is not
reachable from the LAN.

Data lives in named volumes:

| Volume | Contents | Back this up? |
|---|---|---|
| `ttsgrab-data` | SQLite database, history audio | **yes** — this is your history and keys |
| `ttsgrab-cache` | Synthesised audio cache | no, regenerable |
| `ttsgrab-reference` | Reference clips for cloning | **yes**, not regenerable |

```bash
docker run --rm -v ttsgrab-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/ttsgrab-data.tgz -C /data .
```

### Without Docker

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[mcp]"
cd web && npm ci && npm run build && cd ..
python -m ttsgrab --port 8000
```

A launchd/systemd unit that supervises it:

```ini
# /etc/systemd/system/ttsgrab.service
[Unit]
Description=ttsgrab TTS gateway
After=network.target

[Service]
User=ttsgrab
WorkingDirectory=/opt/ttsgrab
EnvironmentFile=/opt/ttsgrab/.env
ExecStart=/opt/ttsgrab/.venv/bin/python -m ttsgrab --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

---

## GPU workers

### What actually works on which hardware

| Worker | CUDA | Apple MPS | CPU only |
|---|---|---|---|
| Kokoro | ✓ fast | ✓ fast | ✓ usable (≈real time) |
| CosyVoice2 | ✓ good | ✗ **no MPS backend** | ✓ but 5–10× slower than real time |
| GPT-SoVITS | ✓ good | ✓ works | ✓ slow |

CosyVoice has no Metal kernels. On an Apple Silicon box it silently falls back to CPU and
the `/health` response reports `"device": "cpu"` — read it before blaming the network.

### NVIDIA runtime

```bash
# the driver and toolkit live on the host, not in the container
nvidia-smi
docker info | grep -i nvidia      # should mention nvidia

sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then:

```bash
make up-gpu
docker compose ps                 # all services should be "healthy"
```

First boot downloads weights into the `kokoro-models` / `cosyvoice-models` volumes. The
healthcheck has `start_period: 300s` (600s for CosyVoice) because of this — a `starting`
status for several minutes is normal, not a hang.

```bash
docker compose logs -f kokoro     # watch the download
```

### Register the worker with the gateway

If the worker is on the same compose network it is reachable by service name, but the
gateway does not auto-register — registration is explicit so nothing can quietly join your
routing table.

```bash
curl -X POST http://localhost:8000/v1/workers \
  -H "Authorization: Bearer $TTSGRAB_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"kokoro","base_url":"http://kokoro:8880","kind":"tts","model_hint":"kokoro"}'
```

Or use **Providers → Add worker** in the UI, which validates the URL and runs a health check
immediately.

---

## Distributed: gateway on A, GPU on B

The realistic home setup: a NAS or Mac mini runs the gateway 24/7, and the GPU lives in
another room and is powered off half the time.

**On the GPU box:**

```bash
git clone https://github.com/jmedzen/ttsgrab && cd ttsgrab
docker compose -f docker-compose.distributed.yml up -d
```

That publishes `8880` (Kokoro) and `8882` (CosyVoice) on all interfaces. **Firewall them** —
they are unauthenticated LAN services by design. Then:

```bash
# verify from the gateway host before registering
curl http://192.168.1.42:8880/health
```

**On the gateway host:**

```bash
curl -X POST http://localhost:8000/v1/workers \
  -H "Authorization: Bearer $TTSGRAB_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"gpu-box","base_url":"http://192.168.1.42:8880","kind":"tts"}'
```

### What happens when the GPU box goes away

This is the part worth understanding, because it is the difference between "nice demo" and
"usable service":

1. The monitor checks every `workers.health_interval_s` (30 s default).
2. After `workers.unhealthy_threshold` consecutive failures the node is marked unhealthy and
   the router's circuit breaker opens for it.
3. Requests stop being routed to it. In-flight requests fail and fall back.
4. The response carries `X-TTSGrab-Fallback: gpu-box->edge` so you know what happened
   instead of wondering why the voice changed.
5. When the box comes back, the next successful probe closes the breaker and routing
   resumes.

Verified behaviour, from the build log:

```
$ curl -X POST localhost:8011/v1/tts -d '{"text":"…","voice":"zh-TW-HsiaoChenNeural","provider":"stub"}'
HTTP 200
x-ttsgrab-provider: edge
x-ttsgrab-fallback: stub->edge
```

To make this work well, put a free provider (`edge`) **before** the paid ones in
`router.fallback_chain`, or use `policy: local_only` for bulk work and accept the gap when
the GPU is asleep.

---

## HTTPS

The gateway speaks plain HTTP and should stay that way; terminate TLS in front of it.
Caddy is included:

```bash
# 1. point a DNS name at this host
# 2. edit deploy/caddy/Caddyfile
```

```caddyfile
tts.example.com {
    encode zstd gzip
    reverse_proxy gateway:8000 {
        flush_interval -1      # required for /v1/tts/stream
    }
}
```

```bash
docker compose --profile core --profile tls up -d
```

`flush_interval -1` matters: without it Caddy buffers the response and streaming becomes a
single 20-second download at the end.

For Traefik, or for a host Caddy in front of Docker:

```
tts.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
```

### Hardening before you expose it

1. **Set `TTSGRAB_ADMIN_TOKEN`.** Without it, and with
   `allow_localhost_without_token: true`, a request from behind the proxy may be treated as
   local. Set both:
   ```bash
   TTSGRAB_ADMIN_TOKEN=$(openssl rand -hex 32)
   TTSGRAB_ALLOW_LOCALHOST=false
   ```
   and make sure your proxy sets `X-Forwarded-For`.
2. Set a budget: `TTSGRAB_MONTHLY_BUDGET_USD=5`.
3. Keep `security.max_text_chars` modest (default 8000).
4. Create scoped API keys per caller instead of handing out the admin token.
5. `security.allowed_worker_hosts` — pin the workers if you do not want anything else
   registrable.

---

## PostgreSQL

SQLite is fine for personal use and handles the write load here easily. Switch when you need
concurrent writers, or history larger than a few hundred thousand rows.

```yaml
services:
  db:
    image: postgres:16.4-alpine
    environment:
      POSTGRES_USER: ttsgrab
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}
      POSTGRES_DB: ttsgrab
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ttsgrab"]
      interval: 5s
      retries: 10

  gateway:
    depends_on:
      db:
        condition: service_healthy
    environment:
      TTSGRAB_DATABASE_URL: postgresql+asyncpg://ttsgrab:${POSTGRES_PASSWORD}@db:5432/ttsgrab
```

Install the driver: `pip install "ttsgrab[postgres]"`.

Tables are created on first boot (`create_all`), which is fine for a new install. For an
existing database, use the versioned migration instead — it records what has been applied,
so a later schema change is a deliberate step rather than a side effect:

```bash
alembic upgrade head      # apply; reads TTSGRAB_DATABASE_URL, so migrations and the app
alembic current           # what revision is applied
alembic downgrade -1      # back out one revision (destructive on the initial one)
```

The initial revision was generated from the SQLAlchemy metadata rather than hand-written,
so it cannot drift from the models.

Mixing engines is a one-way trip for existing data — export history with
`GET /v1/history` first if you care about it.

---

## Platform notes

### arm64

Every Python dependency has an aarch64 wheel, and the gateway image builds natively:

```bash
make build-arm64
docker buildx build --platform linux/arm64 -f deploy/Dockerfile.worker \
  --build-arg BASE_IMAGE=python:3.11.10-slim-bookworm --build-arg WORKER=kokoro \
  -t ttsgrab/worker-kokoro-cpu:0.1.0-arm64 .
```

The CUDA base image is amd64-only, so GPU workers are amd64. On an M-series Mac with
Docker Desktop, the `cpu-local` profile is the honest option — and `workers/kokoro` runs
natively on MPS if you skip Docker entirely.

### Windows

WSL2 with the Docker Desktop backend works. Without WSL, run natively with
`python -m ttsgrab` and install ffmpeg via `winget install Gyan.FFmpeg`.

### Reverse-proxy subpaths

The UI assumes it is served at `/`. A subpath deployment needs `vite build --base=/ttsgrab/`
and matching router basename; not supported out of the box.

---

## Resource sizing

| Component | RAM | VRAM | Notes |
|---|---|---|---|
| gateway | 250 MB idle, ~600 MB under load | — | ffmpeg children are the variable cost |
| Kokoro worker | 1.5 GB | ~1 GB | |
| CosyVoice2 worker | 4 GB | ~4 GB | fp16 off; `WORKER_CONCURRENCY=1` |
| GPT-SoVITS worker | 4 GB | ~4 GB | plus the `api_v2.py` process |

Cache sizing: roughly 1 MB per 10 s of mp3. The default 2 GB holds a few thousand short
clips. Raise `TTSGRAB_CACHE_MAX_BYTES` rather than disabling the cache — cache hits are what
make repeated agent calls cheap.

---

## Observability

```bash
curl localhost:8000/metrics
```

```
ttsgrab_synthesis_requests_total{provider="edge",status="ok"} 42
ttsgrab_synthesis_latency_seconds_bucket{provider="edge",le="2.0"} 40
ttsgrab_characters_total{provider="google_cloud"} 15234
ttsgrab_cost_microusd_total{provider="google_cloud"} 32000
ttsgrab_cache_ops_total{result="hit"} 17
ttsgrab_provider_up{provider="edge"} 1
ttsgrab_worker_up{worker="gpu-box"} 1
ttsgrab_http_requests_total{path="/v1/tts",status="200"} 42
```

A minimal scrape config:

```yaml
scrape_configs:
  - job_name: ttsgrab
    static_configs:
      - targets: ["gateway:8000"]
```

Logs are structured JSON in production (`TTSGRAB_LOG_FORMAT=json`) with a `request_id` on
every line and a scrubber that redacts anything shaped like a credential. `/healthz` is
liveness, `/readyz` checks the database and that at least one provider is enabled.
