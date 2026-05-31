-- Codex usage ingest: parallel to api_requests, separate token column on users.

ALTER TABLE users ADD COLUMN IF NOT EXISTS codex_ingest_token_hash TEXT;

-- Partial unique index: many users may have NULL, but two users cannot share a
-- non-NULL token hash.
CREATE UNIQUE INDEX IF NOT EXISTS users_codex_ingest_token_hash_idx
    ON users (codex_ingest_token_hash)
    WHERE codex_ingest_token_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS codex_api_requests (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model                    TEXT,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    cached_input_tokens      INTEGER,
    reasoning_output_tokens  INTEGER,
    tool_tokens              INTEGER,
    cost_usd                 NUMERIC(18, 10),
    dedup_key                TEXT NOT NULL,
    occurred_at              TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, dedup_key)
);

CREATE INDEX IF NOT EXISTS codex_api_requests_user_occurred_idx
    ON codex_api_requests (user_id, occurred_at DESC);
