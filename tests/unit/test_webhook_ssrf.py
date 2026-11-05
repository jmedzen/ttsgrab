"""Webhook URLs are caller-supplied, so they are an SSRF surface.

Without a check, `POST /v1/jobs {"webhook_url": "http://169.254.169.254/..."}` would make
the gateway fetch cloud metadata and hand the response back as `webhook_status`.
"""

from __future__ import annotations

import pytest

from ttsgrab.api.v1.jobs import _fire_webhook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8000/v1/keys",
        "http://localhost:8000/v1/keys",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_PING",
    ],
)
async def test_internal_webhook_targets_are_refused(url) -> None:
    # A refused webhook reports None (no delivery) rather than raising into the job.
    assert await _fire_webhook(url, {"job_id": "x"}, allow_private=False) is None


@pytest.mark.asyncio
async def test_private_webhooks_need_an_explicit_opt_in() -> None:
    """The policy is the setting, not a hardcoded guess: LAN webhooks are a real use case."""
    url = "http://192.168.1.50:8080/hook"
    # Default (no opt-in): refused before any connection is attempted.
    assert await _fire_webhook(url, {"job_id": "x"}, allow_private=False) is None
