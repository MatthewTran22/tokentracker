import math
from datetime import datetime, timezone
from typing import Any, Iterable

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

# api_requests.billed_tokens is a 32-bit INTEGER (max 2_147_483_647). Clamp
# below that with headroom so a buggy value doesn't blow up the whole
# executemany batch with numeric_value_out_of_range.
_BILLED_TOKENS_MAX = 2_000_000_000


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
    # NUMERIC rejects NaN/Infinity; negative cost would let a user drag their
    # own leaderboard rank below zero.
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

    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)

        for sl in rl.scope_logs:
            for rec in sl.log_records:
                attrs = {**resource_attrs, **_attrs_to_dict(rec.attributes)}
                if attrs.get("event.name") != "api_request":
                    continue

                # request_id is the dedup key via UNIQUE (user_id, request_id);
                # drop events that lack one since multiple NULLs would defeat
                # the constraint.
                request_id = _as_str(attrs.get("request_id"))
                if request_id is None:
                    continue

                billed_tokens = _as_int(attrs.get("output_tokens")) + _as_int(
                    attrs.get("cache_creation_tokens")
                )
                cost_usd = _as_float(attrs.get("cost_usd"))
                model = _as_str(attrs.get("model"))

                occurred_at = _ns_to_datetime(rec.time_unix_nano) or _ns_to_datetime(
                    rec.observed_time_unix_nano
                )
                yield {
                    "billed_tokens": billed_tokens,
                    "cost_usd": cost_usd,
                    "request_id": request_id,
                    "model": model,
                    "occurred_at": occurred_at,
                }
