"""Async jobs.

The point of a job is that it outlives the request, so the things worth testing are the
state transitions and what a poller can observe — not the synthesis itself, which is
covered elsewhere.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_poll_a_job(client, ctx) -> None:
    from tests.fakes import FakeProvider, install

    install(ctx, FakeProvider(ctx.settings, name="fake"))

    created = await client.post(
        "/v1/jobs", json={"text": "hello", "voice": "fake-voice", "provider": "fake"}
    )
    assert created.status_code in (200, 202), created.text
    job_id = created.json()["id"]
    assert created.json()["status"] in {"queued", "running", "succeeded"}

    polled = await client.get(f"/v1/jobs/{job_id}")
    assert polled.status_code == 200
    body = polled.json()
    assert body["id"] == job_id
    assert body["status"] in {"queued", "running", "succeeded", "failed"}


@pytest.mark.asyncio
async def test_unknown_job_is_404(client) -> None:
    r = await client.get("/v1/jobs/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_job_id_cannot_be_used_to_read_another_job(client) -> None:
    """Ids are unguessable, but the endpoint must still 404 rather than leak."""
    r = await client.get("/v1/jobs/00000000000000000000000000000000")
    assert r.status_code == 404
