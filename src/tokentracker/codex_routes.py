"""Codex connections, usage-only OTLP ingestion and provider-specific stats."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse

from .codex_otlp import parse_codex_response_events
from .codex_pricing import compute_cost_usd
from .codex_stats import CodexStatsCache, fetch_codex_leaderboard, fetch_codex_totals
from .ratelimit import check_rate
from .security import current_user_session, generate_token, hmac_hash

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


async def current_user_codex_ingest(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if creds is None:
        raise HTTPException(401, 'Codex ingest token required')
    async with request.app.state.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                'SELECT id FROM users WHERE codex_ingest_token_hash = %s',
                (hmac_hash(creds.credentials),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(401, 'invalid Codex ingest token')
    return {'id': row[0]}


@router.post('/me/codex-ingest-token/rotate')
async def rotate_codex_token(request: Request, user: dict = Depends(current_user_session)):
    token = generate_token()
    async with request.app.state.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET codex_ingest_token_hash = %s WHERE id = %s',
                (hmac_hash(token), user['id']),
            )
    return {'ingest_token': token}


@router.post('/codex/v1/logs')
async def ingest_codex_logs(request: Request, user: dict = Depends(current_user_codex_ingest)):
    pool = request.app.state.pool
    if not await check_rate(pool, f"codex:ingest:user:{user['id']}", limit=600, window_seconds=60):
        raise HTTPException(429, 'ingest rate limit exceeded')
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/x-protobuf':
        raise HTTPException(415, 'expected application/x-protobuf')
    try:
        events = list(parse_codex_response_events(await request.body()))
    except (DecodeError, ValueError, OverflowError):
        raise HTTPException(400, 'invalid OTLP payload') from None
    if events:
        rows = []
        for event in events:
            rows.append({
                **event, 'user_id': user['id'],
                'cache_write_tokens': event.get('cache_write_tokens', 0),
                'occurred_at': event['occurred_at'] or datetime.now(timezone.utc),
                'cost_usd': compute_cost_usd(
                    event['model'], event['input_tokens'], event['cached_input_tokens'],
                    event['output_tokens'], event['reasoning_output_tokens'],
                    cache_write_tokens=event.get('cache_write_tokens', 0),
                ),
            })
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO codex_api_requests
                       (user_id, model, input_tokens, output_tokens, cached_input_tokens,
                        reasoning_output_tokens, tool_tokens, cache_write_tokens, cost_usd, dedup_key, occurred_at)
                       VALUES (%(user_id)s, %(model)s, %(input_tokens)s, %(output_tokens)s,
                               %(cached_input_tokens)s, %(reasoning_output_tokens)s,
                               %(tool_tokens)s, %(cache_write_tokens)s, %(cost_usd)s, %(dedup_key)s, %(occurred_at)s)
                       ON CONFLICT (user_id, dedup_key) DO NOTHING""", rows,
                )
    return Response(content=ExportLogsServiceResponse().SerializeToString(),
                    media_type='application/x-protobuf')


def _cache(request):
    cache = getattr(request.app.state, 'codex_stats_cache', None)
    if cache is None:
        cache = CodexStatsCache()
        request.app.state.codex_stats_cache = cache
    return cache


@router.get('/stats/codex/totals')
async def codex_totals(request: Request):
    async def build():
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor() as cur:
                return await fetch_codex_totals(cur)
    return await _cache(request).get(('totals',), build)


@router.get('/stats/codex/leaderboard')
async def codex_leaderboard(request: Request, limit: int = 100,
                            model: str | None = Query(default=None, max_length=256)):
    limit = max(1, min(limit, 500))
    model = model or None
    async def build():
        async with request.app.state.pool.connection() as conn:
            async with conn.cursor() as cur:
                return {'model': model, 'rows': await fetch_codex_leaderboard(cur, model, limit)}
    return await _cache(request).get(('board', model, limit), build)


@router.get('/usage/me/codex')
async def codex_usage_me(request: Request, user: dict = Depends(current_user_session)):
    async with request.app.state.pool.connection() as conn:
        async with conn.cursor() as cur:
            return {'user_id': user['id'], 'username': user['username'],
                    **await fetch_codex_totals(cur, user['id'])}
