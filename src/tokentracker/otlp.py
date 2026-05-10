import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

# api_requests.billed_tokens is a 32-bit INTEGER (max 2_147_483_647). Clamp
# below that with headroom so a spoofed or buggy value doesn't blow up the
# whole executemany batch with numeric_value_out_of_range.
_BILLED_TOKENS_MAX = 2_000_000_000

# Anti-spoof checks. Real Claude Code OTLP events come from a narrow shape
# the server-side telemetry emits. None of these stop a determined forger
# (the user controls the wire), but together they reject every casual spoof
# and any "I just inflated the numbers" attempt.
#
#   request_id     Anthropic's API stamps requests with `req_<base32>`.
#   model          Real models always look like `claude-{family}-<digit>...`.
#                  Permissive on the version suffix, strict on the family
#                  + digit so junk like `claude-opus-bezos-edition` fails.
#   per-event caps Even the largest legitimate single Claude Code turn is
#                  far below these. ~200K input + ~16K output + ~200K
#                  cache_creation puts billed_tokens around 220K and
#                  cost around $8 at API list price. 2M tokens / $50 is
#                  generous headroom; anything beyond is forged.
_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_\-]{8,}$")
_MODEL_RE = re.compile(r"^claude-(sonnet|opus|haiku)-\d", re.IGNORECASE)
_PER_EVENT_TOKEN_CAP = 2_000_000
_PER_EVENT_COST_CAP = 50.0

# Real Claude Code OTLP exports carry 1-5 log records per HTTP request.
# A payload with dozens to thousands is a forger packing the body cap.
# Reject the entire batch (rather than truncating) so a spoofer can't
# probe to learn the cap.
_MAX_BATCH_RECORDS = 50

# Cost-vs-tokens consistency. Real Claude Code computes cost from token
# counts at Anthropic's published API rates, so cost is always within a
# small multiple of (tokens × rate). A spoofer who pumps cost without
# matching tokens fails this check. Tolerance is generous (5x) to absorb
# rate tier variations (cache discounts, batch API), price changes over
# time, and the fact that we approximate per-family rather than per-model.
#
# These rates are USD per million tokens, conservative on the high side
# of each token type. Heavy real users pass naturally; spoofers must now
# inflate tokens proportionally to cost.
_RATES_BY_FAMILY = {
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_creation": 3.75,  "cache_read": 0.30},
    "opus":   {"input": 15.0, "output": 75.0, "cache_creation": 18.75, "cache_read": 1.50},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_creation": 1.25,  "cache_read": 0.10},
}
_COST_TOLERANCE = 5.0          # claimed cost ≤ expected × this
_COST_FLOOR = 0.05             # absolute floor so tiny events don't FP


def _expected_cost(model: str, attrs: dict[str, Any]) -> float | None:
    """Approx cost from token counts at this model's family rate.
    Returns None if the family can't be classified (skip the check)."""
    m = model.lower()
    family = next((f for f in _RATES_BY_FAMILY if f in m), None)
    if family is None:
        return None
    r = _RATES_BY_FAMILY[family]
    return (
        _as_int(attrs.get("input_tokens"))          * r["input"]          / 1_000_000
        + _as_int(attrs.get("output_tokens"))       * r["output"]         / 1_000_000
        + _as_int(attrs.get("cache_creation_tokens")) * r["cache_creation"] / 1_000_000
        + _as_int(attrs.get("cache_read_tokens"))   * r["cache_read"]     / 1_000_000
    )


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

    # Batch size cap: real Claude Code never sends this many records in one
    # HTTP request. Drop the whole batch (not just the overflow) so probing
    # the cap is uninformative.
    total_records = sum(
        len(sl.log_records)
        for rl in req.resource_logs
        for sl in rl.scope_logs
    )
    if total_records > _MAX_BATCH_RECORDS:
        return

    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)
        for sl in rl.scope_logs:
            for rec in sl.log_records:
                attrs = {**resource_attrs, **_attrs_to_dict(rec.attributes)}
                if attrs.get("event.name") != "api_request":
                    continue

                # ---- shape checks (cheap, catch lazy spoofs) ----
                # Real events always include these. A spoofer can fill them
                # in, but most don't bother.
                if not _as_str(attrs.get("session.id")):
                    continue
                if not _as_str(attrs.get("organization.id")):
                    continue
                request_id = _as_str(attrs.get("request_id"))
                if request_id is None or not _REQUEST_ID_RE.match(request_id):
                    continue
                model = _as_str(attrs.get("model"))
                if model is None or not _MODEL_RE.match(model):
                    continue

                # ---- value checks (catch "just inflate the numbers") ----
                billed_tokens = _as_int(attrs.get("output_tokens")) + _as_int(
                    attrs.get("cache_creation_tokens")
                )
                cost_usd = _as_float(attrs.get("cost_usd"))
                if billed_tokens > _PER_EVENT_TOKEN_CAP:
                    continue
                if cost_usd is not None and cost_usd > _PER_EVENT_COST_CAP:
                    continue
                # Cost-vs-tokens consistency: claimed cost must be within
                # tolerance of what the token counts would produce at the
                # model's family rate. Real telemetry passes naturally;
                # forging a cost above the token-implied value fails here.
                if cost_usd is not None:
                    expected = _expected_cost(model, attrs)
                    if expected is not None and cost_usd > expected * _COST_TOLERANCE + _COST_FLOOR:
                        continue

                occurred_at = _ns_to_datetime(rec.time_unix_nano) or _ns_to_datetime(
                    rec.observed_time_unix_nano
                )
                yield {
                    "billed_tokens": billed_tokens,
                    "cost_usd": cost_usd,
                    "request_id": request_id,
                    "occurred_at": occurred_at,
                }
