"""Register endpoint must respond uniformly on unique-constraint collisions.

The user-controllable unique constraint is `username`. We collapse any
UniqueViolation (current or future) to a single generic 409 so route changes
that add new constraints don't accidentally start leaking which one tripped.
"""
from psycopg import errors as pg_errors


async def _register(client, username="alice", password="pw12345678"):
    return await client.post(
        "/register", json={"username": username, "password": password}
    )


async def test_username_collision_returns_409(
    client, fake_pool, allow_all_rates
):
    distinctive = "zzz_collision_marker"
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_username_key\". "
        f"DETAIL:  Key (username)=({distinctive}) already exists."
    )
    r = await _register(client, username=distinctive)
    assert r.status_code == 409
    # Body must not echo the input or pg's DETAIL.
    detail = r.json()["detail"]
    assert distinctive not in detail
    assert "violates" not in detail.lower()
    assert "constraint" not in detail.lower()
