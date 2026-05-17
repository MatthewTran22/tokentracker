import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

logger = logging.getLogger("tokentracker.otlp")

# api_requests.billed_tokens is a 32-bit INTEGER (max 2_147_483_647). Clamp
# below that with headroom so a spoofed or buggy value doesn't blow up the
# whole executemany batch with numeric_value_out_of_range.
_BILLED_TOKENS_MAX = 2_000_000_000

# Anti-spoof checks. Real Claude Code OTLP events come from a narrow shape
# the server-side telemetry emits. None of these stop a determined forger
# (the user controls the wire), but together they reject every casual spoof
# and any "I just inflated the numbers" attempt.
#
#   service.name   Claude Code's OTLP SDK always sets this to "claude-code"
#                  as a resource attribute (per docs.claude.com/en/monitoring).
#                  This endpoint is Claude-Code-specific by design — we are
#                  not a generic OTLP collector — so we can refuse anything
#                  that doesn't identify itself as CC.
#   request_id     Anthropic's API stamps requests with `req_<base32>`.
#   model          Real models always look like `claude-{family}-<digit>...`.
#                  Permissive on the version suffix, strict on the family
#                  + digit so junk like `claude-opus-bezos-edition` fails.
#   per-event caps Anchored to Anthropic's published physical per-request max
#                  (Opus 4.7, Sonnet 4.6, Haiku 4.5 — checked May 2026):
#                  - Opus 4.7 with 1M context, 64K output, 1h cache writes:
#                      1M × $5 (input) + 64K × $25 (output) + 1M × $10 (1h cache)
#                      = $5.00 + $1.60 + $10.00 ≈ $16.60 max real per request
#                  - Sonnet 4.6 with same shape: ~$9.96 max
#                  $25 cap = ~50% margin. 1.5M token cap covers
#                  1M cache_creation + 64K output with margin. Bump if
#                  Anthropic ships a model with bigger context or output.
_EXPECTED_SERVICE_NAME = "claude-code"
_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_\-]{8,}$")
_MODEL_RE = re.compile(r"^claude-(sonnet|opus|haiku)-\d", re.IGNORECASE)
_PER_EVENT_TOKEN_CAP = 1_500_000
_PER_EVENT_COST_CAP = 25.0

# NOTE: a cost-vs-tokens consistency check used to live here. Removed
# because (1) Claude Code already computes cost_usd from tokens × its own
# rate table, so any check we did would just re-verify Claude Code's math
# against ours; (2) hardcoding Anthropic's rates is a maintenance debt
# that drifts a few times a year; (3) a balanced spoofer pumps both
# tokens and cost together, defeating the check anyway. The per-event
# caps + shape checks are pricing-agnostic and do the work.


def _any_to_python(v: AnyValue) -> Any:
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_any_to_python(i) for i in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any_to_python(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, which)


def _attrs_to_dict(kvs) -> dict[str, Any]:
    return {kv.key: _any_to_python(kv.value) for kv in kvs}


def _ns_to_datetime(ns: int) -> datetime | None:
    if not ns:
        return None
    secs, rem = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(secs, tz=timezone.utc).replace(microsecond=rem // 1000)


def _as_int(x: Any) -> int:
    if x is None:
        return 0
    try:
        n = int(x)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        return 0
    if n > _BILLED_TOKENS_MAX:
        return _BILLED_TOKENS_MAX
    return n


def _as_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    # NUMERIC rejects NaN/Infinity; negative cost is nonsense and would just
    # let a user drag their own leaderboard rank below zero.
    if not math.isfinite(f) or f < 0:
        return None
    return f


def _as_str(x: Any) -> str | None:
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def parse_api_request_events(raw: bytes) -> Iterable[dict[str, Any]]:
    """Yield one dict per claude_code.api_request log record in the OTLP payload.

    Only event.name == "api_request" records are yielded; all other events are
    silently ignored. The api_request event by design carries no prompt or
    tool content, so this filter also acts as a defensive content guard.
    """
    req = ExportLogsServiceRequest()
    req.ParseFromString(raw)

    drops = {
        "service_name": 0,
        "event_name": 0,
        "session_id": 0,
        "organization_id": 0,
        "request_id": 0,
        "model": 0,
        "token_cap": 0,
        "cost_cap": 0,
    }
    sample_model = None
    kept = 0
    total = 0

    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)
        rl_record_count = sum(len(sl.log_records) for sl in rl.scope_logs)
        # Hard requirement: this ResourceLogs scope must self-identify as
        # Claude Code. Generic OTLP clients (Datadog Agent, Vector, custom
        # scripts) won't set this; spoof scripts that built ResourceLogs
        # without resource attrs definitely won't.
        if _as_str(resource_attrs.get("service.name")) != _EXPECTED_SERVICE_NAME:
            drops["service_name"] += rl_record_count
            total += rl_record_count
            continue

        for sl in rl.scope_logs:
            for rec in sl.log_records:
                total += 1
                attrs = {**resource_attrs, **_attrs_to_dict(rec.attributes)}
                if attrs.get("event.name") != "api_request":
                    drops["event_name"] += 1
                    continue

                # ---- shape checks (cheap, catch lazy spoofs) ----
                # Real events always include these. A spoofer can fill them
                # in, but most don't bother.
                if not _as_str(attrs.get("session.id")):
                    drops["session_id"] += 1
                    continue
                if not _as_str(attrs.get("organization.id")):
                    drops["organization_id"] += 1
                    continue
                request_id = _as_str(attrs.get("request_id"))
                if request_id is None or not _REQUEST_ID_RE.match(request_id):
                    drops["request_id"] += 1
                    continue
                model = _as_str(attrs.get("model"))
                if model is None or not _MODEL_RE.match(model):
                    drops["model"] += 1
                    if sample_model is None:
                        sample_model = model
                    continue

                # ---- value checks (catch "just inflate the numbers") ----
                billed_tokens = _as_int(attrs.get("output_tokens")) + _as_int(
                    attrs.get("cache_creation_tokens")
                )
                cost_usd = _as_float(attrs.get("cost_usd"))
                if billed_tokens > _PER_EVENT_TOKEN_CAP:
                    drops["token_cap"] += 1
                    continue
                if cost_usd is not None and cost_usd > _PER_EVENT_COST_CAP:
                    drops["cost_cap"] += 1
                    continue

                occurred_at = _ns_to_datetime(rec.time_unix_nano) or _ns_to_datetime(
                    rec.observed_time_unix_nano
                )
                kept += 1
                yield {
                    "billed_tokens": billed_tokens,
                    "cost_usd": cost_usd,
                    "request_id": request_id,
                    "occurred_at": occurred_at,
                }

    if total:
        nonzero = {k: v for k, v in drops.items() if v}
        logger.info(
            "otlp_parse total=%d kept=%d drops=%s sample_dropped_model=%r",
            total, kept, nonzero or "{}", sample_model,
        )
