"""What is actually running.

``/healthz`` used to answer with the semantic version alone, which is identical for every
commit since the last bump. During an incident the question is never "which minor version"
— it is "is this the build with the fix". This module answers that, and degrades honestly
when the information is not there.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ttsgrab import __version__

_BUILD_FILE = Path(__file__).with_name("build_info.json")


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build: int | None
    commit: str | None
    branch: str | None
    dirty: bool
    built_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "build": self.build,
            "commit": self.commit,
            "dirty": self.dirty,
            "built_at": self.built_at,
        }

    def label(self) -> str:
        """Short form for logs and the UI footer."""
        if self.build is None:
            return self.version
        tail = f"+{self.commit}" if self.commit else ""
        suffix = "-dirty" if self.dirty else ""
        return f"{self.version} (build {self.build}{tail}{suffix})"


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    """Loaded once. ``TTSGRAB_BUILD`` overrides the number for images built without git."""
    data: dict[str, Any] = {}
    # No generated file means a source checkout, or an image built outside a git clone.
    # Better to report the version with no build than to invent one.
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_BUILD_FILE.read_text(encoding="utf-8"))

    override = os.environ.get("TTSGRAB_BUILD")
    build: int | None
    if override and override.isdigit():
        build = int(override)
    else:
        raw = data.get("build")
        build = raw if isinstance(raw, int) else None

    return BuildInfo(
        version=str(data.get("version") or __version__),
        build=build,
        commit=data.get("commit") if isinstance(data.get("commit"), str) else None,
        branch=data.get("branch") if isinstance(data.get("branch"), str) else None,
        dirty=bool(data.get("dirty", False)),
        built_at=data.get("built_at") if isinstance(data.get("built_at"), str) else None,
    )
