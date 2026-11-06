"""The settings API writes a file the service reads at boot.

Two ways to get this wrong hurt more than a wrong value: writing a key nobody reads (a typo
that silently does nothing) and destroying the operator's comments. Both are tested here, plus
the one that would be a security bug — returning a secret unmasked.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def admin_client(app, settings):
    # Pin a known token: relying on the localhost bypass would make the masking test skip
    # in any environment that has one configured, and a skipped security test is no test.
    settings.security.admin_token = settings.security.admin_token or "test-admin-token-1234"
    token = settings.security.admin_token
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    ) as c:
        yield c


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point the app at a throwaway config.yaml with comments worth preserving."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "# operator notes — must survive a write\n"
        "router:\n"
        "  # we prefer the free one\n"
        "  default_policy: cheapest\n",
        encoding="utf-8",
    )
    return p


async def test_get_lists_editable_keys(admin_client):
    r = await admin_client.get("/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert "values" in body and "editable" in body
    # The UI renders from `editable`, so an empty map means a blank page.
    assert "edge.enabled" in body["editable"]


async def test_a_secret_is_masked_and_distinguishable(admin_client):
    r = await admin_client.get("/v1/settings")
    value = r.json()["values"].get("security.admin_token")
    assert value is not None, "the token is configured, so the field must be present"
    # Masked, but enough to tell two keys apart — otherwise the field is unusable.
    assert "•" in str(value)
    assert "test-admin-token-1234" not in str(value)
    assert str(value).endswith("1234")


async def test_an_unknown_key_is_refused_with_the_list(admin_client):
    r = await admin_client.put(
        "/v1/settings", json={"values": {"router.default_polcy": "cheapest"}}
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert "unknown" in err["code"] or err["details"].get("unknown") == ["router.default_polcy"]
    # The response must teach the caller the right name, not just refuse.
    assert any("default_policy" in k for k in err["details"]["editable"])


async def test_saving_preserves_comments(app, cfg):
    """The comments in this file are the documentation for every option."""
    before = cfg.read_text(encoding="utf-8")
    from ttsgrab.core import configfile

    configfile.set_values(cfg, {"router.default_policy": "fastest"})

    after = cfg.read_text(encoding="utf-8")
    assert after != before
    assert "# operator notes — must survive a write" in after
    assert "# we prefer the free one" in after
    assert "fastest" in after


async def test_the_file_is_not_readable_by_other_users(cfg):
    """It holds provider keys now, so 0600 is part of the contract."""
    import stat

    from ttsgrab.core import configfile

    configfile.set_values(cfg, {"router.default_policy": "best_quality"})
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600, f"config.yaml is {oct(mode)}, expected 0600"


async def test_a_bad_type_is_refused_not_coerced(admin_client):
    """`"yes"` arriving for an int field must not become 0 and be written."""
    r = await admin_client.put(
        "/v1/settings", json={"values": {"longform.max_concurrency": "not-a-number"}}
    )
    assert r.status_code == 400
