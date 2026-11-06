# Logic-bug audit — ttsgrab core

Scope: the eight requested files in `src/ttsgrab/core/`. Every finding below was
reproduced against this checkout (Python 3.11.16, ffmpeg/ffprobe from Homebrew) before
being written down. Findings are ordered by blast radius, not by file order.

Severity: **S1** = silent data/audio loss, wrong charge, or a control that does nothing.
**S2** = wrong output or cost under a narrower trigger. **S3** = edge case, degraded but
self-consistent.

| # | Sev | Bug | Where |
|---|---|---|---|
| 1 | S1 | FLAC concat keeps only the first chunk | `audio.py:214-215` |
| 2 | S1 | Cache key omits `loudness`/`want_segments` | `types.py:118-137` |
| 3 | S1 | Per-chunk budget check races; parallel overshoot | `synthesis.py:189-198` |
| 4 | S1 | Per-key quotas never enforced | `usage.py:269-292` |
| 5 | S1 | Unknown tier prices at the cheapest rate | `pricing.py:115-121` |
| 6 | S1 | `pricing.yaml`'s `gemini` block is an orphan | `pricing.yaml` |
| 7 | S2 | Concurrent idempotency → 500, not replay | `idempotency.py:58-73` |
| 8 | S2 | Failed `finish` is swallowed → re-charge | `api/v1/tts.py:118-119` |
| 9 | S2 | Mixed-format chunks concatenated blindly | `synthesis.py:224` |
| 10 | S2 | Cue timeline ignores MP3 padding (~70 ms/chunk) | `synthesis.py:227-232` |
| 11 | S3 | Last short cue never merged | `subtitles.py:37-58` |
| 12 | S3 | `-inf` loudness slips past the guard | `audio.py:364-369` |
| 13 | S3 | Concurrent settings saves lose an update | `configfile.py:84-111` |
| 14 | S3 | Cache `read` raises `FileNotFoundError` | `cache.py:118-119` |
| 15 | S3 | Comment-only config loses every comment | `configfile.py:92-102` |

Each entry below gives the trigger, the wrong behaviour, and a minimal fix. Section
"Suspected issues that are actually handled correctly" records the things that looked wrong
and are not.

---

## S1 findings

### 1. Long-form FLAC concatenation silently drops every chunk but the first

`core/audio.py:214-215` (`"-c", "copy"`), reachable from `core/synthesis.py:224` (`concat(parts, result.format)`).

The concat path stream-copies (`-c copy`) whatever the provider returned. For FLAC that
produces a file whose **STREAMINFO block still describes only the first input**: the correct
total-sample count is never rewritten. Players and ffprobe trust STREAMINFO, so playback
stops at the end of chunk one. The remaining audio is physically present but unreachable.

Reproduced: 6 × 1.37 s FLAC chunks through `audio.concat()` → `probe_duration_ms` = **1370 ms**
instead of 8220 ms. Decoding past 2.0 s yields 34 bytes of PCM (essentially nothing) versus
101 314 bytes for a correctly re-encoded join.

Trigger: any long-form request whose provider reports FLAC — a worker is the live path,
because `workers/client.py:40` puts FLAC in `WORKER_FORMATS`, so it is never flagged
`unsupported`. Text over `longform.chunk_threshold` (1200 in `config.example.yaml`) is enough.

Impact: the caller is billed for every chunk (metering at `synthesis.py:363` sums all chunk
results) and receives audio containing one sentence.

Minimal fix: choose the strategy per codec — stream-copy only where the container's headers
tolerate it (MP3, WAV, Ogg-Opus all verified correct), and re-encode FLAC:

```python
args = [..., "-f", "concat", "-safe", "0", "-i", str(list_file)]
if fmt is AudioFormat.FLAC:
    args += ["-c:a", "flac"]
else:
    args += ["-c", "copy"]
args += ["-f", container, "pipe:1"]
```

Verified: re-encoding FLAC through the concat demuxer yields the full 4110 ms for the same
3-chunk input that currently reports 1370 ms.

---

### 2. Cache key ignores `loudness`, so normalised and un-normalised audio collide

