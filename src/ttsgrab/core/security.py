"""Security helpers: API-key auth, rate limiting, SSRF guards, upload validation."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ttsgrab.core.errors import Forbidden, InvalidRequest

KEY_PREFIX = "ttsg_"


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, sha256_hash, display_prefix)``. Store only the latter two."""
    raw = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw), raw[:12]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Principal:
    kind: str  # "api_key" | "admin" | "anonymous"
    id: str | None = None
    name: str = "anonymous"
    provider_allowlist: list[str] | None = None
    rate_limit_per_minute: int | None = None
    monthly_char_quota: int | None = None

    def allows_provider(self, provider: str) -> bool:
        if not self.provider_allowlist:
            return True
        return provider in self.provider_allowlist


class SlidingWindowLimiter:
    """In-process per-principal rate limiter.

    Deliberately in-memory: a single-container gateway does not need Redis, and per-key
    DB counters would add a write per request. Restarting resets the window, which is
    acceptable for this scale.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()
        # A bucket that has gone quiet is dead weight forever otherwise: one dict entry per
        # principal that ever made a request. Unbounded growth, driven by the caller.
        self._sweep_interval_s = 300.0

    def _sweep(self, now: float, window_s: float) -> None:
        if now - self._last_sweep < self._sweep_interval_s:
            return
        self._last_sweep = now
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > window_s]:
            del self._hits[key]

    def check(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        self._sweep(now, window_s)
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window_s:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window_s - (now - bucket[0])) + 1
            return False, retry_after
        bucket.append(now)
        return True, 0

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class SsrfGuard:
    """Validate worker ``base_url`` values.

    LAN workers are the point of the feature, so private ranges are allowed by default —
    but link-local (169.254/16, cloud metadata) and loopback-beyond-localhost are not,
    and an explicit allow-list can narrow it further.
    """

    BLOCKED_NETWORKS = (
        ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata
        ipaddress.ip_network("fe80::/10"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("0.0.0.0/8"),
    )

    def __init__(self, *, allow_private: bool = True, allowed_hosts: list[str] | None = None):
        self.allow_private = allow_private
        self.allowed_hosts = set(allowed_hosts or [])

    def validate(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise InvalidRequest(f"worker base_url must be http(s), got {parsed.scheme!r}")
        if not parsed.hostname:
            raise InvalidRequest("worker base_url has no host")

        host = parsed.hostname
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise Forbidden(f"host {host!r} is not in security.allowed_worker_hosts")

        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise InvalidRequest(f"cannot resolve worker host {host!r}: {exc}") from exc

        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if any(addr in net for net in self.BLOCKED_NETWORKS):
                raise Forbidden(f"worker host resolves to a blocked address ({addr})")
            if addr.is_private and not self.allow_private:
                raise Forbidden(
                    f"worker host resolves to a private address ({addr}) and "
                    "security.allow_private_worker_urls is false"
                )
        return url.rstrip("/")


MAGIC_BYTES: dict[bytes, str] = {
    b"RIFF": "audio/wav",
    b"ID3": "audio/mpeg",
    b"\xff\xfb": "audio/mpeg",
    b"\xff\xf3": "audio/mpeg",
    b"\xff\xf2": "audio/mpeg",
    b"OggS": "audio/ogg",
    b"fLaC": "audio/flac",
    b"\x1aE\xdf\xa3": "audio/webm",
}


def validate_upload(
    data: bytes, *, declared_mime: str | None, max_bytes: int, allowed: list[str]
) -> str:
    """Check size and magic bytes. Returns the sniffed MIME type."""
    if not data:
        raise InvalidRequest("uploaded file is empty")
    if len(data) > max_bytes:
        raise InvalidRequest(f"upload is {len(data)} bytes; limit is {max_bytes}")

    sniffed = None
    for magic, mime in MAGIC_BYTES.items():
        if data.startswith(magic):
            sniffed = mime
            break
    if sniffed is None and len(data) > 12 and data[4:8] == b"ftyp":
        sniffed = "audio/mp4"

    if sniffed is None:
        raise InvalidRequest(
            "unrecognised audio format; expected wav/mp3/flac/ogg/webm/mp4 (checked magic bytes)"
        )
    if sniffed not in allowed:
        raise InvalidRequest(f"mime type {sniffed!r} is not allowed")
    # `declared_mime` is deliberately not trusted: the caller chooses it, so believing it
    # over the bytes on disk would defeat the check. Magic bytes are the answer.
    return sniffed


def redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def audit_detail(**kwargs: Any) -> dict[str, Any]:
    """Strip anything that looks like a secret before persisting an audit row."""
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if any(h in key.lower() for h in ("key", "token", "secret", "password")):
            out[key] = "***"
        else:
            out[key] = value
    return out
