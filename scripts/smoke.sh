#!/usr/bin/env bash
# Acceptance smoke test — the checklist from the brief, runnable against a live gateway.
#
#   ./scripts/smoke.sh [base_url]
#
# Exits non-zero on the first failure. Skips (does not fail) checks whose credentials are
# absent, because "Google is not configured" is not a defect.

set -uo pipefail

BASE="${1:-http://127.0.0.1:8300}"
# Accept both names. The settings variable is `TTSGRAB_SECURITY__ADMIN_TOKEN` (double
# underscore, as everywhere else); `TTSGRAB_ADMIN_TOKEN` is the short form this script
# documented first. Silently ignoring the documented one meant a correctly configured shell
# still ran unauthenticated and reported a wall of 401s as failures.
TOKEN="${TTSGRAB_ADMIN_TOKEN:-${TTSGRAB_SECURITY__ADMIN_TOKEN:-}}"
pass=0
fail=0
skip=0

c_reset=$'\033[0m'; c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_skip=$'\033[33m'; c_dim=$'\033[2m'

ok()   { pass=$((pass+1)); printf '%s✓%s %s\n' "$c_ok" "$c_reset" "$1"; }
bad()  { fail=$((fail+1)); printf '%s✗%s %s\n' "$c_bad" "$c_reset" "$1"; [ -n "${2:-}" ] && printf '  %s%s%s\n' "$c_dim" "$2" "$c_reset"; }
skip() { skip=$((skip+1)); printf '%s–%s %s %s\n' "$c_skip" "$c_reset" "$1" "$c_dim(skipped: $2)$c_reset"; }

auth=()
[ -n "$TOKEN" ] && auth=(-H "Authorization: Bearer $TOKEN")
# NOTE: every expansion below uses "${auth[@]+"${auth[@]}"}" because `set -u` treats an
# empty array expansion in bash < 4.4 as an unbound variable.

echo "ttsgrab smoke test → $BASE"
echo

# 1. liveness & readiness ---------------------------------------------------------------
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")
[ "$code" = "200" ] && ok "GET /healthz" || bad "GET /healthz" "HTTP $code"

ready=$(curl -s "$BASE/readyz")
if printf '%s' "$ready" | grep -q '"status":"ready"'; then
  ok "GET /readyz reports ready"
else
  bad "GET /readyz" "$ready"
fi

# 2. the catalogue has voices -----------------------------------------------------------
voices=$(curl -s "$BASE/v1/voices" "${auth[@]+"${auth[@]}"}")
count=$(printf '%s' "$voices" | grep -o '"voice_id"' | wc -l | tr -d ' ')
if [ "$count" -gt 0 ]; then
  ok "voice catalogue has $count voices"
else
  bad "voice catalogue is empty" "$(printf '%s' "$voices" | head -c 300)"
fi

# 3. Edge TTS synthesis (acceptance #1) -------------------------------------------------
edge_voice=$(printf '%s' "$voices" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for v in d.get("voices",[]):
    if v.get("provider")=="edge" and v.get("voice_id","").startswith("zh-TW"):
        print(v["voice_id"]); break
' 2>/dev/null)

if [ -n "$edge_voice" ]; then
  out=$(mktemp /tmp/ttsgrab-smoke-XXXX.mp3)
  headers=$(mktemp)
  code=$(curl -s -D "$headers" -o "$out" -w '%{http_code}' -X POST "$BASE/v1/tts" \
    "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
    -d "{\"text\":\"你好，這是 smoke test。\",\"voice\":\"$edge_voice\",\"format\":\"mp3\"}")
  bytes=$(wc -c < "$out" | tr -d ' ')
  provider=$(grep -i '^x-ttsgrab-provider:' "$headers" | tr -d '\r' | cut -d' ' -f2)
  if [ "$code" = "200" ] && [ "$bytes" -gt 1000 ]; then
    ok "POST /v1/tts synthesised $bytes bytes via ${provider:-?}"
  else
    bad "POST /v1/tts" "HTTP $code, $bytes bytes"
  fi

  # 4. cache hit (acceptance: same request is served from disk) -------------------------
  code2=$(curl -s -D "$headers" -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/tts" \
    "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
    -d "{\"text\":\"你好，這是 smoke test。\",\"voice\":\"$edge_voice\",\"format\":\"mp3\"}")
  cached=$(grep -i '^x-ttsgrab-cached:' "$headers" | tr -d '\r' | awk '{print $2}')
  [ "$cached" = "true" ] && ok "second identical request served from cache" \
    || bad "cache miss on repeat request" "x-ttsgrab-cached: ${cached:-absent}"
  rm -f "$out" "$headers"
else
  skip "Edge TTS synthesis" "no zh-TW edge voice in the catalogue"
fi

# 5. estimate (no side effects) ---------------------------------------------------------
est=$(curl -s -X POST "$BASE/v1/tts/estimate" "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
  -d '{"text":"這是一段用來估算長度的文字。","voice":"zh-TW-HsiaoChenNeural"}')
if printf '%s' "$est" | grep -q 'estimated_duration_ms'; then
  ok "POST /v1/tts/estimate returns a duration"
else
  bad "POST /v1/tts/estimate" "$est"
fi

# 6. OpenAI-compatible endpoint (acceptance #4) -----------------------------------------
code=$(curl -s -o /tmp/ttsgrab-openai.mp3 -w '%{http_code}' -X POST "$BASE/v1/audio/speech" \
  "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","input":"hello from the openai sdk","voice":"alloy","response_format":"mp3"}')
obytes=$(wc -c < /tmp/ttsgrab-openai.mp3 | tr -d ' ')
if [ "$code" = "200" ] && [ "$obytes" -gt 1000 ]; then
  ok "POST /v1/audio/speech (OpenAI shape) returned $obytes bytes"
else
  bad "POST /v1/audio/speech" "HTTP $code, $obytes bytes"
fi
rm -f /tmp/ttsgrab-openai.mp3

# 7. comparison across providers (acceptance #6) ---------------------------------------
cmp_code=$(curl -s -o /tmp/ttsgrab-cmp.json -w '%{http_code}' -X POST "$BASE/v1/compare" \
  "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
  -d '{"text":"比較測試","targets":[
        {"provider":"edge","voice":"zh-TW-HsiaoChenNeural"},
        {"provider":"edge","voice":"zh-TW-HsiaoYuNeural"},
        {"provider":"edge","voice":"zh-TW-YunJheNeural"}]}')
