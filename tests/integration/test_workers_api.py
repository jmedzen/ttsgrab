"""Worker registry endpoints.

The registry is the one place a caller can make the gateway talk to an arbitrary address,
so both the happy path and every refusal matter.
"""

from __future__ import annotations

import pytest

ADMIN = {"Authorization": "Bearer admin-token"}


@pytest.fixture(autouse=True)
def _admin(ctx):
    ctx.settings.security.admin_token = "admin-token"
    return ctx


@pytest.mark.asyncio
async def test_register_list_and_delete_a_worker(client, ctx) -> None:
    r = await client.post(
        "/v1/workers",
        json={"name": "lan-1", "base_url": "http://127.0.0.1:9099", "kind": "tts"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    worker_id = r.json()["id"]

    listed = await client.get("/v1/workers", headers=ADMIN)
    assert any(w["id"] == worker_id for w in listed.json()["workers"])

    gone = await client.delete(f"/v1/workers/{worker_id}", headers=ADMIN)
    assert gone.status_code == 200

    listed = await client.get("/v1/workers", headers=ADMIN)
    assert not any(w["id"] == worker_id for w in listed.json()["workers"])


@pytest.mark.asyncio
async def test_duplicate_worker_name_is_refused(client) -> None:
    body = {"name": "dup", "base_url": "http://127.0.0.1:9098", "kind": "tts"}
    first = await client.post("/v1/workers", json=body, headers=ADMIN)
    assert first.status_code == 200, first.text

    second = await client.post("/v1/workers", json=body, headers=ADMIN)
    assert second.status_code == 400
    assert "already registered" in second.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/",  # not http(s)
        "http://169.254.169.254/",  # cloud metadata
        "file:///etc/passwd",
        "http://",  # no host
    ],
)
async def test_dangerous_worker_urls_are_refused(client, url) -> None:
    r = await client.post(
        "/v1/workers", json={"name": "bad", "base_url": url, "kind": "tts"}, headers=ADMIN
    )
    assert r.status_code in (400, 403), f"{url} was accepted: {r.text}"


@pytest.mark.asyncio
async def test_worker_url_allowlist_is_enforced(client, ctx) -> None:
    ctx.settings.security.allowed_worker_hosts = ["gpu-box.lan"]
    r = await client.post(
        "/v1/workers",
        json={"name": "off-list", "base_url": "http://127.0.0.1:9097", "kind": "tts"},
        headers=ADMIN,
    )
    assert r.status_code == 403
    assert "not in security.allowed_worker_hosts" in r.text


@pytest.mark.asyncio
async def test_an_unreachable_worker_registers_but_reports_unhealthy(client) -> None:
    """A worker that is down must not make registration fail — the operator may be
    adding it before starting the container."""
    r = await client.post(
        "/v1/workers",
        json={"name": "offline", "base_url": "http://127.0.0.1:9", "kind": "tts"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["healthy"] is False
    assert body["last_error"]


@pytest.mark.asyncio
async def test_check_refreshes_health(client) -> None:
    created = await client.post(
        "/v1/workers",
        json={"name": "probe", "base_url": "http://127.0.0.1:9", "kind": "tts"},
        headers=ADMIN,
    )
    worker_id = created.json()["id"]
    r = await client.post(f"/v1/workers/{worker_id}/check", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["healthy"] is False


@pytest.mark.asyncio
async def test_check_unknown_worker_is_404(client) -> None:
    r = await client.post("/v1/workers/does-not-exist/check", headers=ADMIN)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_worker_is_404(client) -> None:
    r = await client.delete("/v1/workers/does-not-exist", headers=ADMIN)
    assert r.status_code == 404
