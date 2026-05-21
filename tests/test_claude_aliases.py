"""After the rename, /v1/logs and /v1/metrics MUST keep working as aliases
of /claude/v1/logs and /claude/v1/metrics. This protects every live Claude
Code install that has OTEL_EXPORTER_OTLP_ENDPOINT=http://<host> (no /claude
prefix) in its settings.json.
"""
INGEST_HEADERS = {
    "Authorization": "Bearer test-token",
    "Content-Type": "application/x-protobuf",
}


async def test_v1_logs_alias_still_routes(client, fake_pool, allow_all_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post("/v1/logs", content=b"", headers=INGEST_HEADERS)
    assert r.status_code == 200


async def test_v1_metrics_alias_still_routes(client, fake_pool, allow_all_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post("/v1/metrics", content=b"", headers=INGEST_HEADERS)
    assert r.status_code == 200


async def test_claude_v1_logs_canonical_routes(client, fake_pool, allow_all_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post("/claude/v1/logs", content=b"", headers=INGEST_HEADERS)
    assert r.status_code == 200


async def test_claude_v1_metrics_canonical_routes(client, fake_pool, allow_all_rates):
    fake_pool.cursor.next_row = (42,)
    r = await client.post("/claude/v1/metrics", content=b"", headers=INGEST_HEADERS)
    assert r.status_code == 200
