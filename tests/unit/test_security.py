"""Security: API keys, rate limiting, SSRF guard, upload validation, log scrubbing."""

from __future__ import annotations

import pytest

from ttsgrab.core.errors import Forbidden, InvalidRequest
from ttsgrab.core.security import (
    SlidingWindowLimiter,
    SsrfGuard,
    audit_detail,
    generate_api_key,
    hash_api_key,
    redact,
    validate_upload,
)


def test_generated_key_is_prefixed_and_hashed() -> None:
    raw, digest, prefix = generate_api_key()
    assert raw.startswith("ttsg_")
    assert digest == hash_api_key(raw)
    assert raw.startswith(prefix)
    assert raw not in digest


def test_keys_are_unique() -> None:
    keys = {generate_api_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_rate_limiter_allows_then_blocks() -> None:
    limiter = SlidingWindowLimiter()
    for _ in range(3):
        ok, _ = limiter.check("me", 3)
        assert ok
    ok, retry_after = limiter.check("me", 3)
    assert ok is False
    assert retry_after >= 1


def test_rate_limiter_is_per_key() -> None:
    limiter = SlidingWindowLimiter()
    for _ in range(3):
        limiter.check("a", 3)
    ok, _ = limiter.check("b", 3)
    assert ok is True


def test_rate_limiter_reset() -> None:
    limiter = SlidingWindowLimiter()
    for _ in range(3):
        limiter.check("me", 3)
    limiter.reset("me")
    ok, _ = limiter.check("me", 3)
    assert ok is True


def test_ssrf_blocks_link_local_metadata() -> None:
    guard = SsrfGuard()
    with pytest.raises(Forbidden):
        guard.validate("http://169.254.169.254/latest/meta-data")


def test_ssrf_blocks_non_http_schemes() -> None:
    guard = SsrfGuard()
    with pytest.raises(InvalidRequest):
        guard.validate("file:///etc/passwd")


def test_ssrf_allows_lan_address_by_default() -> None:
    guard = SsrfGuard(allow_private=True)
    assert guard.validate("http://192.168.1.50:8880/") == "http://192.168.1.50:8880"


def test_ssrf_rejects_private_when_disabled() -> None:
    guard = SsrfGuard(allow_private=False)
    with pytest.raises(Forbidden):
        guard.validate("http://192.168.1.50:8880")


def test_ssrf_enforces_allowlist() -> None:
    guard = SsrfGuard(allowed_hosts=["127.0.0.1"])
    with pytest.raises(Forbidden):
        guard.validate("http://192.168.1.50:8880")
    assert guard.validate("http://127.0.0.1:8880") == "http://127.0.0.1:8880"


def test_upload_accepts_wav_by_magic_bytes() -> None:
    wav = b"RIFF" + b"\x00" * 40 + b"WAVE"
    assert (
        validate_upload(wav, declared_mime="audio/wav", max_bytes=1000, allowed=["audio/wav"])
        == "audio/wav"
    )


def test_upload_rejects_wrong_magic() -> None:
    with pytest.raises(InvalidRequest):
        validate_upload(
            b"not audio at all", declared_mime="audio/wav", max_bytes=1000, allowed=["audio/wav"]
        )


def test_upload_rejects_oversize() -> None:
    wav = b"RIFF" + b"\x00" * 2000
    with pytest.raises(InvalidRequest):
        validate_upload(wav, declared_mime="audio/wav", max_bytes=100, allowed=["audio/wav"])


def test_upload_rejects_empty() -> None:
    with pytest.raises(InvalidRequest):
        validate_upload(b"", declared_mime=None, max_bytes=100, allowed=["audio/wav"])


def test_upload_rejects_disallowed_mime() -> None:
    mp3 = b"ID3" + b"\x00" * 100
    with pytest.raises(InvalidRequest):
        validate_upload(mp3, declared_mime="audio/mpeg", max_bytes=1000, allowed=["audio/wav"])


def test_redact_keeps_ends_only() -> None:
    assert redact("ttsg_abcdefghijklmnop") == "ttsg…mnop"
    assert redact(None) == ""


def test_audit_detail_strips_secrets() -> None:
    detail = audit_detail(api_key="secret", token="x", password="y", provider="edge")
    assert detail["api_key"] == "***"
    assert detail["token"] == "***"
    assert detail["password"] == "***"
    assert detail["provider"] == "edge"


def test_log_scrubber_removes_key_like_values() -> None:
    from ttsgrab.core.obs import _scrub

    event = {"api_key": "abc", "msg": "hi", "value": "sk-proj-1234567890abcdefghijklmnop"}
    out = _scrub(None, None, dict(event))
    assert out["api_key"] == "***"
    assert out["value"] == "***"
    assert out["msg"] == "hi"
