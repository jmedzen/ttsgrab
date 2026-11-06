"""Shared test fixtures.

One application instance per test, so the fake providers a test installs are the same ones
the HTTP layer sees. (Two independent contexts would make every integration assertion pass
or fail for the wrong reason.)
"""

from __future__ import annotations

import os

# Set before any test imports the settings module. Without it a developer who ran
# `make .env` (the documented quickstart) gets failures from their own real configuration
# leaking into the suite — e.g. a test that expects the default router policy finding the
# operator's `cheapest` instead.
os.environ.setdefault("TTSGRAB_IGNORE_ENV_FILE", "1")
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Isolate every test run's data directory before settings are imported.
_TMP = tempfile.mkdtemp(prefix="ttsgrab-test-")
os.environ["TTSGRAB_DATA_DIR"] = _TMP
os.environ["TTSGRAB_CACHE__DIR"] = str(Path(_TMP) / "cache")
os.environ["TTSGRAB_ENV"] = "test"


def pytest_sessionfinish(session, exitstatus):
    """Remove the per-run data directory.

    Left behind, it accumulates a SQLite file per test forever, and the aiosqlite worker
    threads that outlive a test surface as "Event loop is closed" noise that hides real
    leaks.
    """
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def tmp_data_dir() -> Path:
    return Path(_TMP)


@pytest.fixture
def settings():
    from ttsgrab.settings import load_settings

    s = load_settings("/nonexistent-config.yaml")
    s.data_dir = _TMP
    s.cache.dir = str(Path(_TMP) / "cache")
    s.database_url = f"sqlite+aiosqlite:///{_TMP}/test-{os.urandom(4).hex()}.db"
    s.ensure_dirs()
    return s


@pytest_asyncio.fixture
async def app(settings):
    from ttsgrab.main import create_app

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def ctx(app):
    """The live application context — the same one the HTTP layer uses."""
    return app.state.ctx


@pytest_asyncio.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def stub_app(settings):
    """An app with a deterministic offline provider.

    End-to-end features (idempotency, subtitles, loudness, provenance) only fail at the
    seams of the whole pipeline, so they need real synthesis. `edge` needs the internet,
    which CI does not reliably have; the stub returns real WAV audio instead.
    """
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).parent))
    from stub_provider import StubProvider
    from ttsgrab.main import create_app

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        ctx = application.state.ctx
        stub = StubProvider(ctx.settings)
        ctx.registry.add_instance(stub)
        # Make it reachable by policy routing and by name.
        ctx.settings.router.fallback_chain = ["stub"]
        await stub.refresh_voices()
        yield application


@pytest_asyncio.fixture
async def stub_client(stub_app, settings):
    import httpx
    from httpx import ASGITransport

    token = settings.security.admin_token or "test-admin"
    transport = ASGITransport(app=stub_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c
