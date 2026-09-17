"""Live stats: shared snapshot + Server-Sent Events broadcast.

The public dashboard used to have every viewer poll ``/stats/totals`` and
``/stats/leaderboard`` every 2s, so the database re-ran a full-table aggregate
over ``api_requests`` once *per viewer per tick*. On a 1 vCPU / 1 GB box (where
Postgres shares the same CPU) that cost grows with both traffic and audience.

This module decouples *computation* from *delivery*:

* A single :class:`SnapshotBuilder` runs the aggregates once per interval,
  independent of how many people are watching.
* A single :class:`StatsHub` fans the latest snapshot out to every connected
  SSE client from one shared, immutable ``bytes`` payload — no per-client
  queues, no history buffer.
* The refresh loop only runs while at least one client is connected, so an
  unwatched box issues zero stats queries.

Tunables (all via env, sensible defaults for a small VM):

* ``STATS_REFRESH_SECONDS``        — headline totals cadence (default 2.0)
* ``STATS_LEADERBOARD_EVERY``      — recompute leaderboards every Nth tick (3)
* ``STATS_STREAM_MAX_CLIENTS``     — hard cap on concurrent streams (256)
* ``STATS_STREAM_HEARTBEAT_SECONDS`` — keep-alive comment interval (15.0)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable

# Model name -> family bucket. Substring match is intentional: real model
# strings look like "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5",
# and we want new minor versions to slot in without code changes. Anything that
# matches none falls into "other" and isn't surfaced in the UI.
MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")
MODEL_FAMILY_PATTERNS = {f: f"%{f}%" for f in MODEL_FAMILIES}

LEADERBOARD_LIMIT = 100

REFRESH_SECONDS = float(os.environ.get("STATS_REFRESH_SECONDS", "2"))
LEADERBOARD_EVERY = max(1, int(os.environ.get("STATS_LEADERBOARD_EVERY", "3")))
MAX_CLIENTS = int(os.environ.get("STATS_STREAM_MAX_CLIENTS", "256"))
HEARTBEAT_SECONDS = float(os.environ.get("STATS_STREAM_HEARTBEAT_SECONDS", "15"))


# ---------------------------------------------------------------------------
# Query helpers — the single source of truth for both the REST endpoints and
# the streamed snapshot, so the two can never drift apart.
# ---------------------------------------------------------------------------


async def fetch_totals(cur) -> dict:
    """Aggregate site-wide totals plus a per-family cost breakdown."""
    await cur.execute(
        """
        SELECT
            (SELECT count(*)::bigint FROM users)    AS total_users,
            count(*)::bigint                        AS total_requests,
            COALESCE(sum(billed_tokens), 0)         AS billed_tokens,
            COALESCE(sum(cost_usd), 0)::float       AS total_cost_usd,
            max(occurred_at)                        AS last_activity_at
        FROM api_requests
        """
    )
    totals = await cur.fetchone()

    await cur.execute(
        """
        SELECT
            CASE
                WHEN model ILIKE %s THEN 'opus'
                WHEN model ILIKE %s THEN 'sonnet'
                WHEN model ILIKE %s THEN 'haiku'
                WHEN model ILIKE %s THEN 'fable'
                ELSE 'other'
            END                                     AS family,
            COALESCE(sum(cost_usd), 0)::float       AS cost_usd,
            COALESCE(sum(billed_tokens), 0)         AS billed_tokens,
            count(*)::bigint                        AS requests
        FROM api_requests
        WHERE model IS NOT NULL
        GROUP BY family
        """,
        (
            MODEL_FAMILY_PATTERNS["opus"],
            MODEL_FAMILY_PATTERNS["sonnet"],
            MODEL_FAMILY_PATTERNS["haiku"],
            MODEL_FAMILY_PATTERNS["fable"],
        ),
    )
    model_rows = await cur.fetchall()

    by_model = {
        f: {"cost_usd": 0.0, "billed_tokens": 0, "requests": 0}
        for f in MODEL_FAMILIES
    }
    for family, cost_usd, billed_tokens, requests in model_rows:
        if family in by_model:
            by_model[family] = {
                "cost_usd": cost_usd,
                "billed_tokens": billed_tokens,
                "requests": requests,
            }

    return {
        "total_users": totals[0],
        "total_requests": totals[1],
        "billed_tokens": totals[2],
        "total_cost_usd": totals[3],
        "last_activity_at": totals[4].isoformat() if totals[4] else None,
        "by_model": by_model,
    }


async def fetch_leaderboard(cur, pattern: str | None, limit: int) -> list[dict]:
    """Top users by spend, optionally filtered to one model family.

    ``pattern`` is an ``ILIKE`` pattern (e.g. ``%opus%``) or ``None`` for the
    unfiltered board. Returns ranked rows ready for JSON.
    """
    if pattern is None:
        await cur.execute(
            """
            SELECT
                u.id,
                COALESCE(u.username, 'user ' || u.id::text) AS handle,
                COALESCE(sum(r.cost_usd), 0)::float         AS total_cost_usd,
                COALESCE(sum(r.billed_tokens), 0)           AS billed_tokens,
                count(r.id)::bigint                         AS requests,
                max(r.occurred_at)                          AS last_activity_at
            FROM users u
            LEFT JOIN api_requests r ON r.user_id = u.id
            GROUP BY u.id, u.username
            ORDER BY total_cost_usd DESC, u.id ASC
            LIMIT %s
            """,
            (limit,),
        )
    else:
        # INNER JOIN so users with no activity in this family fall off entirely
        # — the filtered board shouldn't list them at $0.
        await cur.execute(
            """
            SELECT
                u.id,
                COALESCE(u.username, 'user ' || u.id::text) AS handle,
                COALESCE(sum(r.cost_usd), 0)::float         AS total_cost_usd,
                COALESCE(sum(r.billed_tokens), 0)           AS billed_tokens,
                count(r.id)::bigint                         AS requests,
                max(r.occurred_at)                          AS last_activity_at
            FROM users u
            JOIN api_requests r
              ON r.user_id = u.id AND r.model ILIKE %s
            GROUP BY u.id, u.username
            ORDER BY total_cost_usd DESC, u.id ASC
            LIMIT %s
            """,
            (pattern, limit),
        )
    rows = await cur.fetchall()
    return [
        {
            "rank": i + 1,
            "user_id": r[0],
            "handle": r[1],
            "total_cost_usd": r[2],
            "billed_tokens": r[3],
            "requests": r[4],
            "last_activity_at": r[5].isoformat() if r[5] else None,
        }
        for i, r in enumerate(rows)
    ]


# ---------------------------------------------------------------------------
# Snapshot builder — owns the two-track refresh cadence.
# ---------------------------------------------------------------------------


class SnapshotBuilder:
    """Builds the full dashboard snapshot, recomputing the cheap totals every
    tick and the heavier leaderboards only every ``leaderboard_every`` ticks.
    Leaderboards are cached and reused on the in-between ticks so the headline
    numbers stay snappy without hammering the shared CPU with full-table joins.
    """

    def __init__(self, pool, leaderboard_every: int = LEADERBOARD_EVERY):
        self._pool = pool
        self._every = leaderboard_every
        self._tick = 0
        self._leaderboards: dict | None = None

    async def build(self) -> dict:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                totals = await fetch_totals(cur)
                if self._leaderboards is None or self._tick % self._every == 0:
                    self._leaderboards = {
                        "all": await fetch_leaderboard(cur, None, LEADERBOARD_LIMIT),
                        **{
                            fam: await fetch_leaderboard(
                                cur, MODEL_FAMILY_PATTERNS[fam], LEADERBOARD_LIMIT
                            )
                            for fam in MODEL_FAMILIES
                        },
                    }
        self._tick += 1
        return {"totals": totals, "leaderboards": self._leaderboards}


# ---------------------------------------------------------------------------
# Broadcast hub — one snapshot, many SSE subscribers.
# ---------------------------------------------------------------------------


class StatsHub:
    """Holds the latest serialized snapshot and wakes subscribers on change.

    The refresh loop is lazily started on the first subscriber and stops once
    the last one disconnects, so an idle box does no work. Subscribers share a
    single immutable ``bytes`` payload; the only per-client state is one
    coroutine frame plus an ``asyncio.Event`` reference.
    """

    def __init__(
        self,
        build: Callable[[], Awaitable[dict]],
        *,
        refresh_seconds: float = REFRESH_SECONDS,
    ):
        self._build = build
        self._refresh_seconds = refresh_seconds
        self._payload: bytes = b"{}"
        self._version = 0
        self._updated = asyncio.Event()
        self._subscribers = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def version(self) -> int:
        return self._version

    @property
    def payload(self) -> bytes:
        return self._payload

    @property
    def subscribers(self) -> int:
        return self._subscribers

    def _publish(self, payload: bytes) -> bool:
        # Skip the fan-out entirely when nothing changed — no version bump, no
        # wakeups, no bytes on the wire.
        if payload == self._payload:
            return False
        self._payload = payload
        self._version += 1
        ev, self._updated = self._updated, asyncio.Event()
        ev.set()
        return True

    async def _refresh_once(self) -> None:
        try:
            snap = await self._build()
        except Exception:
            # Transient DB error: keep serving the last good snapshot.
            return
        self._publish(json.dumps(snap, separators=(",", ":"), default=str).encode())

    async def _run(self) -> None:
        try:
            while self._subscribers > 0:
                await asyncio.sleep(self._refresh_seconds)
                if self._subscribers <= 0:
                    break
                await self._refresh_once()
        finally:
            self._task = None

    async def _ensure_running(self) -> None:
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())

    async def subscribe(self) -> None:
        """Register a subscriber. Primes the first snapshot synchronously so the
        very first client gets data without waiting a full interval."""
        self._subscribers += 1
        if self._version == 0:
            await self._refresh_once()
        await self._ensure_running()

    def unsubscribe(self) -> None:
        self._subscribers = max(0, self._subscribers - 1)

    async def wait_for_change(self, timeout: float) -> bool:
        """Block until the snapshot changes or ``timeout`` elapses."""
        ev = self._updated
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def get_hub(app) -> StatsHub:
    """Lazily create the per-app hub bound to the live connection pool."""
    hub = getattr(app.state, "stats_hub", None)
    if hub is None:
        builder = SnapshotBuilder(app.state.pool)
        hub = StatsHub(builder.build)
        app.state.stats_hub = hub
    return hub
