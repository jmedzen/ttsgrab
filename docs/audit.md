# Audit log — security, correctness, memory

A record of what an audit found, what was done about it, and what remains open. Kept
because the *reasons* are worth more than the diffs, and because "we checked this already"
is only useful if you can see what was checked.

Scope: the whole gateway — API surface, provider layer, worker registry, cache, UI. Date:
2026-11-05.

---

## Fixed

### Authorisation: an API key could mint API keys

`AdminDep` was written, imported nowhere, and therefore protected nothing. Every
administrative route — `/v1/keys`, `/v1/workers`, `/v1/references`, `/v1/audit`,
`/v1/providers/{name}/refresh` — accepted *any* valid key.

The consequence is specific: a scoped key is given out precisely so it cannot do more than
it was issued for. One with `provider_allowlist: ["edge"]` and a 1 000-character quota could
`POST /v1/keys` and create an unrestricted key, then use that. The allowlist was a
suggestion.

Verified before fixing by issuing a scoped key and calling `POST /v1/keys` with it: **200**
and a new key in the response. Now 401.

### Authorisation: `X-Forwarded-For` was believed unconditionally

`_client_ip` read the leftmost `X-Forwarded-For` entry without asking whether a proxy was
configured. The client sets that header. Combined with
`allow_localhost_without_token: true` (the default), any remote caller could send
`X-Forwarded-For: 127.0.0.1` and be treated as local — which is admin.

Fixed by adding `security.trusted_proxy_hops` (default `0`, meaning the header is ignored
and the socket peer is used). When set, the entry is taken that many places from the *right*
— the side a real proxy writes — so a client-prepended value is discarded.

### SSRF: webhook URLs were fetched unchecked

`POST /v1/jobs` accepted any `webhook_url` and POSTed the job result to it. A caller could
aim it at `169.254.169.254` and read cloud credentials back out of `webhook_status`, or
probe internal services.

Now routed through the same `SsrfGuard` as worker URLs. Private targets (a webhook receiver
on the LAN, which is the common legitimate case) are opt-in via
`security.allow_private_webhooks`.

### Privacy: reference audio and history audio were unauthenticated

`GET /v1/references/{id}/audio` and `GET /v1/history/{id}/audio` returned files to anyone who
knew an id. A reference clip is a person's recorded voice — biometric-adjacent data used for
cloning. Ids are random, so this was not trivially enumerable, but "unguessable" is not an
access control.

Both now require authentication; reference audio requires admin.

### A configured limit that was never enforced

`security.max_request_body_bytes` existed, was documented, and was checked nowhere. Starlette
buffered whatever arrived. Now rejected on `Content-Length` before the body is read
(`413 request_too_large`).

Worth stating plainly: this is the failure mode to watch for in any config-driven system — a
knob that parses, validates, appears in `/v1/capabilities`, and does nothing.

### Memory: the rate limiter never forgot a caller

`SlidingWindowLimiter._hits` kept one dict entry per principal forever. 50 000 distinct keys
left 50 000 entries, none collected. Keys derive from caller-controlled input, so this is a
slow, caller-driven leak.

Buckets are now swept (at most every 5 minutes) when they have been idle longer than the
window.

### Memory/correctness: cancelled tasks were never awaited

Four sites called `task.cancel()` and moved on. Cancellation is a request, not a completion —
the coroutine may still be running against a closing event loop, which is exactly how
`RuntimeError: Event loop is closed` appears in unrelated later tests. All four now await the
cancellation.

### Correctness: the limiter could be created twice

`check_rate_limit` built the limiter lazily into `ctx.extras`. Two concurrent first requests
could each construct one, giving double the allowance and half the state. It is now a real
`AppContext` field — hidden state is state nobody audits.

### Correctness: blocking file I/O on the request path

`DiskCache.get` ran `is_file()`, `read_text()` and `utime()` synchronously inside an async
function. On a local disk that is microseconds; on an NFS/EFS-backed `/data` volume it stalls
every concurrent request. Offloaded to a thread.

### Dead code that looked like a check

`validate_upload` had an `if declared_mime ...: pass` branch commented "report a mismatch"
which reported nothing. Removed rather than left to imply a check that does not happen. The
declared MIME type is deliberately ignored; magic bytes decide.

---

## UI/UX

- Five pages (`Voices`, `Usage`, `History`, `Keys`, `Compare`) rendered `data?.x ?? []` with
  no error branch. A failed request showed an empty list — indistinguishable from "there is
  no data". `Usage` returned `null` outright, i.e. a blank page. Added a shared error state
  with a retry.
- The voice picker's popup could only be closed by choosing an option. Added Escape and
  outside-click, plus `aria-haspopup`/`aria-expanded`/`aria-labelledby`.
- Errors were announced to nobody: added `role="alert"`. Synthesis had no visible progress,
  only a changed label; added a spinner.
- The compare table scrolls horizontally on narrow screens. A scrollable region that is not
  focusable cannot be scrolled by keyboard, so it now has `tabIndex=0`, a `role`, and a name.
- `+N more` was hardcoded English in a bilingual UI. Now translated.

---

## Open

Stated so nobody assumes more than was done.

- **Live provider verification is still missing for Google Cloud, Gemini and Azure.** Their
  adapters are unit-tested against mocked HTTP; nobody has run them against the real APIs
  with real credentials. Acceptance criterion #2 remains unverified.
- **The `gpu` and `cpu-local` compose profiles have never been started.** No GPU worker has
  loaded a model. The Docker image is CI-tested; the multi-container topology is not.
  Mitigated, not solved: the worker *protocol* now has integration tests against a real ASGI
  app (`tests/integration/test_worker_protocol.py`) covering single-flight model loading,
  the concurrency cap, cloning-without-reference, base64 validation and error mapping — so
  what remains unverified is the container topology and the model weights, not the contract.
- **Docker is unavailable on the development machine**, so compose is validated only through
  CI and by reading it.
- **The API key is compared with a plain sha256 lookup, not a slow hash.** Correct for
  128-bit random tokens (brute force is infeasible, so stretching buys nothing) but wrong if
  keys ever become human-chosen. Worth revisiting only if that changes.
- **Rate limiting is per-process.** With multiple replicas, each has its own window; the
  effective limit multiplies. Documented rather than solved.
- **No CSRF protection** — unnecessary while auth is a bearer header, but the UI also stores
  its admin token in `localStorage`, which is readable by any XSS. CSP is set; a token in
  memory would be better.
- Coverage is ~76% (now measured across `workers/` too, which pulls the figure down: the
  model wrappers are imports-around-torch and cannot run without a GPU). The weakest areas
  are the worker *client* and the model wrappers themselves.
