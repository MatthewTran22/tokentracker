"""Shared test setup.

`AUTH_SECRET` is read at module-import time inside `tokentracker.security`, so
we must seed os.environ BEFORE any test module imports the package. Putting
this in conftest.py ensures it runs before pytest collects tests.
"""
import os
import secrets

os.environ.setdefault("AUTH_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@localhost/stub")

from contextlib import asynccontextmanager  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB primitives. The routes access the pool with a uniform shape:
#   async with pool.connection() as conn:
#       async with conn.transaction():        # only used by register
#           async with conn.cursor() as cur:
#               await cur.execute(...)
#               row = await cur.fetchone()
#               await cur.executemany(...)
# We mirror just enough of that surface to drive the routes from tests
# without standing up a real Postgres.
# ---------------------------------------------------------------------------


class FakeCursor:
    """Single-cursor stub. Tests configure scripted behavior before each call."""

    def __init__(self):
        self.executions: list[tuple[str, object]] = []
        # Set before a route call to control fetchone() / fetchall() outputs
        # and to inject errors on execute().
        self.next_row = None
        self.next_rows: list = []
        self.raise_on_execute: BaseException | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        self.executions.append((" ".join(str(sql).split()), params))
        if self.raise_on_execute is not None:
            err = self.raise_on_execute
            self.raise_on_execute = None
            raise err

    async def executemany(self, sql, seq):
        self.executions.append((" ".join(str(sql).split()), list(seq)))

    async def fetchone(self):
        return self.next_row

    async def fetchall(self):
        return list(self.next_rows)


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def cursor(self):
        return self._cursor

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None

        return _tx()


class FakePool:
    def __init__(self):
        self.cursor = FakeCursor()

    def connection(self):
        return FakeConn(self.cursor)


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def test_app(fake_pool: FakePool) -> FastAPI:
    """FastAPI app with the real router but no lifespan / no real pool."""
    from tokentracker.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.pool = fake_pool
    return app


@pytest.fixture
async def client(test_app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def allow_all_rates(monkeypatch):
    """Stub `check_rate` to record bucket calls and always allow.

    Returns the list that captures `(bucket, limit, window)` tuples in order.
    """
    captured: list[tuple[str, int, int]] = []

    async def fake_check_rate(pool, bucket, limit, window_seconds):
        captured.append((bucket, limit, window_seconds))
        return True

    monkeypatch.setattr("tokentracker.routes.check_rate", fake_check_rate)
    return captured


@pytest.fixture
def block_all_rates(monkeypatch):
    """Stub `check_rate` to record bucket calls and always block."""
    captured: list[tuple[str, int, int]] = []

    async def fake_check_rate(pool, bucket, limit, window_seconds):
        captured.append((bucket, limit, window_seconds))
        return False

    monkeypatch.setattr("tokentracker.routes.check_rate", fake_check_rate)
    return captured
