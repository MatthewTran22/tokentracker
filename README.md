# tokentracker

OTLP ingest server + public dashboards for Claude Code and Codex usage data.

The homepage shows both providers. Accounts are shared; dashboards, leaderboards, and ingest tokens are separate.

## Auth model

A user is identified by a **username** (2-32 chars, `[A-Za-z0-9_-]`) which doubles as login id and public handle on the leaderboard. An **email** is also collected at registration so users can self-serve a password reset.

Two secrets per user, issued at register time:

| Secret | Where it lives | Used for |
|---|---|---|
| **Password** | user's head / password manager | logging into the web dashboard |
| **Ingest token** | Claude Code's `settings.json` | POSTing OTLP telemetry to `/v1/logs`, `/v1/metrics` |

Why two: a stolen session cookie can read dashboards but can't forge usage data, and a stolen ingest token can forge usage data but can't see anything else. Both are stored server-side only as hashes — passwords with Argon2id, ingest tokens and session cookies with HMAC-SHA256 keyed by a server-side pepper (`AUTH_SECRET`).

Web sessions are HttpOnly, SameSite=Strict cookies — JavaScript cannot read them, so XSS can't lift them. Ingest tokens are sent as `Authorization: Bearer …` and never touch the browser.

## Quick start (local dev)

```bash
docker compose up -d
open http://localhost:8000/
```

Register in the UI → copy the **prompt** shown after registration → paste it into a Claude Code session. Claude Code will add the config to your user-level `settings.json` (the global per-machine one at `~/.claude/settings.json`, not a project-level `.claude/settings.json` inside a repo). The prompt looks like this:

```
Please add this config to my user-level Claude Code settings.json in my home .claude folder (~/.claude/settings.json on macOS/Linux, %USERPROFILE%\.claude\settings.json on Windows). If an "env" block already exists, merge these keys into it instead of overwriting. After saving, let me know so I can restart Claude Code (env vars are read at startup).

{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:8000",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <your-ingest-token>"
  }
}
```

Restart Claude Code. Events flow within ~5s. (Prefer to edit by hand? Paste just the JSON block into `~/.claude/settings.json` yourself.)

## Endpoints

**Public (no auth):**
- `GET /` — the SPA
- `GET /stats/claude/totals` — aggregate spend + counts
- `GET /stats/claude/leaderboard?limit=100` — ranked by cost, handles only
- `GET /stats/claude/stream` — Server-Sent Events; pushes a combined snapshot (totals + all five leaderboards) live to the dashboard. One shared snapshot is computed per interval and fanned out to every viewer, so database load is independent of audience size. The refresh loop only runs while at least one client is connected. The `/stats/claude/totals` and `/stats/claude/leaderboard` endpoints above remain for instant first paint and as the SSE-less fallback.
- `GET /healthz`

**Web session cookie (HttpOnly):**
- `POST /register` — username + email + password; sets cookie, returns ingest token
- `POST /login` — username + password; sets cookie
- `POST /logout` — clears cookie + deletes the session row
- `GET /me` — current user
- `GET /me/full` — current user including email (for the dashboard)
- `GET /usage/me/claude` — per-user totals
- `PATCH /me/username` — rename
- `PATCH /me/email` — set or change email
- `POST /me/claude-ingest-token/rotate` — invalidates the old ingest token, returns a new one

**Public (no auth, password reset):**
- `POST /password-reset/request` — `{email}`; always returns `{ok:true}` (no enumeration). If the email matches a user, mails them a one-hour reset link.
- `GET /password-reset/validate?token=…` — cheap probe used by the `/reset` page.
- `POST /password-reset/confirm` — `{token, password}`; sets the new password and revokes every existing session for that user.

**Ingest Bearer token (Claude Code only):**
- `POST /v1/logs` — OTLP/protobuf logs; only `claude_code.api_request` records are stored
- `POST /v1/metrics` — stub that accepts and discards

## Connect Codex

1. Register or sign in with your existing account, select **Codex → dashboard**, and create a Codex ingest token.
2. Copy the generated configuration into your user-level `~/.codex/config.toml` (Windows: `%USERPROFILE%\.codex\config.toml`). Merge the existing `[otel]` section; do not overwrite unrelated settings or add duplicate sections.
3. Restart Codex, generate a response, and wait for telemetry to export. The configuration works with clients supporting the documented OTel exporter; CLI `0.154.0-alpha.6.2` was verified locally. Desktop export requires the app to load that configuration after restart.

```toml
[otel]
log_user_prompt = false
exporter = { otlp-http = { endpoint = "http://localhost:8000/codex/v1/logs", protocol = "binary", headers = { "Authorization" = "Bearer <your-codex-ingest-token>" } } }
```

