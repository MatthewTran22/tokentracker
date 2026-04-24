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
from .security import current_user, generate_token, hash_token

router = APIRouter()

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")


def _validate_username(u: str) -> str:
    u = u.strip()
    if not _USERNAME_RE.fullmatch(u):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "username must be 2-32 chars: letters, digits, underscore, hyphen",
        )
    return u


class RegisterIn(BaseModel):
    email: str | None = None
    username: str | None = Field(default=None)


class RegisterOut(BaseModel):
    user_id: int
    token: str
    username: str | None


@router.post("/register", response_model=RegisterOut)
async def register(body: RegisterIn, request: Request) -> RegisterOut:
    username = _validate_username(body.username) if body.username else None
    token = generate_token()
    token_hash = hash_token(token)
    pool = request.app.state.pool
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO users (email, username, token_hash) VALUES (%s, %s, %s) RETURNING id",
                    (body.email, username, token_hash),
                )
                row = await cur.fetchone()
    except pg_errors.UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    return RegisterOut(user_id=row[0], token=token, username=username)


class UsernameIn(BaseModel):
    username: str


@router.patch("/me/username")
async def set_username(
    body: UsernameIn, request: Request, user: dict = Depends(current_user)
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


@router.post("/v1/logs")
async def ingest_logs(request: Request, user: dict = Depends(current_user)) -> Response:
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
async def ingest_metrics(request: Request, user: dict = Depends(current_user)) -> Response:
    # v1 stub: accept and discard metrics so clients with OTEL_METRICS_EXPORTER=otlp
    # don't receive errors. All per-request usage data is already captured via /v1/logs.
    return Response(
        content=ExportMetricsServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


@router.get("/usage/me")
async def usage_me(request: Request, user: dict = Depends(current_user)) -> dict:
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
                    COALESCE(sum(r.cost_usd), 0)::float   AS total_cost_usd,
                    COALESCE(sum(r.billed_tokens), 0)     AS billed_tokens,
                    count(r.id)::bigint                   AS requests,
                    max(r.occurred_at)                    AS last_activity_at
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
