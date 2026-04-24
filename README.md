# tokentracker

OTLP ingest server for Claude Code usage data. Receives `claude_code.api_request`
events via OpenTelemetry, stores them in Postgres, exposes a per-user usage query.

## Quick start

```bash
docker compose up -d
curl http://localhost:8000/healthz
```

The `app` container builds with `uv`; Postgres initializes from `sql/001_init.sql`
on first start. No host-side Python or Postgres install needed.

## Register a user (local dev)

```bash
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"email":"me@example.com"}'
# => {"user_id":1,"token":"<save this>"}
```

The plaintext token is returned once. Only its SHA-256 hash is stored.

## Point Claude Code at the server

Add to `~/.claude/settings.json` (user-level, keeps the secret out of any repo):

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:8000",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <your-token>"
  }
}
```

Events arrive on `/v1/logs`. Metrics currently hit `/v1/metrics` as a stub (accepted,
discarded) so Claude Code doesn't see errors.

## Sanity check

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/usage/me
```

## Synthetic event (no Claude Code needed)

```bash
docker compose exec app python /app/scripts/post_sample_event.py <token>
```

On Git Bash for Windows prefix with `MSYS_NO_PATHCONV=1` so the `/app/...` path is
passed through unmangled.

## Layout

```
src/tokentracker/
  main.py       FastAPI app + lifespan
  db.py         asyncpg pool
  security.py   token generation, hashing, Bearer auth dependency
  otlp.py       OTLP protobuf parser; keeps only api_request events
  routes.py     /register, /v1/logs, /v1/metrics, /usage/me, /healthz
sql/001_init.sql
scripts/post_sample_event.py
```

## Notes

- Only `event.name == "api_request"` records are ingested. Prompt content,
  tool inputs, and raw API bodies are never emitted by Claude Code on this
  event and would be ignored if present.
- Dedup key: `(user_id, request_id)`. Retried OTLP batches are safe.
- `user.email` arrives automatically for OAuth-authenticated (Pro/Max) users
  and is stored in `api_requests.reported_email` for reference.
