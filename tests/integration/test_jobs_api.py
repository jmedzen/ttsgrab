"""Async jobs: quota at enqueue, and a failed job that says so.

The worker has no principal, so anything a job needs enforced has to be enforced before it is
queued. Otherwise a restricted key can queue work and the router picks the provider.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests.helpers_keys import make_key


async def _client(app, token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_a_restricted_key_cannot_queue_a_job_without_naming_a_provider(app, settings):
    token = await make_key(app, settings, allowlist=["edge"])
    async with await _client(app, token) as c:
        r = await c.post("/v1/jobs", json={"text": "hello", "voice": "en-US-AriaNeural"})
        assert r.status_code in (400, 403), "the allowlist was not checked at enqueue time"
        assert r.json()["error"]["code"] in ("invalid_request", "forbidden")


async def test_an_over_quota_key_is_refused_before_the_job_is_queued(app, settings):
    token = await make_key(app, settings, quota=1000)
    async with await _client(app, token) as c:
        r = await c.post(
            "/v1/jobs",
            json={"text": "x" * 2000, "voice": "en-US-AriaNeural", "provider": "edge"},
        )
        assert r.status_code == 402
        assert r.json()["error"]["code"] == "quota_exceeded"

        # And nothing was queued: a job that starts only to be rejected has already been
        # reported as `running`.
        listed = await c.get("/v1/jobs")
        assert listed.status_code == 200
        assert listed.json().get("jobs") in ([], None)
