import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from .otlp import parse_api_request_events
from .ratelimit import check_rate
from .security import (
    clear_session_cookie,
    current_user_ingest,
    current_user_session,
    generate_token,
    hash_password,
    hmac_hash,
    new_session,
    set_session_cookie,
    verify_password_safe,
)

router = APIRouter()

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_username(u: str) -> str:
    u = u.strip()
    if not _USERNAME_RE.fullmatch(u):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "username must be 2-32 chars: letters, digits, underscore, hyphen",
        )
    return u


def _validate_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid email")
    return email


def _validate_password(pw: str) -> None:
    if not isinstance(pw, str) or len(pw) < 8:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "password must be at least 8 characters"
        )
    if len(pw) > 128:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password too long (max 128)")


_TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"


def _client_key(request: Request) -> str:
    # When TRUST_PROXY=1, honor proxy-set IP headers in this order of
    # preference: x-real-ip (Vercel/nginx), cf-connecting-ip (Cloudflare),
    # then leftmost x-forwarded-for. Set this ONLY when the immediate
    # upstream is a trusted proxy — otherwise any client can spoof the
    # header to bypass rate limits.
    if _TRUST_PROXY:
        for hdr in ("x-real-ip", "cf-connecting-ip"):
            v = request.headers.get(hdr)
            if v and v.strip():
                return v.strip()
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            first = fwd.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"


# ---- auth: register ----

class RegisterIn(BaseModel):
    email: str
    password: str
    username: str | None = Field(default=None)


class RegisterOut(BaseModel):
    user_id: int
    username: str | None
    ingest_token: str


@router.post("/register", response_model=RegisterOut)
async def register(body: RegisterIn, request: Request, response: Response) -> RegisterOut:
    pool = request.app.state.pool
    if not await check_rate(pool, f"register:{_client_key(request)}", limit=3, window_seconds=300):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many registrations, try later")

    email = _validate_email(body.email)
    _validate_password(body.password)
    username = _validate_username(body.username) if body.username else None

    password_hash = hash_password(body.password)
    ingest_token = generate_token()
    ingest_token_hash = hmac_hash(ingest_token)
    session_token, session_hash, expires_at = new_session()

    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO users (email, username, password_hash, ingest_token_hash)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (email, username, password_hash, ingest_token_hash),
                    )
                    user_id = (await cur.fetchone())[0]
                    await cur.execute(
                        "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                        (user_id, session_hash, expires_at),
                    )
    except pg_errors.UniqueViolation:
        # Generic message for ALL unique violations — distinguishing
        # email vs. username collisions lets an attacker enumerate which
        # emails are registered (an email collision yields a different
        # response than a fresh email + duplicate username).
        raise HTTPException(status.HTTP_409_CONFLICT, "registration failed")

    set_session_cookie(response, session_token)
    return RegisterOut(user_id=user_id, username=username, ingest_token=ingest_token)


# ---- auth: login ----

class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    pool = request.app.state.pool
    email = body.email.strip().lower()

    # Two buckets: the IP bucket caps abuse from a single source; the email
    # bucket caps how often any one account can be tried, so an attacker
    # rotating IPs cannot credential-spray a target email.
    ip_bucket = f"login:ip:{_client_key(request)}"
    email_bucket = f"login:email:{email}"
    if not await check_rate(pool, ip_bucket, limit=10, window_seconds=60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")
    if not await check_rate(pool, email_bucket, limit=10, window_seconds=300):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, password_hash, username FROM users WHERE email = %s",
                (email,),
            )
            row = await cur.fetchone()

    if row is None:
        # Still verify against a dummy hash to equalize timing.
        verify_password_safe(body.password, None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    user_id, password_hash, username = row
    if not verify_password_safe(body.password, password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    session_token, session_hash, expires_at = new_session()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                (user_id, session_hash, expires_at),
            )

    set_session_cookie(response, session_token)
    return {"user_id": user_id, "username": username}


# ---- auth: logout ----

@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    cookie = request.cookies.get("tt_session")
    if cookie:
        pool = request.app.state.pool
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM sessions WHERE token_hash = %s",
                    (hmac_hash(cookie),),
                )
    clear_session_cookie(response)
    return {"ok": True}


# ---- session-authed account routes ----

@router.get("/me")
async def me(user: dict = Depends(current_user_session)) -> dict:
    return {"user_id": user["id"], "username": user["username"]}


class UsernameIn(BaseModel):
    username: str


@router.patch("/me/username")
async def set_username(
    body: UsernameIn, request: Request, user: dict = Depends(current_user_session)
) -> dict:
    username = _validate_username(body.username)
    pool = request.app.state.pool
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET username = %s WHERE id = %s RETURNING username",
                    (username, user["id"]),
                )
                row = await cur.fetchone()
    except pg_errors.UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    return {"user_id": user["id"], "username": row[0]}


@router.post("/me/ingest-token/rotate")
async def rotate_ingest_token(
    request: Request, user: dict = Depends(current_user_session)
) -> dict:
    new_token = generate_token()
    new_hash = hmac_hash(new_token)
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET ingest_token_hash = %s WHERE id = %s",
                (new_hash, user["id"]),
            )
    return {"ingest_token": new_token}