Use the deployed HTTPS origin when tracking against production. A local endpoint only receives telemetry from Codex running on that machine; cloud tasks need a reachable server. Tokens are shown once and stored only as hashes. Rotating the Codex token does not change Claude's token.

If Codex returns HTTP 401 while the token works directly, check inherited `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_LOGS_HEADERS`: exporter environment headers can override the TOML token. Launch Codex without conflicting OTel variables (for example, `env -u OTEL_EXPORTER_OTLP_HEADERS -u OTEL_EXPORTER_OTLP_LOGS_HEADERS codex` on macOS/Linux). Keep other tools’ telemetry configuration intact.

The exporter may send other event metadata, including tool-result snippets. TokenTracker discards non-usage events and persists only allowlisted usage fields, model, hashed dedup key, and timestamps. `log_user_prompt = false` keeps prompt logging disabled. Do not enable HTTP request-body logging at a proxy in front of ingestion.

### Codex usage and estimates

- Total tokens = input + output. Cached input, cache writes, and reasoning are subsets, never added again.
- Dollar amounts estimate **standard API-equivalent cost**, not ChatGPT subscription charges. Rates and sources are recorded in `codex_pricing.py` (verified 2026-09-17). Service tiers, regional premiums, and hosted-tool fees are not included. GPT-5.4/5.5 long-context session premiums are approximated per response; newer supported models use request-level thresholds.
- Unknown models retain tokens and requests with a null cost. The UI reports unpriced requests and excludes them from dollar estimates. Adding a rate affects new ingested events; it does not retroactively reprice stored events.
- Retry deduplication prefers a response ID, then conversation + event timestamp (or observed timestamp on the verified client). Identical retries count once. A collector that regenerates observed timestamps cannot guarantee deduplication without a stable response ID.
- Global totals/leaderboards share a bounded two-second cache. Personal usage is session-protected; public output contains handles and aggregate usage only.

### Codex endpoints

- `POST /me/codex-ingest-token/rotate` — session-authenticated create/rotate; returns `ingest_token` once.
- `POST /codex/v1/logs` — Codex Bearer token + `application/x-protobuf`; usage-only OTLP ingestion.
- `GET /usage/me/codex` — session-authenticated personal totals and model breakdown.
- `GET /stats/codex/totals` — public totals and model breakdown, including unpriced request counts.
- `GET /stats/codex/leaderboard?model=<exact-model-id>&limit=100` — public Codex ranking; omit model for all usage. The `unknown` model bucket includes missing model identifiers.

Browser routes are `#/codex/me`, `#/codex/leaderboard`, and `#/codex/setup`. Existing Claude URLs remain compatible.

### Existing installations

Apply all idempotent SQL migrations before starting the new app, including `003_codex.sql` and `005_codex_cache_write.sql`. The GitHub deploy workflow already does this. Docker's initialization mount only applies migrations on a fresh volume; for an existing local volume run:

```bash
for f in sql/*.sql; do
  docker compose exec -T db psql -v ON_ERROR_STOP=1 -U tokentracker -d tokentracker < "$f"
done
```

### Integration test

The normal Python test suite needs no database. Run `node --test tests/test_frontend_session.cjs` for the account-switching UI regression. CI runs both suites and supplies Postgres for the integration test. To exercise real auth, ingestion, retries, pricing, filters, provider isolation, and token rotation, use a **local test database**:

```bash
TT_TEST_DATABASE_URL=postgresql://tokentracker:dev@127.0.0.1:5432/tokentracker uv run pytest tests/test_codex_integration.py -q
```

The test creates and removes its own unique schema; existing application tables are untouched. A different mapped Postgres port can be used.

## Deploying

This app runs on a single **Oracle Cloud VM** with Postgres on-box. See **[docs/deploy-vm.md](docs/deploy-vm.md)** for the one-time setup; after that, every push to `main` auto-deploys via `.github/workflows/deploy.yml`.

## Production deploy checklist

1. **Set `AUTH_SECRET`** to 32+ bytes of random data. Rotating it invalidates every session and every ingest token — plan accordingly.
   ```bash
   AUTH_SECRET=$(openssl rand -hex 32)
   ```
