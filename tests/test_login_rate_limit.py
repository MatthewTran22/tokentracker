"""Login must rate-limit on a per-email bucket in addition to per-IP.

Risk: per-IP-only limit lets an attacker behind a botnet credential-spray any
account, since each IP gets its own 10/min budget. A per-email bucket caps
how often any single account can be tried regardless of source IP.

Tradeoff: a malicious actor can lock a victim's email out for the window by
spamming. We accept that — without MFA the password is the sole gate, and a
short lockout is much better than uncapped guessing.
"""


async def _login(client, email="victim@example.com", password="badguess"):
    return await client.post(
        "/login", json={"email": email, "password": password}
    )


async def test_login_consults_both_ip_and_email_buckets(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.next_row = None  # user-not-found path
    await _login(client, email="victim@example.com")

    buckets = [b for b, _l, _w in allow_all_rates]
    has_ip = any("127.0.0.1" in b for b in buckets)
    has_email = any("victim@example.com" in b for b in buckets)
    assert has_ip, f"expected an IP bucket among {buckets!r}"
    assert has_email, f"expected an email bucket among {buckets!r}"


async def test_login_email_bucket_is_lowercased(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.next_row = None
    await _login(client, email="Victim@Example.COM")
    assert any("victim@example.com" in b for b, *_ in allow_all_rates)
    assert not any("Victim@Example.COM" in b for b, *_ in allow_all_rates)


async def test_login_blocked_when_email_bucket_full(
    client, fake_pool, monkeypatch
):
    """Email bucket exceeded → 429 even if IP bucket has room."""

    async def fake_check_rate(pool, bucket, limit, window_seconds):
        # Allow IP bucket, block any email bucket.
        return "@" not in bucket

    monkeypatch.setattr("tokentracker.routes.check_rate", fake_check_rate)
    r = await _login(client)
    assert r.status_code == 429


async def test_login_blocked_when_ip_bucket_full(
    client, fake_pool, block_all_rates
):
    """Pre-existing behavior: IP bucket exceeded → 429."""
    r = await _login(client)
    assert r.status_code == 429
