"""Body-size and input limits must be enforced, not merely configured.

`max_request_body_bytes` was declared, documented, and never checked: every byte of a
2 MB-limit request was buffered before validation. A limit nobody enforces is worse than
no limit, because it is a claim the operator relies on.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_before_parsing(client, ctx) -> None:
    ctx.settings.security.max_request_body_bytes = 1024
    payload = {"text": "x" * 5000, "voice": "v"}
    r = await client.post("/v1/tts", json=payload)
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["error"]["code"] == "request_too_large"
    assert body["error"]["details"]["limit_bytes"] == 1024


@pytest.mark.asyncio
async def test_a_body_within_the_limit_is_accepted(client, ctx) -> None:
    """The check must not reject normal traffic."""
    ctx.settings.security.max_request_body_bytes = 1024 * 1024
    r = await client.post("/v1/tts/estimate", json={"text": "hello", "voice": "fake-voice"})
    assert r.status_code != 413, r.text


@pytest.mark.asyncio
async def test_text_longer_than_the_policy_limit_is_413(client, ctx) -> None:
    """Two different limits: `text_too_long` is a policy cap on characters."""
    ctx.settings.security.max_request_body_bytes = 10 * 1024 * 1024
    ctx.settings.security.max_text_chars = 100
    r = await client.post("/v1/tts", json={"text": "x" * 500, "voice": "fake-voice"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "text_too_long"


@pytest.mark.asyncio
async def test_upload_rejects_a_non_audio_file(client, ctx) -> None:
    """Magic bytes, not the declared content type, decide what an upload is."""
    ctx.settings.security.admin_token = "admin-token"
    r = await client.post(
        "/v1/references",
        files={"file": ("x.wav", b"#!/bin/sh\necho pwned\n", "audio/wav")},
        data={"name": "evil", "provider": "gpt_sovits", "consent_note": "test"},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert r.status_code == 400, r.text
    assert "magic" in r.text.lower() or "format" in r.text.lower()


@pytest.mark.asyncio
async def test_upload_rejects_an_empty_file(client, ctx) -> None:
    ctx.settings.security.admin_token = "admin-token"
    r = await client.post(
        "/v1/references",
        files={"file": ("x.wav", b"", "audio/wav")},
        data={"name": "empty", "provider": "gpt_sovits", "consent_note": "test"},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert r.status_code == 400, r.text
