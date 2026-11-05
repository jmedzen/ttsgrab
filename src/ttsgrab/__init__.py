"""ttsgrab — a unified text-to-speech gateway.

The version lives in exactly one place, ``pyproject.toml``, and is read back from the
installed package metadata. Hand-maintaining a second copy here is how ``__version__``
and the built wheel drift apart, and the one thing a version must never do is lie about
which code is running.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("ttsgrab")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