2. **Set `COOKIE_SECURE=1`** — only valid if you terminate TLS in front of the app. Also enables HSTS and hides `/docs`, `/redoc`, `/openapi.json`.
3. **Set `TRUST_PROXY=1`** if (and only if) requests reach the app through a proxy you control (Cloudflare, Fly.io, nginx). Enables `X-Forwarded-For`-based rate-limit keys. **Do not enable without a trusted proxy** — any client can spoof the header.
4. Terminate TLS (Caddy / Cloudflare / Fly.io / nginx — whichever).
5. Change the Postgres password in `docker-compose.yml` (default is `dev`).
6. The Dockerfile CMD runs without `--reload`; `docker-compose.yml` adds `--reload` only for dev. Deploy using the image directly (e.g., `docker run tokentracker-app`), not via `docker compose up`, so dev-only overrides don't leak into prod.
7. **If a reverse proxy sits in front of the app, disable response buffering for `/stats/claude/stream`.** Server-Sent Events must flush each event immediately. The app already sends `X-Accel-Buffering: no` (honored by nginx); for others, ensure proxy buffering / gzip is off on that route. The single-worker uvicorn default fits a 1-vCPU box — one process keeps one shared snapshot and one refresh loop. The live-stats refresh can be tuned via env:
   ```
   STATS_REFRESH_SECONDS         = 2     # headline totals cadence
   STATS_LEADERBOARD_EVERY       = 3     # recompute leaderboards every Nth tick (~6s)
   STATS_STREAM_MAX_CLIENTS      = 256   # hard cap on concurrent SSE connections
   STATS_STREAM_HEARTBEAT_SECONDS = 15   # keep-alive comment interval
   ```
   On a small box the snapshot is computed once per interval regardless of viewer count, and only while at least one client is connected — an unwatched instance issues zero stats queries.

## Security properties

- Passwords hashed with Argon2id (default parameters, ~100ms per verify).
- Ingest tokens and session cookies hashed with HMAC-SHA256(pepper, token) — a DB dump alone cannot be used to authenticate without also capturing `AUTH_SECRET`.
- Session cookies: HttpOnly, SameSite=Strict, 30-day TTL, server-side session rows so logout is a real revocation.
- Timing: login path always runs Argon2 verify (real or dummy) so a missing user and a wrong password have the same latency.
- Rate limits (DB-backed, effective across workers): `/login` 10/min per IP **and** 10/5min per username (defeats credential spraying from rotating IPs); `/register` 3/5min per IP; `/v1/logs` + `/v1/metrics` 600/min per authed user (caps row-spam by a leaked ingest token).
- Request-body cap: 5 MB on every route via `Content-Length`; chunked `Transfer-Encoding` rejected outright (411) since it bypasses the cap.
- Registration error responses are uniform across any unique-constraint collision (today: username only) so future constraints can't accidentally start leaking.
- Container: runs as non-root user `app` (UID 1001).
- Response headers: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, CSP with `default-src 'self'` and `frame-ancestors 'none'`, and HSTS in prod.
- `/docs`, `/redoc`, `/openapi.json` disabled in prod (gated on `COOKIE_SECURE=1`).
- Dependency footprint: FastAPI + uvicorn + psycopg + opentelemetry-proto + argon2-cffi — roughly 25 transitive packages.

## Known limitations (for now)

- Email is not verified at registration — a typo means you can't self-serve a reset. The CLI fallback (`scripts/reset_password.py`) handles that case.
- No audit log of who changed what.
- Per-username login bucket can be used to lock a victim out of `/login` for the 5-minute window by spamming their username. Acceptable trade against credential spraying given there's no MFA; revisit if MFA lands.
- CSP allows `'unsafe-inline'` for scripts and styles since the HTML has inline blocks; migrate to nonced external files for a tighter policy.

## Tests

```bash
docker compose build app           # one-time; picks up the dev dep group
docker compose run --rm --no-deps app pytest tests/ -q
```

Tests stub the DB at the `pool` interface (no Postgres needed) and exercise the security-sensitive routes end-to-end through ASGI.

## Schema

```
users            id, username UNIQUE, email (UNIQUE case-insensitive, nullable), password_hash, ingest_token_hash UNIQUE, created_at
sessions         id, user_id, token_hash UNIQUE, created_at, last_seen_at, expires_at
password_resets  id, user_id, token_hash UNIQUE, expires_at, used_at, created_at
api_requests     id, user_id, billed_tokens (output + cache_creation), cost_usd, request_id, occurred_at, UNIQUE(user_id, request_id)
```

## Layout

```
src/tokentracker/
  main.py          FastAPI app + lifespan + SecurityHeaders middleware
  db.py            connection pool
  security.py      Argon2, HMAC, cookies, auth dependencies
  mailer.py        stdlib SMTP sender; falls back to stderr when unconfigured
  ratelimit.py     in-memory per-IP token bucket
  otlp.py          protobuf parser; keeps only api_request events
  routes.py        auth routes, /v1/logs, /v1/metrics, /usage/me, /stats/*, /password-reset/*
  static/index.html  SPA frontend
sql/001_init.sql
sql/002_password_reset.sql
scripts/post_sample_event.py
scripts/reset_password.py   one-shot CLI fallback for locked-out users
```
