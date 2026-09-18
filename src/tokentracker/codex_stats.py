"""Codex-only aggregates. Input/output counts already include cache/reasoning."""
import asyncio
import time
from collections import OrderedDict

# Always show these models, including before the first usage event.
DISPLAY_MODELS = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


async def fetch_codex_totals(cur, user_id: int | None = None) -> dict:
    where = 'WHERE user_id = %s' if user_id is not None else ''
    params = (user_id,) if user_id is not None else ()
    await cur.execute(
        f"""SELECT count(DISTINCT user_id), count(*)::bigint,
                   COALESCE(sum(input_tokens::bigint + output_tokens::bigint), 0),
                   COALESCE(sum(cost_usd), 0)::float,
                   count(*) FILTER (WHERE cost_usd IS NULL), max(occurred_at),
                   COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0),
                   COALESCE(sum(cached_input_tokens), 0),
                   COALESCE(sum(reasoning_output_tokens), 0)
            FROM codex_api_requests {where}""", params,
    )
    row = await cur.fetchone()
    await cur.execute(
        f"""SELECT COALESCE(model, 'unknown'), COALESCE(sum(cost_usd), 0)::float,
                   COALESCE(sum(input_tokens::bigint + output_tokens::bigint), 0),
                   count(*)::bigint, count(*) FILTER (WHERE cost_usd IS NULL)
            FROM codex_api_requests {where}
            GROUP BY COALESCE(model, 'unknown') ORDER BY COALESCE(model, 'unknown')""", params,
    )
    models = await cur.fetchall()
    by_model = {
        model: {'cost_usd': 0.0, 'total_tokens': 0, 'requests': 0, 'unpriced_requests': 0}
        for model in DISPLAY_MODELS
    }
    by_model.update({
        m[0]: {'cost_usd': m[1], 'total_tokens': m[2], 'requests': m[3],
               'unpriced_requests': m[4]}
        for m in models
    })
    return {
        'total_users': row[0], 'total_requests': row[1], 'total_tokens': row[2],
        'total_cost_usd': row[3], 'unpriced_requests': row[4],
        'last_activity_at': row[5].isoformat() if row[5] else None,
        'input_tokens': row[6], 'output_tokens': row[7],
        'cached_input_tokens': row[8], 'reasoning_output_tokens': row[9],
        'by_model': by_model,
    }


async def fetch_codex_leaderboard(cur, model: str | None, limit: int) -> list[dict]:
    where = "WHERE COALESCE(r.model, 'unknown') = %s" if model else ''
    params = (model, limit) if model else (limit,)
    await cur.execute(
        f"""SELECT u.id, u.username, COALESCE(sum(r.cost_usd), 0)::float AS cost,
                   COALESCE(sum(r.input_tokens::bigint + r.output_tokens::bigint), 0),
                   count(*)::bigint, count(*) FILTER (WHERE r.cost_usd IS NULL),
                   max(r.occurred_at)
            FROM users u JOIN codex_api_requests r ON r.user_id = u.id
            {where}
            GROUP BY u.id, u.username ORDER BY cost DESC, u.id ASC LIMIT %s""",
        params,
    )
    return [
        {'rank': i + 1, 'username': row[1], 'cost_usd': row[2],
         'total_tokens': row[3], 'requests': row[4], 'unpriced_requests': row[5],
         'last_activity_at': row[6].isoformat() if row[6] else None}
        for i, row in enumerate(await cur.fetchall())
    ]


class CodexStatsCache:
    """Short, bounded public cache so viewers share aggregate queries.

    Personal usage bypasses this cache. A lock also coalesces simultaneous misses.
    No background tasks or queries when nobody is viewing Codex.
    """
    def __init__(self, ttl=2.0, max_entries=128):
        self.ttl = ttl
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, key, build):
        async with self.lock:
            now = time.monotonic()
            cached = self.entries.get(key)
            if cached is not None and cached[0] > now:
                self.entries.move_to_end(key)
                return cached[1]
            value = await build()
            self.entries[key] = (time.monotonic() + self.ttl, value)
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            return value
