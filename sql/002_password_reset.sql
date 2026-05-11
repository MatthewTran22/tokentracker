ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx
    ON users (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS password_resets (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS password_resets_user_idx
    ON password_resets (user_id);
CREATE INDEX IF NOT EXISTS password_resets_expires_idx
    ON password_resets (expires_at);