`core/types.py:118-137` — the `payload` dict covers text/voice/provider/format/rate/speed/
pitch/volume/ssml/style/prompt/language/ref/extra, and **omits `loudness`**, `want_segments`
and `strict`. `core/synthesis.py:120` builds the key; the cache write at `synthesis.py:380`
stores the audio *after* loudness normalisation (`synthesis.py:283-303`).

Reproduced: these four requests all hash to `d802d479f061f34c`:

| request | cache key |
|---|---|
| `loudness=None` | `d802d479f061f34c` |
| `loudness=-16.0` (podcast) | `d802d479f061f34c` |
| `loudness=-14.0` (streaming) | `d802d479f061f34c` |
| `want_segments=True` | `d802d479f061f34c` |
| `strict=True` | `d802d479f061f34c` |

Trigger: synthesize some text without `loudness`, then repeat it with
`"loudness": "podcast"`. The second request is served from cache and returns the **original,
un-normalised** audio while reporting success — directly contradicting the documented
behaviour in `audio.py:383-386` ("Raises rather than returning the input unchanged") and the
product promise in AGENTS.md that `loudness` unifies levels across providers. The reverse
ordering also holds: a −14 LUFS entry is replayed to a `podcast` (−16) caller. `speed`
correctly produces a different key, so this is an omission rather than a broken hash.

Impact: silently wrong loudness — the one thing the parameter exists to guarantee. Mixing
providers is exactly the workflow AGENTS.md recommends `loudness` for, so the collision lands
on the intended use.

Minimal fix:

```python
payload = {
    ...,
    "ld": self.loudness,
    # want_segments changes the split, hence the audio boundaries:
    "ws": self.want_segments,
}
```

`strict` need not be added — it only changes error behaviour, not bytes.

A second, independent defect in the same area: the cache-hit branch
(`synthesis.py:126-138`) constructs a fresh `SynthResult` and never sets `segments`, and the
cache write at `synthesis.py:387-392` does not persist them. So a replayed request that
passed `"subtitles": "srt"` with `"response": "json"` returns `subtitles: null` with no
error, because the refusal at `api/v1/tts.py:101-113` only fires when `response == "audio"`.
Callers silently lose captions on any repeat request. Fix: persist `segments` in the cache
meta and restore them on hit, or refuse both response modes when `want_segments` is set and
no segments exist.

---

### 3. Per-provider budget is checked per chunk against a stale snapshot — parallel chunks overshoot the cap

`core/synthesis.py:189-198` fans chunks out with `asyncio.gather` (bounded by
`longform.max_concurrency`, default 4), and each chunk independently reaches
`core/router.py:238-247`, which calls `usage.check_budget` inside its own session.
`core/usage.py:230-270` reads `month_summary` and compares `used_chars + characters`
against the cap. Nothing is written until the chunk finishes, so **N concurrent chunks all
read the same `used_chars`** and all pass.

Reproduced: 10 concurrent 200-char chunks against a 1000-char monthly cap →
**10 allowed, 0 blocked**, i.e. 2000 chars spent against a 1000 cap (2× overshoot, and it
scales with `max_concurrency`).

Note this is not the "budget checked twice" duplication (that part is harmless and even
intentional, per the comment at `router.py:233-235`). The defect is that the check is
read-only and therefore not a reservation. `check_budget` is also called once *before* the
split at `synthesis.py:78-85` with `len(text)`; the per-chunk checks then see the same
unchanged total, so neither call prevents the parallel overshoot.

Impact: `402 budget_exceeded` is documented as a hard financial stop (AGENTS.md). Under
long-form parallel synthesis it is not one — the overshoot is bounded only by
`max_concurrency × max_sentence_chars` per request, per concurrent request.

Minimal fix: make the check-and-reserve atomic. Either serialise chunk admission behind a
lock and add a pending-reservation total that `check_budget` includes:

```python
# orchestrator, once per request, before gather:
async with self._budget_lock:
    await self.usage.check_budget(session, provider=name, tier=tier,
                                  characters=len(text))
    self._pending += len(text)   # and subtract in a finally
```

…or simply skip the per-chunk re-check and enforce once for the whole request (the
`synthesis.py:80` call already does that with the full length) — the per-attempt re-check
only exists to catch a fallback to a *different* provider, so it should compare against that
provider's cap using the request-total, not the chunk.

---

