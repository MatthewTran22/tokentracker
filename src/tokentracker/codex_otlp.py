"""Parse only completed Codex usage records, never prompt/tool content.

Telemetry reference: https://learn.chatgpt.com/docs/config-file/config-advanced
WebSocket completion is accepted only with explicit input/output counts; the
public docs do not guarantee those counts on every WebSocket event/version.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Iterable

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

_TOKEN_MAX = 2_000_000_000
_FIELDS = frozenset({
    'event.name', 'event.kind', 'model', 'conversation.id', 'response.id', 'response_id',
    'input_token_count', 'output_token_count', 'cached_token_count',
    'reasoning_token_count', 'tool_token_count', 'cache_write_token_count',
})
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z')


def _attrs_to_dict(kvs) -> dict[str, Any]:
    # Do not even materialize bodies, nested attributes or unrelated content.
    result = {}
    for kv in kvs:
        kind = kv.value.WhichOneof('value')
        if kv.key in _FIELDS and kind in ('string_value', 'int_value', 'double_value', 'bool_value'):
            result[kv.key] = getattr(kv.value, kind)
    return result


def _ns_to_datetime(ns: int) -> datetime | None:
    if not ns:
        return None
    try:
        secs, rem = divmod(ns, 1_000_000_000)
        result = datetime.fromtimestamp(secs, tz=timezone.utc).replace(microsecond=rem // 1000)
        # Allow clock skew, but do not store corrupt timestamps centuries ahead.
        if result > datetime.now(timezone.utc) + timedelta(days=1):
            return None
        return result
    except (ValueError, OverflowError, OSError):
        return None


def _count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError('boolean token count')
    if isinstance(value, str):
        if not re.fullmatch(r'[0-9]{1,10}', value):
            raise ValueError('invalid token count')
        value = int(value)
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise ValueError('invalid token count')
    if not 0 <= value <= _TOKEN_MAX:
        raise ValueError('token count out of range')
    return int(value)


def _identifier(value: Any) -> str | None:
    if isinstance(value, str) and value and len(value) <= 256 and '\x00' not in value:
        return value
    return None


def parse_codex_response_events(raw: bytes) -> Iterable[dict[str, Any]]:
    """Yield bounded usage metadata; malformed records are skipped independently.

    Stable response IDs deduplicate re-exports. Otherwise use conversation and
    the original nanosecond event timestamp, falling back to observed timestamp
    for clients such as 0.154.0 that emit no event timestamp. Collector rewrites
    of observed timestamps cannot be deduplicated without a stable response ID.
    Trace/span alone is not an event identity: it may cover several responses.
    """
    req = ExportLogsServiceRequest()
    req.ParseFromString(raw)
    for rl in req.resource_logs:
        resource_attrs = _attrs_to_dict(rl.resource.attributes)
        for sl in rl.scope_logs:
            for rec in sl.log_records:
                attrs = {**resource_attrs, **_attrs_to_dict(rec.attributes)}
                if attrs.get('event.name') not in ('codex.sse_event', 'codex.websocket_event'):
                    continue
                if attrs.get('event.kind') != 'response.completed':
                    continue
                try:
                    input_tokens = _count(attrs.get('input_token_count'))
                    output_tokens = _count(attrs.get('output_token_count'))
                    cached = _count(attrs.get('cached_token_count', 0))
                    writes = _count(attrs.get('cache_write_token_count', 0))
                    reasoning = _count(attrs.get('reasoning_token_count', 0))
                    tool = _count(attrs.get('tool_token_count', 0))
                except ValueError:
                    continue
                if cached + writes > input_tokens or reasoning > output_tokens:
                    continue
                occurred_at = _ns_to_datetime(rec.time_unix_nano) or _ns_to_datetime(rec.observed_time_unix_nano)
                if occurred_at is None:
                    continue
                response_id = _identifier(attrs.get('response.id')) or _identifier(attrs.get('response_id'))
                if response_id:
                    identity = ['response', response_id]
                elif (nanos := rec.time_unix_nano or rec.observed_time_unix_nano) and (convo := _identifier(attrs.get('conversation.id'))):
                    identity = ['conversation', convo, nanos]
                elif nanos and rec.trace_id and rec.span_id:
                    identity = ['trace', rec.trace_id.hex(), rec.span_id.hex(), nanos]
                else:
                    continue
                # Fixed-size keys also keep arbitrary identifier strings out of storage.
                dedup_key = 'codex:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                model = attrs.get('model')
                yield {
                    'model': model if isinstance(model, str) and _MODEL.fullmatch(model) else None,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'cached_input_tokens': cached,
                    'cache_write_tokens': writes,
                    'reasoning_output_tokens': reasoning,
                    'tool_tokens': tool,
                    'dedup_key': dedup_key,
                    'occurred_at': occurred_at,
                }
