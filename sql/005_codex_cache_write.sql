-- New clients report cache-write tokens separately. Older requests default to zero.
ALTER TABLE codex_api_requests
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS codex_api_requests_model_user_idx
    ON codex_api_requests (model, user_id);
