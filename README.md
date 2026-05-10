# tokentracker

OTLP ingest server + public dashboard for Claude Code usage data.

## Auth model

A user is identified by a **username** (2-32 chars, `[A-Za-z0-9_-]`) which doubles as login id and public handle on the leaderboard. No email — there's no verification flow or password reset, so storing one would just be a worse identifier.

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

Register in the UI → copy the ingest token → paste it into `~/.claude/settings.json`:

```json
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

Restart Claude Code. Events flow within ~5s.

## Endpoints

**Public (no auth):**
- `GET /` — the SPA
- `GET /stats/totals` — aggregate spend + counts
- `GET /stats/leaderboard?limit=100` — ranked by cost, handles only
- `GET /healthz`

**Web session cookie (HttpOnly):**
- `POST /register` — username + password; sets cookie, returns ingest token
- `POST /login` — username + password; sets cookie
- `POST /logout` — clears cookie + deletes the session row
- `GET /me` — current user
- `GET /usage/me` — per-user totals
- `PATCH /me/username` — rename
- `POST /me/ingest-token/rotate` — invalidates the old ingest token, returns a new one

**Ingest Bearer token (Claude Code only):**
- `POST /v1/logs` — OTLP/protobuf logs; only `claude_code.api_request` records are stored
- `POST /v1/metrics` — stub that accepts and discards

## Deploying to Vercel

Vercel is supported via the `api/index.py` entry point and `vercel.json` at the repo root.

**One-time setup (Neon as the Postgres host):**
1. Create a Neon project at console.neon.tech → copy the **pooled** connection string (hostname contains `-pooler`, e.g. `ep-xxx-pooler.us-east-1.aws.neon.tech`). Do not use the direct endpoint — serverless fan-out will blow through connection limits.
2. In Neon's SQL Editor, paste and run the contents of `sql/001_init.sql`.
3. In the Vercel project dashboard → Settings → Environment Variables, set:
   ```
   DATABASE_URL         = <your Neon pooled connection string with ?sslmode=require>
   AUTH_SECRET          = <openssl rand -hex 32>
   COOKIE_SECURE        = 1
   TRUST_PROXY          = 1
   DB_POOL_MAX          = 1
   DB_DISABLE_PREPARE   = 1
   ```
   `DB_DISABLE_PREPARE=1` is **mandatory** on Neon's pooled endpoint — it uses pgbouncer in transaction mode, which breaks psycopg3's default prepared-statement caching.
4. **Enable "Deployment Protection"** on preview deployments (Vercel → Project → Settings → Deployment Protection → Vercel Authentication). Previews shouldn't be publicly accessible with a shared `AUTH_SECRET`.
5. Deploy: `vercel deploy --prod` or push to main.

Supabase works too — swap in its `*-pooler.supabase.com:6543` URL and keep `DB_DISABLE_PREPARE=1` (same pgbouncer constraint).

**Caveats of running on Vercel:**
- The OTLP ingest endpoint incurs one serverless invocation per Claude Code export (every ~5s per active session). Watch your invocation budget; for heavy use a VPS is cheaper.
- `lifespan` runs per cold container — first request after scale-up pays ~1s cold start.
- `rate_limits` table grows a bit; opportunistic prune happens inline, no cron needed.

## Production deploy checklist (generic / non-Vercel)

1. **Set `AUTH_SECRET`** to 32+ bytes of random data. Rotating it invalidates every session and every ingest token — plan accordingly.
   ```bash
   AUTH_SECRET=$(openssl rand -hex 32)
   ```
2. **Set `COOKIE_SECURE=1`** — only valid if you terminate TLS in front of the app. Also enables HSTS and hides `/docs`, `/redoc`, `/openapi.json`.
3. **Set `TRUST_PROXY=1`** if (and only if) requests reach the app through a proxy you control (Cloudflare, Fly.io, nginx). Enables `X-Forwarded-For`-based rate-limit keys. **Do not enable without a trusted proxy** — any client can spoof the header.
4. Terminate TLS (Caddy / Cloudflare / Fly.io / nginx — whichever).
5. Change the Postgres password in `docker-compose.yml` (default is `dev`).
6. The Dockerfile CMD runs without `--reload`; `docker-compose.yml` adds `--reload` only for dev. Deploy using the image directly (e.g., `docker run tokentracker-app`), not via `docker compose up`, so dev-only overrides don't leak into prod.

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

- No password reset / account recovery (no email on file by design).
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
users         id, username UNIQUE, password_hash, ingest_token_hash UNIQUE, created_at
sessions      id, user_id, token_hash UNIQUE, created_at, last_seen_at, expires_at
api_requests  id, user_id, billed_tokens (output + cache_creation), cost_usd, request_id, occurred_at, UNIQUE(user_id, request_id)
```

## Layout

```
src/tokentracker/
  main.py          FastAPI app + lifespan + SecurityHeaders middleware
  db.py            connection pool
  security.py      Argon2, HMAC, cookies, auth dependencies
  ratelimit.py     in-memory per-IP token bucket
  otlp.py          protobuf parser; keeps only api_request events
  routes.py        auth routes, /v1/logs, /v1/metrics, /usage/me, /stats/*
  static/index.html  SPA frontend
sql/001_init.sql
scripts/post_sample_event.py
```
