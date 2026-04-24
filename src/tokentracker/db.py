import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool


async def _configure_connection(conn):
    # Transaction-mode poolers (Neon's pooler host, Supabase pooler, raw
    # pgbouncer) don't preserve server-side prepared statements across
    # transactions. psycopg3 prepares after 5 uses by default, which breaks
    # the next invocation. Set DB_DISABLE_PREPARE=1 when using such a pooler.
    if os.environ.get("DB_DISABLE_PREPARE") == "1":
        conn.prepare_threshold = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ["DATABASE_URL"]
    # Keep pool small by default — on serverless (Vercel) each warm container
    # holds its own pool and fan-out easily exhausts Postgres max_connections.
    # A downstream pooler (Neon / Supabase / pgbouncer) should do the real
    # multiplexing. Override via DB_POOL_MAX if you're deploying to a
    # long-lived VM with a single process.
    max_size = int(os.environ.get("DB_POOL_MAX", "2"))
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=max_size,
        open=False,
        configure=_configure_connection,
    )
    await pool.open(wait=True)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()
