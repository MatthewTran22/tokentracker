"""Codex OTLP parser.

Codex CLI emits OpenTelemetry log records via `SessionTelemetry` in the
openai/codex repo. We accept and store exactly one event type:

    event.name = "codex.sse_event"  AND  event.kind = "response.completed"

This event carries the per-response token counts. All other Codex events
(codex.api_request, codex.user_prompt, codex.tool_result, codex.sse_event
with non-completed kinds, ...) carry HTTP/auth metadata or prompt/tool
content and must never be persisted — the event filter is both a usefulness
gate and a content-leak guard.

Model and conversation.id come from session-level attributes auto-attached
to every log record via Codex's `log_event!` macro (events/shared.rs).
"""
from datetime import datetime, timezone
from typing import Any, Iterable

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

# Token columns are INTEGER (32-bit) in Postgres. Clamp below 2^31 with
# headroom so a buggy value can't blow up the whole executemany batch.
_TOKEN_MAX = 2_000_000_000


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
    if n > _TOKEN_MAX:
        return _TOKEN_MAX
    return n


def _as_str(x: Any) -> str | None:
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def parse_codex_response_events(raw: bytes) -> Iterable[dict[str, Any]]:
    """Yield one dict per codex.sse_event response.completed log record."""
    req = ExportLogsServiceRequest()
    req.ParseFromString(raw)

    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)
        for sl in rl.scope_logs:
            for rec in sl.log_records:
                attrs = {**resource_attrs, **_attrs_to_dict(rec.attributes)}
                if attrs.get("event.name") != "codex.sse_event":
                    continue
                if attrs.get("event.kind") != "response.completed":
                    continue

                # Dedup key: prefer trace_id:span_id from the envelope (both
                # are bytes — empty when no trace context was active).
                trace_id_hex = rec.trace_id.hex() if rec.trace_id else ""
                span_id_hex = rec.span_id.hex() if rec.span_id else ""
                if trace_id_hex and span_id_hex:
                    dedup_key = f"{trace_id_hex}:{span_id_hex}"
                else:
                    convo_id = _as_str(attrs.get("conversation.id"))
                    nanos = rec.time_unix_nano
                    if not convo_id or not nanos:
                        # No usable dedup material — drop rather than risk
                        # NULL-key insert collisions.
                        continue
                    dedup_key = f"{convo_id}:{nanos}"

                yield {
                    "model": _as_str(attrs.get("model")),
                    "input_tokens": _as_int(attrs.get("input_token_count")),
                    "output_tokens": _as_int(attrs.get("output_token_count")),
                    "cached_input_tokens": _as_int(attrs.get("cached_token_count")),
                    "reasoning_output_tokens": _as_int(attrs.get("reasoning_token_count")),
                    "tool_tokens": _as_int(attrs.get("tool_token_count")),
                    "dedup_key": dedup_key,
                    "occurred_at": (
                        _ns_to_datetime(rec.time_unix_nano)
                        or _ns_to_datetime(rec.observed_time_unix_nano)
                    ),
                }
