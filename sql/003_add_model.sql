ALTER TABLE api_requests
    ADD COLUMN IF NOT EXISTS model TEXT;

CREATE INDEX IF NOT EXISTS api_requests_user_model_idx
    ON api_requests (user_id, model);
