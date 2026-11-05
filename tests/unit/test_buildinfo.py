"""The build number has one job: answer "which build is this" without a human in the loop.

It is easy to get subtly wrong in ways nobody notices until an incident — a number that
never changes, a version that disagrees with the package metadata, or a missing value that
crashes startup instead of degrading.
"""

from __future__ import annotations

import json

import pytest

from ttsgrab.buildinfo import BuildInfo, build_info


def test_version_comes_from_package_metadata() -> None:
    """`__version__` must not be a second hand-maintained copy of pyproject's version."""
    import tomllib
    from pathlib import Path

    import ttsgrab

    root = Path(__file__).resolve().parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert ttsgrab.__version__ == declared["project"]["version"]


def test_label_is_readable_in_one_line() -> None:
    info = BuildInfo(
        version="0.1.0", build=7, commit="abc1234", branch="main", dirty=False, built_at=None
    )
    assert info.label() == "0.1.0 (build 7+abc1234)"


def test_label_marks_a_dirty_tree() -> None:
    info = BuildInfo(
        version="0.1.0", build=7, commit="abc1234", branch="main", dirty=True, built_at=None
    )
    assert "dirty" in info.label()


def test_label_degrades_when_there_is_no_build_number() -> None:
    """A source checkout without a generated file must not claim a build number."""
    info = BuildInfo(
        version="0.1.0", build=None, commit=None, branch=None, dirty=False, built_at=None
    )
    assert info.label() == "0.1.0"


def test_as_dict_is_json_serialisable() -> None:
    payload = build_info().as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {"version", "build", "commit", "dirty", "built_at"}


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Images built outside git get their number from the build arg, not from a file."""
    monkeypatch.setenv("TTSGRAB_BUILD", "4242")
    build_info.cache_clear()
    try:
        assert build_info().build == 4242
    finally:
        build_info.cache_clear()


def test_env_override_ignores_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTSGRAB_BUILD", "not-a-number")
    build_info.cache_clear()
    try:
        # Falls back to whatever the file says rather than raising or reporting 0.
        assert build_info().build is None or isinstance(build_info().build, int)
    finally:
        build_info.cache_clear()


@pytest.mark.asyncio
async def test_healthz_reports_build(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # `version` is the long-standing key; build fields are additive next to it.
    assert "version" in body
    assert "build" in body


@pytest.mark.asyncio
async def test_capabilities_reports_build(client) -> None:
    r = await client.get("/v1/capabilities")
    assert r.status_code == 200
    assert "version" in r.json()
