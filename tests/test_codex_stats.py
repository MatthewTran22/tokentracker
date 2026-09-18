import asyncio
from tokentracker.codex_stats import CodexStatsCache, fetch_codex_totals


class Cursor:
    def __init__(self):
        self.calls = []
    async def execute(self, sql, params):
        self.calls.append((sql, params))
    async def fetchone(self):
        return (1, 2, 150, 0.002, 1, None, 100, 50, 80, 20)
    async def fetchall(self):
        return [('gpt-5.1', 0.002, 100, 1, 0), ('new-model', 0.0, 50, 1, 1)]


async def test_personal_totals_and_breakdown_scope_to_user():
    cur = Cursor()
    result = await fetch_codex_totals(cur, user_id=42)
    assert result['total_tokens'] == 150
    assert result['cached_input_tokens'] == 80
    assert result['reasoning_output_tokens'] == 20
    assert result['unpriced_requests'] == 1
    assert result['by_model']['new-model']['unpriced_requests'] == 1
    assert all(params == (42,) and 'WHERE user_id = %s' in sql for sql, params in cur.calls)


async def test_public_cache_coalesces_requests_and_expires():
    cache = CodexStatsCache(ttl=60)
    calls = []
    async def build():
        calls.append(1)
        await asyncio.sleep(0)
        return {'value': len(calls)}
    results = await asyncio.gather(*(cache.get('totals', build) for _ in range(10)))
    assert all(r == {'value': 1} for r in results)
    cache.entries['totals'] = (0, {'value': 1})
    assert await cache.get('totals', build) == {'value': 2}


async def test_public_cache_is_bounded_and_isolates_models():
    cache = CodexStatsCache(max_entries=2)
    async def one(): return 1
    async def two(): return 2
    assert await cache.get(('model', 'a'), one) == 1
    assert await cache.get(('model', 'b'), two) == 2
    await cache.get(('model', 'c'), one)
    assert len(cache.entries) == 2
    assert ('model', 'a') not in cache.entries


async def test_current_codex_models_are_listed_before_any_usage():
    result = await fetch_codex_totals(Cursor())
    names = list(result['by_model'])
    assert names[:4] == ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna']
    for name in names[:4]:
        assert result['by_model'][name] == {
            'cost_usd': 0.0, 'total_tokens': 0, 'requests': 0, 'unpriced_requests': 0,
        }
    assert result['by_model']['new-model']['unpriced_requests'] == 1
