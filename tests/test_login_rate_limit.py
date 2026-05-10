"""Login must rate-limit on a per-username bucket in addition to per-IP.

Risk: per-IP-only limit lets an attacker behind a botnet credential-spray any
account, since each IP gets its own 10/min budget. A per-username bucket caps
how often any single account can be tried regardless of source IP.

Tradeoff: a malicious actor can lock a victim's username out for the window by
spamming. We accept that — without MFA the password is the sole gate, and a
short lockout is much better than uncapped guessing.
"""


async def _login(client, username="victim", password="badguess"):
    return await client.post(
        "/login", json={"username": username, "password": password}
    )


async def test_login_consults_both_ip_and_username_buckets(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.next_row = None  # user-not-found path
    await _login(client, username="victim")

    buckets = [b for b, _l, _w in allow_all_rates]
    has_ip = any("127.0.0.1" in b for b in buckets)
    has_username = any("login:username:victim" in b for b in buckets)
    assert has_ip, f"expected an IP bucket among {buckets!r}"
    assert has_username, f"expected a username bucket among {buckets!r}"


async def test_login_blocked_when_username_bucket_full(
    client, fake_pool, monkeypatch
):
    """Username bucket exceeded → 429 even if IP bucket has room."""

    async def fake_check_rate(pool, bucket, limit, window_seconds):
        # Allow IP bucket, block any username bucket.
        return not bucket.startswith("login:username:")

    monkeypatch.setattr("tokentracker.routes.check_rate", fake_check_rate)
    r = await _login(client)
    assert r.status_code == 429


async def test_login_blocked_when_ip_bucket_full(
    client, fake_pool, block_all_rates
):
    """Pre-existing behavior: IP bucket exceeded → 429."""
    r = await _login(client)
    assert r.status_code == 429
