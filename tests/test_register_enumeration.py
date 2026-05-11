"""Register endpoint must respond uniformly on unique-constraint collisions.

User-controllable unique constraints today: `username` and `lower(email)`.
We collapse any UniqueViolation to a single generic 409 so /register can't
be used to enumerate which usernames/emails are already registered.
"""
from psycopg import errors as pg_errors


async def _register(client, username="alice", password="pw12345678", email="alice@example.com"):
    return await client.post(
        "/register",
        json={"username": username, "password": password, "email": email},
    )


async def test_username_collision_returns_generic_409(
    client, fake_pool, allow_all_rates
):
    distinctive = "zzz_collision_marker"
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_username_key\". "
        f"DETAIL:  Key (username)=({distinctive}) already exists."
    )
    r = await _register(client, username=distinctive)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert distinctive not in detail
    assert "violates" not in detail.lower()
    assert "constraint" not in detail.lower()


async def test_email_collision_returns_same_generic_409(
    client, fake_pool, allow_all_rates
):
    """Email-collision message must be byte-identical to username-collision
    so an attacker can't tell which field they hit."""
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_email_lower_idx\". "
        "DETAIL:  Key (lower(email))=(taken@example.com) already exists."
    )
    r_email = await _register(client, email="taken@example.com")
    assert r_email.status_code == 409

    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_username_key\". "
        "DETAIL:  Key (username)=(alice) already exists."
    )
    r_user = await _register(client)
    assert r_user.status_code == 409

    assert r_email.json()["detail"] == r_user.json()["detail"]
