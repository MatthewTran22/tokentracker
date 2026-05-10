import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

SESSION_COOKIE = "tt_session"
SESSION_TTL = timedelta(days=30)

_bearer = HTTPBearer(auto_error=False)
_ph = PasswordHasher()

_AUTH_SECRET = os.environ["AUTH_SECRET"].encode("utf-8")
if len(_AUTH_SECRET) < 32:
    raise RuntimeError("AUTH_SECRET must be at least 32 bytes")

# Precomputed dummy hash used to equalize timing when a login user doesn't exist.
_DUMMY_HASH = _ph.hash("timing_equalization_dummy_password_never_valid")


# ---- password hashing ----

def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password_safe(pw: str, pw_hash: str | None) -> bool:
    """Constant-ish time verify; still hashes when user missing."""
    target = pw_hash if pw_hash is not None else _DUMMY_HASH
    try:
        _ph.verify(target, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return pw_hash is not None


# ---- opaque token helpers ----

def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hmac_hash(token: str) -> str:
    return hmac.new(_AUTH_SECRET, token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_session() -> tuple[str, str, datetime]:
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    return token, hmac_hash(token), expires_at


# ---- cookie helpers ----

def _cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "0") == "1"


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )


# ---- dependencies ----

async def current_user_session(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """Authenticate via HttpOnly session cookie. For all web UI routes."""
    if not session_cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    token_hash = hmac_hash(session_cookie)
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sessions
                   SET last_seen_at = now()
                 WHERE token_hash = %s AND expires_at > now()
             RETURNING user_id
                """,
                (token_hash,),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
            await cur.execute(
                "SELECT id, username FROM users WHERE id = %s",
                (row[0],),
            )
            urow = await cur.fetchone()
    return {"id": urow[0], "username": urow[1]}


async def current_user_ingest(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Authenticate via Bearer ingest token. For /v1/logs and /v1/metrics only."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ingest token required")
    token_hash = hmac_hash(creds.credentials)
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM users WHERE ingest_token_hash = %s",
                (token_hash,),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ingest token")
    return {"id": row[0]}
