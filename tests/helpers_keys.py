"""Create a real API key through the admin API, for tests that need two tenants."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient


async def make_key(
    app,
    settings,
    *,
    name: str = "test",
    allowlist: list[str] | None = None,
    quota: int | None = None,
) -> str:
    token = settings.security.admin_token or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as c:
        body: dict[str, object] = {"name": name}
        if allowlist is not None:
            body["provider_allowlist"] = allowlist
        if quota is not None:
            body["monthly_char_quota"] = quota
        r = await c.post("/v1/keys", json=body)
        r.raise_for_status()
        return str(r.json()["token"])
