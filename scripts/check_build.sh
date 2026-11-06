#!/usr/bin/env bash
# Assert that a running gateway reports the build it was supposed to be built from.
#
# The failure this catches is silent: an image built without the build-arg reports
# "build 0" and looks perfectly healthy. Nothing else in the test suite can see it,
# because every other check runs against source rather than against an image.
#
#   ./scripts/check_build.sh http://127.0.0.1:8300 42
set -euo pipefail

BASE="${1:-http://127.0.0.1:8300}"
EXPECTED="${2:-}"

body=$(curl -sf "$BASE/healthz")
echo "healthz: $body"

if [ -z "$EXPECTED" ]; then
  # Nothing to compare against; still require that *a* build number is present.
  python3 - "$body" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
if body.get("build") is None:
    print("no build number in /healthz", file=sys.stderr)
    raise SystemExit(1)
print(f"build number present: {body['build']}")
PY
  exit 0
fi

python3 - "$body" "$EXPECTED" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
expected = int(sys.argv[2])
got = body.get("build")
print(f"build={got} expected={expected} version={body.get('version')}")
if got != expected:
    print(f"image reports build {got}, but was built as build {expected}", file=sys.stderr)
    raise SystemExit(1)
PY
