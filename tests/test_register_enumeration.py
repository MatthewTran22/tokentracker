"""Register endpoint must not reveal which constraint collided.

Risk: returning "email already registered" vs. "username already taken" lets
an attacker enumerate which emails are registered. We collapse both (and any
future unique constraint) to a single generic 409.
"""
from psycopg import errors as pg_errors


async def _register(client, email="a@b.co", password="pw12345678", username=None):
    payload = {"email": email, "password": password}
    if username is not None:
        payload["username"] = username
    return await client.post("/register", json=payload)


async def test_email_collision_returns_generic_409(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_email_key\". "
        "DETAIL:  Key (email)=(taken@example.com) already exists."
    )
    r = await _register(client, email="taken@example.com")
    assert r.status_code == 409
    detail = r.json()["detail"]
    # Must not name the colliding column or repeat the email.
    assert "email" not in detail.lower()
    assert "taken@example.com" not in detail


async def test_username_collision_returns_same_generic_409(
    client, fake_pool, allow_all_rates
):
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "duplicate key value violates unique constraint \"users_username_key\". "
        "DETAIL:  Key (username)=(taken) already exists."
    )
    r = await _register(client, email="new@example.com", username="taken")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "username" not in detail.lower()


async def test_email_and_username_collisions_have_identical_response(
    client, fake_pool, allow_all_rates
):
    """Bytewise-identical bodies for both collision kinds; otherwise an
    attacker can still distinguish."""
    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "Key (email)=(a@b.co) already exists."
    )
    r1 = await _register(client, email="a@b.co")

    fake_pool.cursor.raise_on_execute = pg_errors.UniqueViolation(
        "Key (username)=(bob) already exists."
    )
    r2 = await _register(client, email="c@d.co", username="bob")

    assert r1.status_code == r2.status_code == 409
    assert r1.json() == r2.json()
