"""Live-stats hub, snapshot builder, and the /stats/claude/stream SSE endpoint.

These cover the behaviors that make the push model cheap on a small box:
the hub bumps a version only on real change, fans one payload out to many
subscribers, idles when nobody's connected, and refuses excess connections.
The builder recomputes the heavy leaderboards only every Nth tick.

The streaming route is exercised by pulling directly from the response's
async body iterator rather than over an ASGI test client — an SSE generator
never completes, so a buffering client would just hang.
"""

import asyncio

import pytest
from fastapi import HTTPException

from tokentracker.routes import stats_stream
from tokentracker.stats import MAX_CLIENTS, SnapshotBuilder, StatsHub


# ---------------------------------------------------------------------------
# A cursor that returns scripted results per query, so we can drive the real
# build_snapshot query helpers (the conftest FakeCursor returns one fixed row
# for every fetch, which can't represent totals + five distinct boards).
# ---------------------------------------------------------------------------


class ScriptedCursor:
    def __init__(self, fetchone_rows, fetchall_rows):
        self._fetchone = list(fetchone_rows)
        self._fetchall = list(fetchall_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        pass

    async def fetchone(self):
        return self._fetchone.pop(0)

    async def fetchall(self):
        return self._fetchall.pop(0)


class ScriptedConn:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def cursor(self):
        return self._cursor


class ScriptedPool:
    """Hands out a fresh ScriptedCursor per connection from a factory."""

    def __init__(self, cursor_factory):
        self._factory = cursor_factory
        self.connections = 0

    def connection(self):
        self.connections += 1
        return ScriptedConn(self._factory())


def _totals_row():
    # total_users, total_requests, billed_tokens, total_cost_usd, last_activity
    return (3, 10, 1000, 12.5, None)


def _by_model_rows():
    return [("opus", 8.0, 600, 6), ("sonnet", 4.5, 400, 4)]


def _board_rows(n=2):
    # id, handle, cost, tokens, requests, last_activity
    return [(i, f"user {i}", 1.0 * i, 100 * i, i, None) for i in range(1, n + 1)]


def _full_cursor():
    # fetchone: totals. fetchall (in order): by_model, all, opus, sonnet, haiku, fable.
    return ScriptedCursor(
        fetchone_rows=[_totals_row()],
        fetchall_rows=[
            _by_model_rows(),
            _board_rows(),  # all
            _board_rows(),  # opus
            _board_rows(),  # sonnet
            _board_rows(),  # haiku
            _board_rows(),  # fable
        ],
    )


# ---------------------------------------------------------------------------
# StatsHub
# ---------------------------------------------------------------------------


async def test_publish_bumps_version_only_on_change():
    calls = []

    async def build():
        calls.append(1)
        return {"n": len(calls)}  # changes every call

    hub = StatsHub(build, refresh_seconds=3600)
    await hub._refresh_once()
    assert hub.version == 1
    first = hub.payload

    # Identical payload -> no version bump, no new bytes.
    hub._publish(first)
    assert hub.version == 1

    await hub._refresh_once()
    assert hub.version == 2
    assert hub.payload != first


async def test_subscribe_primes_snapshot_then_idles_when_empty():
    async def build():
        return {"ok": True}

    hub = StatsHub(build, refresh_seconds=3600)
    assert hub.version == 0
    await hub.subscribe()
    # First subscriber gets data immediately, without waiting an interval.
    assert hub.version == 1
    assert hub.subscribers == 1

    hub.unsubscribe()
    assert hub.subscribers == 0
    await hub.shutdown()


async def test_wait_for_change_wakes_on_publish_and_times_out():
    async def build():
        return {"t": 1}

    hub = StatsHub(build, refresh_seconds=3600)

    # Times out (returns False) when nothing changes.
    assert await hub.wait_for_change(0.05) is False

    # Wakes (returns True) when a publish happens while waiting.
    waiter = asyncio.create_task(hub.wait_for_change(1.0))
    await asyncio.sleep(0.01)
    hub._publish(b'{"t":2}')
    assert await waiter is True


# ---------------------------------------------------------------------------
# SnapshotBuilder
# ---------------------------------------------------------------------------


async def test_builder_shape_and_leaderboard_caching():
    pool = ScriptedPool(_full_cursor)
    builder = SnapshotBuilder(pool, leaderboard_every=3)

    snap = await builder.build()
    assert set(snap) == {"totals", "leaderboards"}
    assert snap["totals"]["total_users"] == 3
    assert snap["totals"]["by_model"]["opus"]["cost_usd"] == 8.0
    assert set(snap["leaderboards"]) == {"all", "opus", "sonnet", "haiku", "fable"}
    assert snap["totals"]["by_model"]["fable"] == {"cost_usd": 0.0, "billed_tokens": 0, "requests": 0}
    assert snap["leaderboards"]["fable"][0]["rank"] == 1
    assert snap["leaderboards"]["all"][0]["rank"] == 1

    # Ticks 2 and 3 reuse the cached boards (only the cheap totals query runs);
    # tick 4 (index 3, 3 % 3 == 0) recomputes them. Each build opens exactly one
    # connection, so four builds == four connections.
    await builder.build()
    await builder.build()
    snap4 = await builder.build()
    assert snap4["leaderboards"]["all"][0]["rank"] == 1
    assert pool.connections == 4


# ---------------------------------------------------------------------------
# /stats/claude/stream route — drive the body iterator directly.
# ---------------------------------------------------------------------------


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, hub):
        self.state = _FakeState()
        self.state.stats_hub = hub
        self.state.pool = None  # get_hub returns the pre-set hub, never used


class _FakeRequest:
    def __init__(self, app):
        self.app = app

    async def is_disconnected(self):
        return False


async def test_stream_emits_initial_snapshot_event():
    async def build():
        return {"totals": {"total_cost_usd": 1.5}, "leaderboards": {"all": []}}

    hub = StatsHub(build, refresh_seconds=3600)
    resp = await stats_stream(_FakeRequest(_FakeApp(hub)))
    assert resp.media_type == "text/event-stream"
    assert resp.headers.get("x-accel-buffering") == "no"

    agen = resp.body_iterator
    try:
        first = await agen.__anext__()
        text = first.decode() if isinstance(first, (bytes, bytearray)) else first
        assert text.startswith("event: snapshot")
        assert '"total_cost_usd":1.5' in text
    finally:
        await agen.aclose()
        await hub.shutdown()


async def test_stream_unsubscribes_when_client_goes_away():
    async def build():
        return {"ok": 1}

    hub = StatsHub(build, refresh_seconds=3600)
    resp = await stats_stream(_FakeRequest(_FakeApp(hub)))
    agen = resp.body_iterator
    await agen.__anext__()
    assert hub.subscribers == 1
    # Closing the stream must release the subscriber slot so the box can idle.
    await agen.aclose()
    assert hub.subscribers == 0
    await hub.shutdown()


async def test_stream_503_when_at_capacity():
    async def build():
        return {"x": 1}

    hub = StatsHub(build, refresh_seconds=3600)
    hub._subscribers = MAX_CLIENTS  # simulate a full box
    try:
        with pytest.raises(HTTPException) as excinfo:
            await stats_stream(_FakeRequest(_FakeApp(hub)))
        assert excinfo.value.status_code == 503
        assert excinfo.value.headers.get("Retry-After") == "30"
    finally:
        await hub.shutdown()
