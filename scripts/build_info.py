#!/usr/bin/env python3
"""Write ``src/ttsgrab/build_info.json`` from git.

Why a separate build number at all: a semantic version answers "which API am I talking
to", and it moves rarely. When someone reports a bug the question is "which build" — and
"0.1.0" is the same answer for every commit since the last bump. The build number is the
commit count, so it increases on every commit, is never reused, and needs no human input.

The file is generated, not hand-edited. If git is unavailable (a release tarball, a
Docker build context without ``.git``), the existing file is left alone rather than
replaced with a placeholder that would claim to be build 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "src" / "ttsgrab" / "build_info.json"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def main() -> int:
    count = _git("rev-list", "--count", "HEAD")
    if count is None:
        print("build_info: no git history, leaving the existing file untouched", file=sys.stderr)
        return 0 if TARGET.exists() else 1

    from ttsgrab import __version__

    dirty = bool(_git("status", "--porcelain"))
    payload = {
        "version": __version__,
        "build": int(count),
        "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty": dirty,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    TARGET.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"build_info: version {payload['version']} build {payload['build']} "
          f"({payload['commit']}{', dirty' if dirty else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
