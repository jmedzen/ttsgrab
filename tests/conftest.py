"""Shared test fixtures.

One application instance per test, so the fake providers a test installs are the same ones
the HTTP layer sees. (Two independent contexts would make every integration assertion pass
or fail for the wrong reason.)
"""

from __future__ import annotations

import os
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
