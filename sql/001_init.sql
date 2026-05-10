CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    ingest_token_hash   TEXT NOT NULL UNIQUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS api_requests (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    billed_tokens INTEGER,
    cost_usd      NUMERIC(18, 10),
    request_id    TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    -- Source IP at ingest time. Powers the cross-user per-IP cap so
    -- multi-accounting from a single host can't scale leaderboard impact.
    ingest_ip     TEXT,
    UNIQUE (user_id, request_id)
);

CREATE INDEX IF NOT EXISTS api_requests_user_occurred_idx
    ON api_requests (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS api_requests_ingest_ip_occurred_idx
    ON api_requests (ingest_ip, occurred_at DESC);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket  TEXT NOT NULL,
    hit_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rate_limits_bucket_hit_idx
    ON rate_limits (bucket, hit_at DESC);
