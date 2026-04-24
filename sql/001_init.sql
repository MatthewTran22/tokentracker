CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT,
    username    TEXT UNIQUE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_requests (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    billed_tokens INTEGER,
    cost_usd      NUMERIC(18, 10),
    request_id    TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, request_id)
);

CREATE INDEX IF NOT EXISTS api_requests_user_occurred_idx
    ON api_requests (user_id, occurred_at DESC);