@router.get("/usage/me")
async def usage_me(request: Request, user: dict = Depends(current_user_session)) -> dict:
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    u.username,
                    count(r.id)::bigint                  AS requests,
                    COALESCE(sum(r.billed_tokens), 0)    AS billed_tokens,
                    COALESCE(sum(r.cost_usd), 0)::float  AS total_cost_usd,
                    max(r.occurred_at)                   AS last_activity_at
                FROM users u
                LEFT JOIN api_requests r ON r.user_id = u.id
                WHERE u.id = %s
                GROUP BY u.id, u.username
                """,
                (user["id"],),
            )
            row = await cur.fetchone()
    return {
        "user_id": user["id"],
        "username": row[0],
        "requests": row[1],
        "billed_tokens": row[2],
        "total_cost_usd": row[3],
        "last_activity_at": row[4].isoformat() if row[4] else None,
    }


# ---- ingest (Bearer-authed via ingest token) ----

_INGEST_LIMIT = 600       # per user, per window
_INGEST_WINDOW = 60       # seconds


def _ingest_bucket(user_id: int) -> str:
    return f"ingest:user:{user_id}"


@router.post("/v1/logs")
async def ingest_logs(
    request: Request, user: dict = Depends(current_user_ingest)
) -> Response:
    pool = request.app.state.pool

    # Cap per-user ingest. Without this an authed user (or whoever holds a
    # leaked token) can pump unbounded rows into api_requests by varying
    # request_id. 600/min is well above legitimate Claude Code traffic.
    if not await check_rate(
        pool, _ingest_bucket(user["id"]), limit=_INGEST_LIMIT, window_seconds=_INGEST_WINDOW
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "ingest rate limit exceeded"
        )

    ctype = request.headers.get("content-type", "")
    if "protobuf" not in ctype:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "expected application/x-protobuf",
        )

    raw = await request.body()
    events = list(parse_api_request_events(raw))

    if events:
        pool = request.app.state.pool
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO api_requests (
                        user_id, billed_tokens, cost_usd, request_id, occurred_at
                    ) VALUES (
                        %(user_id)s, %(billed_tokens)s, %(cost_usd)s, %(request_id)s, %(occurred_at)s
                    )
                    ON CONFLICT (user_id, request_id) DO NOTHING
                    """,
                    [
                        {
                            **e,
                            "user_id": user["id"],
                            "occurred_at": e["occurred_at"] or datetime.now(timezone.utc),
                        }
                        for e in events
                    ],
                )

    return Response(
        content=ExportLogsServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


@router.post("/v1/metrics")
async def ingest_metrics(
    request: Request, user: dict = Depends(current_user_ingest)
) -> Response:
    # v1 stub: accept and discard so clients with OTEL_METRICS_EXPORTER=otlp don't error.
    # Shared rate-limit bucket with /v1/logs so the per-user cap covers both.
    pool = request.app.state.pool
    if not await check_rate(
        pool, _ingest_bucket(user["id"]), limit=_INGEST_LIMIT, window_seconds=_INGEST_WINDOW
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "ingest rate limit exceeded"
        )
    return Response(
        content=ExportMetricsServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


# ---- public stats ----

@router.get("/stats/totals")
async def stats_totals(request: Request) -> dict:
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    (SELECT count(*)::bigint FROM users)    AS total_users,
                    count(*)::bigint                        AS total_requests,
                    COALESCE(sum(billed_tokens), 0)         AS billed_tokens,
                    COALESCE(sum(cost_usd), 0)::float       AS total_cost_usd,
                    max(occurred_at)                        AS last_activity_at
                FROM api_requests
                """
            )
            row = await cur.fetchone()
    return {
        "total_users": row[0],
        "total_requests": row[1],
        "billed_tokens": row[2],
        "total_cost_usd": row[3],
        "last_activity_at": row[4].isoformat() if row[4] else None,
    }


@router.get("/stats/leaderboard")
async def stats_leaderboard(request: Request, limit: int = 100) -> dict:
    limit = max(1, min(limit, 500))
    pool = request.app.state.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    u.id,
                    COALESCE(u.username, 'user ' || u.id::text) AS handle,
                    COALESCE(sum(r.cost_usd), 0)::float         AS total_cost_usd,
                    COALESCE(sum(r.billed_tokens), 0)           AS billed_tokens,
                    count(r.id)::bigint                         AS requests,
                    max(r.occurred_at)                          AS last_activity_at
                FROM users u
                LEFT JOIN api_requests r ON r.user_id = u.id
                GROUP BY u.id, u.username
                ORDER BY total_cost_usd DESC, u.id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    return {
        "rows": [
            {
                "rank": i + 1,
                "user_id": r[0],
                "handle": r[1],
                "total_cost_usd": r[2],
                "billed_tokens": r[3],
                "requests": r[4],
                "last_activity_at": r[5].isoformat() if r[5] else None,
            }
            for i, r in enumerate(rows)
        ]
    }


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
