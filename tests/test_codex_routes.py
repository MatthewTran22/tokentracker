"""Codex route authentication, payload validation and isolation from Claude."""
import pytest
from tokentracker.security import current_user_session
from test_codex_otlp_validation import _payload

HEADERS = {'Authorization': 'Bearer codex-test-token', 'Content-Type': 'application/x-protobuf'}


@pytest.fixture
def codex_rates(monkeypatch):
    calls = []
    async def allow(pool, bucket, limit, window_seconds):
        calls.append((bucket, limit, window_seconds))
        return True
    monkeypatch.setattr('tokentracker.codex_routes.check_rate', allow)
    return calls


async def test_codex_requires_own_token(client, fake_pool):
    assert (await client.post('/codex/v1/logs', content=b'')).status_code == 401
    fake_pool.cursor.next_row = None
    assert (await client.post('/codex/v1/logs', content=b'', headers=HEADERS)).status_code == 401
    sql, _ = fake_pool.cursor.executions[-1]
    assert 'codex_ingest_token_hash' in sql


async def test_codex_ingestion_stores_only_usage(client, fake_pool, codex_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post('/codex/v1/logs', content=_payload(), headers=HEADERS)
    assert r.status_code == 200
    sql, rows = fake_pool.cursor.executions[-1]
    assert 'INSERT INTO codex_api_requests' in sql
    assert 'ON CONFLICT (user_id, dedup_key) DO NOTHING' in sql
    assert rows[0]['user_id'] == 42
    assert rows[0]['input_tokens'] == 1234
    assert rows[0]['cost_usd'] is not None
    assert 'prompt' not in rows[0]
    assert codex_rates == [('codex:ingest:user:42', 600, 60)]


async def test_unknown_model_keeps_tokens_without_price(client, fake_pool, codex_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post('/codex/v1/logs', content=_payload(model='future-model'), headers=HEADERS)
    assert r.status_code == 200
    row = fake_pool.cursor.executions[-1][1][0]
    assert row['cost_usd'] is None
    assert row['input_tokens'] == 1234


async def test_non_usage_events_are_discarded(client, fake_pool, codex_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post('/codex/v1/logs', content=_payload(event_name='codex.user_prompt'), headers=HEADERS)
    assert r.status_code == 200
    assert not any('INSERT' in sql for sql, _ in fake_pool.cursor.executions)


async def test_malformed_payload_and_wrong_media_type(client, fake_pool, codex_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post('/codex/v1/logs', content=b'not protobuf', headers=HEADERS)
    assert r.status_code == 400
    r = await client.post('/codex/v1/logs', content=b'{}', headers={**HEADERS, 'Content-Type': 'application/json'})
    assert r.status_code == 415


async def test_codex_rate_limit(client, fake_pool, monkeypatch):
    async def deny(*args, **kwargs):
        return False
    monkeypatch.setattr('tokentracker.codex_routes.check_rate', deny)
    fake_pool.cursor.next_row = (42,)
    r = await client.post('/codex/v1/logs', content=_payload(), headers=HEADERS)
    assert r.status_code == 429
    assert not any('INSERT' in sql for sql, _ in fake_pool.cursor.executions)


async def test_codex_rotation_requires_session_and_only_rotates_codex(client, test_app, fake_pool):
    assert (await client.post('/me/codex-ingest-token/rotate')).status_code == 401
    test_app.dependency_overrides[current_user_session] = lambda: {'id': 42, 'username': 'tester'}
    r = await client.post('/me/codex-ingest-token/rotate')
    assert r.status_code == 200
    token = r.json()['ingest_token']
    sql, params = fake_pool.cursor.executions[-1]
    assert 'SET codex_ingest_token_hash' in sql
    assert params[0] != token
    assert params[1] == 42


async def test_personal_codex_usage_requires_session(client):
    assert (await client.get('/usage/me/codex')).status_code == 401


async def test_codex_leaderboard_exact_filter_and_limits(client, fake_pool):
    fake_pool.cursor.next_rows = [(42, 'tester', 1.2, 250, 2, 1, None)]
    r = await client.get('/stats/codex/leaderboard', params={'model': 'model_%', 'limit': 9999})
    assert r.status_code == 200
    assert r.json()['rows'][0]['unpriced_requests'] == 1
    sql, params = fake_pool.cursor.executions[-1]
    assert "COALESCE(r.model, 'unknown') = %s" in sql
    assert params == ('model_%', 500)