n=$(python3 -c '
import json
try:
    d = json.load(open("/tmp/ttsgrab-cmp.json"))
    # A target counts as successful when it produced audio rather than an error row.
    print(sum(1 for r in d.get("results", []) if not r.get("error") and r.get("bytes")))
except Exception:
    print(0)
')
if [ "$cmp_code" = "200" ] && [ "$n" -ge 3 ]; then
  ok "POST /v1/compare synthesised $n targets"
else
  bad "POST /v1/compare" "HTTP $cmp_code, $n successful targets"
fi
rm -f /tmp/ttsgrab-cmp.json

# 8. docs & agent surfaces (acceptance: MCP / llms.txt discoverable) --------------------
for path in /openapi.json /llms.txt /agents.md; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
  [ "$code" = "200" ] && ok "GET $path" || bad "GET $path" "HTTP $code"
done

# 9. metrics ---------------------------------------------------------------------------
if curl -s "$BASE/metrics" | grep -q 'ttsgrab_synthesis_requests_total'; then
  ok "GET /metrics exposes synthesis counters"
else
  bad "GET /metrics" "no ttsgrab_* series found"
fi

# 10. Google usage (acceptance #2) — needs credentials ---------------------------------
providers=$(curl -s "$BASE/v1/providers" "${auth[@]+"${auth[@]}"}")
if printf '%s' "$providers" | grep -q '"name":"google_cloud"[^}]*"enabled":true'; then
  usage=$(curl -s "$BASE/v1/usage" "${auth[@]+"${auth[@]}"}")
  if printf '%s' "$usage" | grep -q '"characters"'; then
    ok "GET /v1/usage reports characters (Google configured)"
  else
    bad "GET /v1/usage" "$usage"
  fi
else
  skip "Google Cloud usage accounting" "google_cloud not enabled"
fi

# 11. workers (acceptance #3) ----------------------------------------------------------
workers=$(curl -s "$BASE/v1/workers" "${auth[@]+"${auth[@]}"}")
nw=$(printf '%s' "$workers" | grep -o '"base_url"' | wc -l | tr -d ' ')
if [ "$nw" -gt 0 ]; then
  ok "$nw worker(s) registered"
else
  skip "worker registration" "no workers registered yet"
fi

# 12. secrets must not appear in the catalogue or docs ---------------------------------
leak=0
# Note: 'sk-' would false-positive on Slovak voice ids (sk-SK-*), so the OpenAI-style
# pattern is anchored to a key-shaped run of characters instead of the bare prefix.
for path in /v1/providers /v1/voices /llms.txt /agents.md /v1/usage; do
  body=$(curl -s "$BASE$path" "${auth[@]+"${auth[@]}"}")
  for pattern in 'AIza[0-9A-Za-z_-]\{20,\}' 'sk-[A-Za-z0-9]\{20,\}' 'BEGIN PRIVATE KEY' 'client_secret'; do
    printf '%s' "$body" | grep -q "$pattern" && { leak=1; bad "secret pattern '$pattern' found in $path"; }
  done
done
[ "$leak" = "0" ] && ok "no credential-shaped strings in public payloads"

echo
printf 'passed %s%d%s · failed %s%d%s · skipped %s%d%s\n' \
  "$c_ok" "$pass" "$c_reset" "$c_bad" "$fail" "$c_reset" "$c_skip" "$skip" "$c_reset"
[ "$fail" -eq 0 ]
