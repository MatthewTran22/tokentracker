"""Opt-in Postgres integration: TT_TEST_DATABASE_URL points to a LOCAL test DB.

All objects live in a uniquely named schema, dropped on completion. Existing
application data is untouched. Uses real auth, migrations, SQL, and HTTP routes.
"""
import os
import uuid
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool
import pytest
from test_codex_otlp_validation import _payload
from tokentracker.main import app

DSN = os.environ.get('TT_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='set TT_TEST_DATABASE_URL for local Postgres integration')


async def test_codex_end_to_end():
    schema = 'tt_test_' + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            scoped = make_conninfo(DSN, options=f'-c search_path={schema}')
            async with await psycopg.AsyncConnection.connect(scoped, autocommit=True) as setup:
                for migration in sorted((Path(__file__).parents[1] / 'sql').glob('*.sql')):
                    await setup.execute(migration.read_text())
            async with AsyncConnectionPool(scoped, min_size=1, max_size=2, open=False) as pool:
                app.state.pool = pool
                app.state.codex_stats_cache = None
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    reg = await client.post('/register', json={
                        'username': 'codex_tester', 'email': 'tester@example.com',
                        'password': 'Integration-only-password-42',
                    })
                    assert reg.status_code == 200, reg.text
                    claude_token = reg.json()['ingest_token']
                    token = (await client.post('/me/codex-ingest-token/rotate')).json()['ingest_token']
                    def headers(t): return {'Authorization': f'Bearer {t}', 'Content-Type': 'application/x-protobuf'}
                    payload = _payload(input_tokens=1000, cached_tokens=200, output_tokens=100, reasoning_tokens=50)
                    assert (await client.post('/codex/v1/logs', content=payload, headers=headers(claude_token))).status_code == 401
                    assert (await client.post('/v1/logs', content=b'', headers=headers(token))).status_code == 401
                    for _ in range(2):
                        assert (await client.post('/codex/v1/logs', content=payload, headers=headers(token))).status_code == 200
                    unknown = _payload(model='future-model', input_tokens=100, output_tokens=20, cached_tokens=20, reasoning_tokens=5)
                    assert (await client.post('/codex/v1/logs', content=unknown, headers=headers(token))).status_code == 200
                    mine = (await client.get('/usage/me/codex')).json()
                    assert mine['total_requests'] == 2  # retry deduplicated
                    assert mine['total_tokens'] == 1220  # cache/reasoning are subsets
                    assert mine['unpriced_requests'] == 1
                    assert mine['total_cost_usd'] == pytest.approx(0.002025)
                    totals = (await client.get('/stats/codex/totals')).json()
                    assert totals['total_requests'] == 2
                    assert totals['by_model']['future-model']['unpriced_requests'] == 1
                    board = (await client.get('/stats/codex/leaderboard?model=gpt-5.1')).json()
                    assert board['rows'][0]['requests'] == 1
                    assert board['rows'][0]['username'] == 'codex_tester'
                    assert (await client.get('/stats/codex/leaderboard?model=missing')).json()['rows'] == []
                    assert (await client.get('/stats/claude/totals')).json()['total_requests'] == 0
                    new_token = (await client.post('/me/codex-ingest-token/rotate')).json()['ingest_token']
                    assert new_token != token
                    assert (await client.post('/codex/v1/logs', content=b'', headers=headers(token))).status_code == 401
                    assert (await client.post('/codex/v1/logs', content=b'', headers=headers(new_token))).status_code == 200
                    # A second shared account has no personal usage and no ranked row.
                    await client.post('/logout')
                    reg2 = await client.post('/register', json={
                        'username': 'other_tester', 'email': 'other@example.com',
                        'password': 'Integration-only-password-43',
                    })
                    assert reg2.status_code == 200
                    assert (await client.get('/usage/me/codex')).json()['total_requests'] == 0
        finally:
            await admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
