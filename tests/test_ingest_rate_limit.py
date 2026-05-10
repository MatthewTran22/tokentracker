"""POST /v1/logs must rate-limit per authenticated user.

Risk: an attacker holding a leaked ingest token can spam unique request_id
values and inflate the api_requests table without bound. Rate-limiting per
user caps that. The cap (600/min) sits well above any realistic Claude Code
traffic — heavy users typically emit a few exports per second per session.
"""

INGEST_HEADERS = {
    "Authorization": "Bearer test-token",
    "Content-Type": "application/x-protobuf",
}


async def _ingest(client, body: bytes = b""):
    return await client.post("/v1/logs", content=body, headers=INGEST_HEADERS)


async def test_ingest_consults_per_user_rate_bucket(
    client, fake_pool, allow_all_rates
):
    # Auth lookup: token_hash → user_id 42
    fake_pool.cursor.next_row = (42,)
    r = await _ingest(client)
    assert r.status_code == 200

    # Some bucket must depend on the authed user id.
    assert any("42" in b for b, *_ in allow_all_rates), (
        f"expected bucket containing user id, got {allow_all_rates!r}"
    )


async def test_ingest_429_when_user_bucket_full(
    client, fake_pool, monkeypatch
):
    fake_pool.cursor.next_row = (42,)

    async def fake_check_rate(pool, bucket, limit, window_seconds):
        return False  # always block

    monkeypatch.setattr("tokentracker.routes.check_rate", fake_check_rate)
    r = await _ingest(client)
    assert r.status_code == 429


async def test_ingest_distinct_buckets_per_user(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.next_row = (1,)
    await _ingest(client)
    user1_buckets = {b for b, *_ in allow_all_rates}
    allow_all_rates.clear()

    fake_pool.cursor.next_row = (2,)
    await _ingest(client)
    user2_buckets = {b for b, *_ in allow_all_rates}

    assert user1_buckets != user2_buckets, (
        "ingest rate-limit bucket must depend on the authed user"
    )


async def test_ingest_unauthenticated_still_401_no_rate_check(
    client, fake_pool, allow_all_rates
):
    """Auth must run first; unauthed requests don't burn rate budget."""
    fake_pool.cursor.next_row = None  # no matching token
    r = await client.post(
        "/v1/logs",
        content=b"",
        headers={"Authorization": "Bearer nope", "Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 401
    assert allow_all_rates == []
