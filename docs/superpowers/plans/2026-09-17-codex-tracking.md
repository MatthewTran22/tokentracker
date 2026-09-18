# Codex Tracking Implementation Plan

> Execute using superpowers:subagent-driven-development with independent file ownership and integration review.

**Goal:** Add Codex usage tracking with shared accounts and separate provider views.
**Architecture:** Reuse existing Codex schema and OTLP parser. Add a provider-specific router and statistics module; retain existing Claude routes. Frontend adds dual totals and distinct Codex dashboard, leaderboard, and connection controls.
**Tech Stack:** FastAPI, psycopg, PostgreSQL, OTLP protobuf, plain HTML/CSS/JavaScript.
**Spec:** ../specs/2026-09-17-codex-tracking.md

## Global constraints
- Preserve all Claude endpoints and Fable-first ordering.
- Codex token authentication is independent from Claude.
- Token totals include cached input and reasoning only once.
- Unknown prices remain null and expose unpriced request counts.
- No credentials or conversation content in public responses.
- Use existing local checkout on a feature branch so the approved preview remains easy to run.

## Tasks
- [x] Parser/pricing: verify official event and pricing documentation; test deduplication, token accounting, malformed values, and unknown model handling; implement in codex_otlp.py and codex_pricing.py.
- [x] Backend: implement codex_routes.py and codex_stats.py; mount through existing router for test compatibility. Token rotation POST /me/codex-ingest-token/rotate returns ingest_token. Ingest POST /codex/v1/logs accepts protobuf with distinct auth. GET /stats/codex/totals returns total_users, total_requests, total_tokens, total_cost_usd, unpriced_requests, by_model. GET /stats/codex/leaderboard?model=<exact-id> returns model and rows with rank, username, cost_usd, total_tokens, requests, unpriced_requests. GET /usage/me/codex returns same usage totals scoped to session user. Model breakdown entries contain cost_usd, total_tokens, requests, unpriced_requests. Add auth, isolation, malformed payload, rate limit, and aggregate tests.
- [x] UI: dual provider homepage totals, Codex leaderboard and dashboard, model filters, connection config and copy controls. Poll Codex every 2 seconds while visible. Display API-equivalent estimate labels and unknown-pricing counts, loading/errors, escape model/user text. Preserve existing account flows and responsive beach design.
- [x] Integration: local Postgres schema verification, register/connect/ingest/duplicate/filter/rotate exercises, real Codex event capture when feasible, browser desktop/mobile checks, full pytest suite, diff review, deployment instructions.

## Verification and implementation decisions

- Verified installed Codex 0.154.0-alpha.6.2 with temporary per-invocation OTel export, without modifying global client configuration. Observed timestamps are essential: actual records omit event timestamps and trace/span IDs.
- Replayed its two completion records twice into the local app: 2 requests, 29,598 total tokens, $0.118472 standard API-equivalent estimate. No duplicate count.
- Added cache-write token storage through migration 005 after the real client exposed that field.
- Public Codex data uses a bounded two-second cache. Private data bypasses the cache and is cleared on account/view changes.
- Verified desktop side-by-side homepage, narrow-screen layout without page overflow, model filter, setup, login redirect, and personal Codex totals in the browser.
- Full Python suite, opt-in real Postgres integration, and the Node account-switch regression pass. CI now runs all three.
- Global Codex telemetry configuration and deployment remain unchanged; connect a personal account through the generated setup instructions.