### 4. Per-key character and cost quotas are stored, exposed, documented — and never enforced

`core/usage.py:269-292` defines `check_key_quota`, which raises `QuotaExceeded`. Grepping
the whole tree for callers returns **none**: the only hits are the definition, the exception
class (`core/errors.py:168`), the column (`db/models.py:93`), the API writer
(`api/v1/keys.py:55`) and the principal loader (`api/deps.py:98`). `monthly_char_quota` and
`monthly_cost_quota_micro_usd` are both accepted by `POST /v1/keys` (`api/schemas.py:175-176`)
and asserted nowhere.

Trigger: create a key with `monthly_char_quota: 1000`, synthesize 10 000 characters. All
requests succeed. `QuotaExceeded` is impossible to raise from any request path.

Impact: a documented customer-facing control (`quota_exceeded`, retryable:false, in
AGENTS.md's error table) silently does nothing. An operator who sets a quota to contain a
noisy tenant gets no enforcement and no warning. The cost quota is worse — it is a spend
control that never spends a check.

Minimal fix: call it where the budget is checked, in the same session, so the two share a
snapshot:

```python
# core/router.py, next to check_budget
if self.usage is not None and api_key_id:
    await self.usage.check_key_quota(
        budget_session, api_key_id=api_key_id,
        characters=len(req.text or ""), quota=principal.monthly_char_quota,
    )
```

(Requires threading the quota through from the principal; `check_key_quota`'s signature
already takes it.) The cost quota needs a matching branch — `check_key_quota` currently has
no cost parameter at all.

---

### 5. Unknown voice tier resolves to the *cheapest* price tier, so costs collapse to the free allowance

`core/pricing.py:115-121`:

```python
def tier(self, provider, tier):
    if tier is not None and (provider, tier) in self._tiers:
        return self._tiers[(provider, tier)]
    for (prov, _name), spec in self._tiers.items():
        if prov == provider:
            return spec          # <-- first tier in insertion order wins
    ...
```

When the tier is unknown, the fallback is "the first tier registered for this provider",
which is whatever the pricing table lists first. For `google_cloud` that is `Standard`
($4.00/M, **4 000 000 free chars**) — not the most expensive and not the one actually used.

This matters because `tier` legitimately arrives as `None`: `router._tier_for`
(`router.py:127-131`) returns `None` whenever the voice is not in `cached_voices`, and
`router.py:239-241` uses exactly that lookup for the budget check. `google_cloud`'s
`cached_voices` is only populated by `refresh_voices()`, which runs at startup
(`core/registry.py:116-128`) and can legitimately fail or still be in flight (the warning is
swallowed at `core/registry.py:126`).

Reproduced:

| characters | tier | correct cost | cost with `tier=None` |
|---|---|---|---|
| 3 000 000 | `Chirp3-HD` ($30/M, 1M free) | $60.00 | **$0.00** |
| 5 000 000 | `Studio` ($160/M, 1M free) | $640.00 | **$4.00** |

In the first row `Standard`'s free allowance (4M) exceeds the 3M requested, so the request
is declared entirely free. In the second, 4M of the 5M chars are absorbed by that allowance
and the remaining 1M bill at $4/M. The undercount is 160× for `Studio` and unbounded for any
request under 4M characters.

Impact: understated spend, `within_free_tier` reported true on paid audio
(`api/v1/tts.py:487` on the estimate endpoint), and — because
`check_budget` compares `used_usd + cost` against the cap (`usage.py:256`) — a $0 cost makes
the USD cap unreachable. This is the one finding that can let real money be spent while the
budget UI shows nothing.

Minimal fix: never let an unknown tier price at the cheapest rate. Either raise, or fall
back to the provider's *maximum* rate so the estimate errs toward refusing:

```python
def tier(self, provider, tier):
    if tier is not None and (provider, tier) in self._tiers:
        return self._tiers[(provider, tier)]
    candidates = [s for (prov, _), s in self._tiers.items() if prov == provider]
    if candidates:
        # Unknown tier: assume the most expensive one rather than the first listed.
        # Under-pricing an unknown tier silently disables the budget cap.
        return max(candidates, key=lambda s: s.usd_per_mchar)
    return PriceTier(provider=provider, tier=tier or "unknown", usd_per_mchar=0.0,
                     free_chars_per_month=0)
```

Better still, make `_tier_for` fall back to `tier_from_voice_id` (which `google_cloud.py:62`
already implements and defaults to `Standard` *correctly* for that provider) instead of
returning `None`.

---

### 6. `pricing.yaml`'s `gemini` block is an orphan — edits to it do nothing

`pricing.yaml` lists rates under `gemini:`, but the provider registers as
`google_ai_studio` (`providers/google_ai_studio.py:111`). No provider is ever named
`gemini`. Confirmed by grep: no occurrence of the bare string `"gemini"` outside
`gemini-tts` in `src/`.

Reproduced: load `pricing.yaml`, raise `tiers.gemini.gemini-tts.usd_per_mchar` from 12.0 to
120.0, and `compute_cost_micro_usd(provider="google_ai_studio", tier="gemini-tts",
characters=1_000_000)` still returns **12 000 000 µUSD ($12.00)** — the effective rate is
unchanged at 12.0. The default in `pricing.py:61` (keyed `google_ai_studio`) is what
actually applies.

Trigger: an operator follows the file's own header ("Edit this file to match the rates you
actually pay") and corrects the Gemini rate. Nothing changes, and nothing says so.

Impact: a 10× mispricing that the operator believes they fixed. The `gemini-tts` tier name
is right, so the entry *looks* wired up; only the provider key is wrong.

Minimal fix: rename the block in `pricing.yaml` to `google_ai_studio:` (the `PriceTier`
already carries `tier: gemini-tts`, so nothing else changes), or add a load-time assertion
that every provider key in the table matches a registered provider name, which would have
caught this.

---

## S2 findings

### 7. Concurrent `Idempotency-Key` requests raise `IntegrityError` → 500 instead of replaying

`core/idempotency.py:58-73`: `begin` does `session.get`, then `session.add` + `flush`. Two
requests with the same key in flight both see `row is None`, both insert, and the second
violates the `(key, api_key_id)` unique constraint at flush. `api/v1/tts.py:80-83` only
catches `IdempotencyConflict`, so the `IntegrityError` escapes to the generic handler
(`main.py:215-224`) as **HTTP 500, `retryable: true`**.

Reproduced: 4 concurrent `begin()` calls with one key → 1 claimed, **3 × IntegrityError**.

Trigger: this is the exact scenario the feature exists for — an SDK or load balancer
retries while the first attempt is still running. The client is told "retryable server
error" and retries again.

Impact: a 500 with `retryable: true` on a request that is, by design, safe to retry;
clients that honour the flag can loop. Not a double charge (the first request owns the
insert), but the retry contract is broken exactly where it matters.

Minimal fix: treat the constraint violation as "someone else claimed it":

```python
async with session_scope() as session:
    row = await session.get(IdempotencyRecord, (key, scoped))
    if row is None:
        try:
            session.add(IdempotencyRecord(...))
            await session.flush()
            return None
        except IntegrityError:
            await session.rollback()
            row = await session.get(IdempotencyRecord, (key, scoped))
    ...
```

(The outer `session_scope` already rolls back on exception, so the retry needs its own
short session rather than continuing on a poisoned one.)

### 8. A `pending` idempotency row replays nothing and re-charges

`core/idempotency.py:78-83`. When a row is found with `status == "pending"` (the process
died between `begin` and `finish`), `begin` returns `None`, so the caller re-synthesises.
The docstring calls this deliberate, and between "replay nothing" and "charge again" it
picks the lesser evil — that reasoning is sound and I am **not** flagging the choice.

What is a real defect is the pairing with `api/v1/tts.py:118-119`:

```python
if idem_key:
    with contextlib.suppress(Exception):
        await finish(key=idem_key, ...)
```

`finish` failing is swallowed entirely. `history_id` is captured *after* the audio is
persisted, so if `finish` throws (DB hiccup, lock timeout), the row stays `pending` for the
full 24 h retention while the client already has its audio. Every subsequent retry with
that key re-synthesises and re-charges. The suppression guarantees the failure is invisible:
no log, no metric, no header.

Trigger: transient SQLite lock or disk-full at `finish` time. Reproduced the swallowed
exception directly.

Minimal fix: log it at minimum, and prefer failing loudly over silent re-charging:

```python
if idem_key:
    try:
        await finish(key=idem_key, api_key_id=principal.id,
                     history_id=outcome.history_id)
    except Exception:
        logger.exception("idempotency finish failed; key %s left pending", idem_key)
```

### 9. Mixed-format chunks are concatenated as if uniform, producing garbage

`core/synthesis.py:224` calls `concat(parts, result.format)`, where `result` is the first
chunk's result — so the **first** chunk's format is imposed on all of them. Nothing checks
that the other chunks agree.

Reproduced with a WAV chunk and an MP3 chunk through `audio.concat(..., AudioFormat.WAV)`:
it returned 54 414 bytes with **no error**. Reaching it: a long-form request where chunk 1
is served by `google_ai_studio` (WAV/MP3) and chunk 2 falls back to `edge` (MP3 only) — each
chunk runs `Orchestrator.synthesize` independently, so `providers_used` differing across
chunks is the *expected* outcome of a fallback, not an exotic one.

Also reproduced the subtler same-container case: MP3 chunks at 24 kHz and 48 kHz
(fallback between `edge` at 24 kHz and a worker at 48 kHz) → joined duration 9.056 s for
2.0 s of input, with `non monotonically increasing dts` warnings. The failure is silent in
both cases; the caller gets a corrupt file with a 200.

Minimal fix: after the gather, assert uniformity and transcode rather than trusting chunk
one:

```python
formats = {r.format for r, _ in results}
if len(formats) > 1 or len({r.sample_rate for r, _ in results}) > 1:
    # Transcode each chunk to the first chunk's format/rate before joining.
    parts = [await transcode(r.audio, src_format=r.format, dst_format=result.format,
                             sample_rate=result.sample_rate) for r, _ in results]
```

### 10. Subtitle timings omit MP3 encoder padding, drifting ~70 ms per chunk

`core/synthesis.py:227-232` builds the cue timeline by summing each chunk's
`probe_duration_ms` and treating the total as the joined-audio timeline. For MP3 the join
adds encoder delay/padding at every chunk boundary that the per-chunk probe does not see.
Reproduced, 1.37 s chunks joined with `concat()`:

| chunks | cue timeline | actual audio | drift |
|---|---|---|---|
| 3 | 4110 ms | 4320 ms | 210 ms |
| 6 | 8220 ms | 8640 ms | 420 ms |
| 12 | 16440 ms | 17280 ms | 840 ms |

WAV is exact (0 ms drift); OPUS is within ~30 ms. MP3 — the default format, and what `edge`
always emits — drifts linearly at roughly **70 ms per chunk**. The code comment at
`synthesis.py:225-226` says durations are accumulated "so subtitle timings come out of
measurements we already have", which is the assumption that fails.

Impact: captions desynchronise progressively; at 12 chunks the last cue appears 0.84 s early.

Minimal fix: after `concat`, probe the joined audio and rescale the cue timeline
proportionally, rather than trusting the sum:

```python
joined_ms = await probe_duration_ms(merged, result.format)
total = sum(max(1, s.end_ms - s.start_ms) for s in segments)
if joined_ms and total:
    scale = joined_ms / total
    segments = [s.model_copy(update={"start_ms": round(s.start_ms * scale),
                                     "end_ms": round(s.end_ms * scale)})
                for s in segments]
```

---

## S3 findings

### 11. Last short cue is never merged

`core/subtitles.py:37-58`. `_merge_short` merges a short cue forward into its successor, but
a short *final* cue has no successor: the loop's tail (`if pending is not None:
merged.append(pending)`) appends it unchanged.

Reproduced: `[A 0→1200, B 1200→2400, C 2400→2600]` → unchanged, so the 200 ms cue `C` is
emitted despite `MIN_CUE_MS = 700`. All-short input does merge correctly (`A B` → 0→400).

Impact: one flickering final caption. Cosmetic, but it is the exact symptom `MIN_CUE_MS`
exists to prevent.

Minimal fix: after the loop, if the final `pending` is short and `merged` is non-empty,
merge it backward instead of appending:

```python
if pending is not None:
    if merged and (pending.end_ms - pending.start_ms) < MIN_CUE_MS:
        prev = merged.pop()
        merged.append(prev.model_copy(
            update={"text": f"{prev.text} {pending.text}".strip(),
                    "end_ms": pending.end_ms}))
    else:
        merged.append(pending)
```

### 12. `-inf` loudness measurement slips past the guard and fails in the second pass

`core/audio.py:364-369` validates the four `loudnorm` fields with `float()`. On true silence
ffmpeg reports `"input_i" : "-inf"`, and `float("-inf")` **succeeds**, so the guard that
exists to catch unmeasurable audio does not fire. Control then reaches the second pass with
`measured_I=-inf`, which ffmpeg rejects: `Value -inf for parameter 'measured_I' out of range
[-99 - 0]` → `rc=222`, 0-byte output.

Reproduced end-to-end on a silent WAV with `loudness=-16`.

How bad this is depends on the path, and it is less severe than it first looks: both routes
end in `AudioProcessingError` → HTTP 500 (`audio.py:24-26`), so this is **not** a
"success returned on failure" bug. The real defects are narrower:

1. **The intended diagnostic never runs.** The `measured is None` branch
   (`audio.py:393-397`) is written precisely for this input and says "too short or silent;
   omit the loudness parameter for this input" — a message a caller can act on. Instead the
   caller gets ffmpeg's `Value -inf for parameter 'measured_I' out of range [-99 - 0]`,
   which describes the operator's toolchain, not their audio. `MIN_MEASURABLE_MS`
   (`audio.py:282`) is the constant that was supposed to gate this and is never referenced.
2. **The failure is late and unrecorded.** Normalisation runs at `synthesis.py:283-303`,
   *after* the paid provider call but *before* metering (`synthesis.py:363`) and the cache
   write (`synthesis.py:379`). So the upstream characters are really spent, no `UsageEvent`
   is written for them, and nothing is cached — the caller gets a 500, the ledger shows no
   charge, and a retry pays again. A silent-input check belongs with the pre-spend budget
   check at `synthesis.py:80`, before any provider is called.

Trigger: a request that is essentially all silence with `"loudness": "podcast"` — e.g. text
that normalises to punctuation only, or a provider that renders an unpronounceable string as
silence.

Minimal fix: reject non-finite values, not just non-numeric ones, so the purpose-built error
message fires:

```python
import math
for field in ("input_i", "input_tp", "input_lra", "input_thresh"):
    try:
        if not math.isfinite(float(data[field])):
            return None          # silent input: fall through to the actionable error
    except (KeyError, TypeError, ValueError):
        return None
```

`measure_integrated_lufs` (`audio.py:428-467`) has the same hole and will return `-inf` to the
A/B page; the same one-line guard fixes both.

### 13. Concurrent settings saves lose an update

`core/configfile.py:84-111` is read-modify-write with no lock: `set_values` loads the file
(92-98), mutates the in-memory doc (104-105), and writes it atomically (111). The atomicity
of `_atomic_write` protects against a *torn* file, but not against a lost update — two
saves that overlap both read the same base, and the second write silently discards the
first's key.

Reproduced (two threads, `_atomic_write` slowed to model a slow `/data` volume):
`cache.ttl_seconds: 111` + `cache.max_bytes: 222` → final file has `ttl_seconds: 2592000`
(the old value) and `max_bytes: 222`. **`ttl_seconds` is silently lost.** Comments are all
preserved (96/96), so the corruption is invisible in a diff.

Trigger: two admins on the settings page at once, or one double-clicking Save; each `PUT
/v1/settings` call is its own read-modify-write.

Impact: a setting the operator saved reverts with no error. Relative to a corruption bug
this is mild — the file stays valid — but it is the failure mode the module's docstring
claims to prevent ("a crash mid-write must not leave a truncated config").

Minimal fix: serialise writes with a process-local lock and a re-read inside it (a
single-file, single-process product does not need file locking):

```python
import asyncio, threading
_write_lock = threading.Lock()   # or asyncio.Lock if set_values becomes async

def set_values(path, updates):
    with _write_lock:
        ...  # existing load → assign → dump → _atomic_write
```

### 14. Cache `read` can raise `FileNotFoundError` out of `synthesize`

`core/cache.py:58-74` (`get` stats and returns an entry) and `core/cache.py:118-119`
(`read` opens the path) are separate steps with no lock between them. `evict` (135-162)
runs under `self._lock`, but readers never take it, and `put` (115-116) triggers eviction
whenever the total exceeds the budget.

Reproduced: `get()` returns a valid entry, the file is then removed, `read()` raises
`FileNotFoundError`, which is not caught anywhere in the cache-hit branch
(`synthesis.py:122-124`) and surfaces as a 500.

Trigger: a request reading a cache entry while a concurrent `put` pushes the cache over
`max_bytes` and evicts the entry being read. Narrow, but it is a real interleaving on the
hot path, and the entry chosen for eviction is the LRU one — which under load is often the
one just touched.

Minimal fix: treat a vanished entry as a miss.

```python
async def read(self, entry: CacheEntry) -> bytes | None:
    try:
        return await asyncio.to_thread(entry.path.read_bytes)
    except OSError:
        return None
```

and in `synthesis.py`, `blob = await ctx.cache.read(entry); if blob is None: <fall through
to synthesis>`.

### 15. A comment-only config is rewritten from scratch, losing every comment

`core/configfile.py:92-102`. `set_values` loads the file; a document containing only comments
loads as `None`, and `if doc is None: doc = {}` (101-102) then builds a fresh document. The
dump at 107-111 writes *only* the requested keys — every comment in the original is gone,
including the header that documents every other option.

Reproduced: a file containing just `# ── gateway …` became exactly
`router:\n  default_policy: cheapest\n` after one `set_values` call.

Trigger: an operator comments out their whole config (to start from defaults, or while
debugging) and then saves any setting from the UI. Note the guard at 93-98 deliberately
refuses to overwrite *invalid* YAML to protect a half-edited file — the comment-only case is
valid YAML with a `None` document, so it slips past that intent entirely.

Impact: total, silent loss of the annotated documentation the round-trip loader exists to
protect. The resulting file is still valid, so nothing errors; the operator finds their
comments gone.

Minimal fix: distinguish "no document" from "no content" — append the new keys to the
original text when the raw content is non-empty but parses to `None`:

```python
raw = path.read_text(encoding="utf-8") if path.is_file() else ""
doc = _yaml().load(raw) if raw.strip() else None
if doc is None:
    if raw.strip():
        # Comment-only document: keep the text and append rather than dumping a fresh one.
        doc = _yaml().load(raw + "\n" + "\n".join(
            f"{k.split('.')[0]}: {{}}" for k in updates))
    else:
        doc = {}
```

(after which the normal `_assign` merge runs as usual). Verified: with the placeholder
approach the comment-only file keeps its header and gains the key cleanly:

```
# ── gateway ──        →    # ── gateway ──
# providers:               # providers:
#   edge: on               #   edge: on
                           router:
                             default_policy: cheapest
```

---

## Suspected issues that are actually handled correctly

These were on my list and each one is fine — recording them so the negative result is
explicit.

- **Month boundary arithmetic** (`pricing.py:178-182`). `month_bounds` is correct at the
  year boundary: `2026-12-31 23:30Z` → `start 2026-12-01`, `end 2027-01-01`. The
  `+32 days` then `.replace(day=1)` idiom handles December and February properly.
  `datetime.replace` keeps the `UTC` tzinfo, which is what the SQL comparisons need.
- **Timezone consistency in usage queries.** Timestamps are stored via `_utcnow()` with
  `DateTime(timezone=True)` (`db/models.py:28-29, 88, 104`), compared against `month_bounds`
  UTC values. I inserted rows at `11-30 23:59:59.999999Z`, `12-01 00:00:00Z`,
  `12-15`, `01-01 00:00:00Z` and the November/December sums were exactly 10 and 60 — the
  `>= start, < end` bounds are half-open with no double count or gap. (SQLite drops the
  tzinfo on read, but both sides are UTC, so the comparison is consistent.)
- **`TTL` boundary** (`cache.py:68`). `>` rather than `>=` means an entry exactly at the TTL
  is still served, which is the conventional reading of "expires after N seconds"; and
  `self.ttl_seconds` being falsy disables expiry entirely, as intended.
- **Cache key collisions from sharding** (`cache.py:53-55`). Two-level shard on
  `key[:2]/key[2:4]` with the full key as the filename; the filename is complete, so
  sharding cannot collide. (The *content* collision in finding 2 is a different problem —
  missing inputs, not a path bug.)
- **Torn cache writes** (`cache.py:113-114`). `.bin` then `.json` is not atomic as a pair, but
  a reader that sees only `.bin` gets `(False, None)` from `_read_meta` (77-83) and treats it
  as a miss. A reader that sees a half-written `.json` gets `JSONDecodeError` → `(True, None)`
  → also a miss. No corrupt audio is ever served.
- **`configfile` comment preservation and secret masking.** On a real
  `config.example.yaml`, `set_values({"google_cloud.enabled": True})` preserved 96/96
  comments, wrote mode `0o600`, and merged the nested block correctly — `isinstance(child,
  dict)` at `configfile.py:123` is `True` for ruamel's `CommentedMap`, so the "nested merge
  might flatten to a plain dict" suspicion is wrong. `mask`/`mask_tree` correctly handle
  list members (`free_api_keys`) as well as scalars.
  (The one `configfile` data-loss path I did find is separate and narrower: see finding 15.)
- **`providers[].tier` reporting** (`synthesis.py:348-352`). The tier is re-derived from the
  *serving* provider's voice list after fallback, rather than trusting the requested
  provider's — which correctly avoids billing a fallback at the original provider's rate.
  The remaining gap is only the `None` case in finding 5.
- **`concat` of a single chunk** (`audio.py:181-182`) returns the input untouched — no
  pointless re-encode.
- **`restore_original_text` count mismatch** (`subtitles.py:106-107`). Returning the measured
  segments unchanged when the two splits disagree is the right call, and the reasoning in
  the docstring (a wrong pairing is worse than wrong glyphs) holds.

## Dead settings (documented as functional, wired to nothing)

- **`longform.pause_ms_between_sentences`** (`settings.py:204`, documented at
  `config.example.yaml:105-106` as "Silence inserted between sentences when concatenating")
  is **never read anywhere in `src/`**. Confirmed by grep: the only hits are the field
  definition and the example config. `concat`'s docstring (`audio.py:178`) makes the same
  promise — "with a short cross-segment silence" — and inserts no silence: I verified a
  2×1000 ms join measures 2112 ms with `silencedetect` reporting no gap at all. So the
  feature is doubly absent: the setting is unread and the code has no insertion step. A
  sentence boundary therefore joins with zero pause, which is audibly clipped for plosives.
  Either implement it (`concat(..., pause_ms=...)` inserting `apad`/silence between inputs)
  or remove the setting and the docstring claim.
- **`MIN_MEASURABLE_MS`** (`audio.py:282`) is defined, documented ("Below this, a single-pass
  measurement is not trustworthy enough to derive gain from"), and never referenced. The
  guard it describes does not exist — which is directly related to finding 12: short/silent
  input reaches the second `loudnorm` pass and fails there instead of being screened here.

## Note on the ffprobe WAV path

`probe_duration_ms` (`audio.py:101-117`) documents a "fast pipe path for WAV, which is
self-describing", but ffprobe over `pipe:0` returns `N/A` for WAV — so `_ffprobe_duration`
returns `None` and **every WAV duration actually comes from `_cheap_duration_ms`**
(`audio.py:161-174`). I verified this for both plain and metadata-bearing WAVs, and it is
why WAV timings are exact: the arithmetic path reads the real `channels`/`rate`/`bits` from
the header. Two caveats worth knowing rather than fixing:

- The `len(audio) - 44` assumption is wrong for WAVs with extra chunks before `data`. A file
  with a 1474-byte `LIST` chunk reported **2029 ms for 2000 ms** of audio (~1.5% high). All
  provider WAVs I sampled put `data` at 70-94 bytes, so the bias is ~1-2 ms in practice —
  negligible, but it is why the arithmetic cannot simply be trusted for arbitrary input.
- `_cheap_duration_ms`'s MP3 branch (169-171) assumes 48 kbps CBR ("what Edge emits"). It is
  only reached when ffprobe is absent, but then it is wrong for any other bitrate.

Neither is worth changing on its own; they are documented here because they explain why the
"ffprobe is used when present" comment overstates what happens for WAV.
