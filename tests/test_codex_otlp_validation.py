"""Codex OTLP parser tests.

The parser yields one dict per codex.sse_event response.completed log record.
All other Codex events (codex.api_request, codex.user_prompt, codex.tool_result, ...)
are dropped — that filter is also the content guard.
"""
import time

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from tokentracker.codex_otlp import parse_codex_response_events


def _kv(key, value):
    av = AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    elif isinstance(value, float):
        av.double_value = value
    else:
        av.string_value = str(value)
    return KeyValue(key=key, value=av)


def _resource(service_name: str = "codex-cli") -> Resource:
    return Resource(attributes=[_kv("service.name", service_name)])


# 16-byte trace_id and 8-byte span_id used by happy-path tests.
TRACE_ID = bytes(range(16))
SPAN_ID = bytes(range(8))


def _payload(
    *,
    event_name: str = "codex.sse_event",
    event_kind: str = "response.completed",
    model: str = "gpt-5.1",
    conversation_id: str = "conv-abc",
    input_tokens: int = 1234,
    output_tokens: int = 567,
    cached_tokens: int | None = 100,
    reasoning_tokens: int | None = 50,
    tool_tokens: int = 10,
    trace_id: bytes = TRACE_ID,
    span_id: bytes = SPAN_ID,
    service_name: str = "codex-cli",
) -> bytes:
    now_ns = time.time_ns()
    attrs = [
        _kv("event.name", event_name),
        _kv("event.kind", event_kind),
        _kv("model", model),
        _kv("conversation.id", conversation_id),
        _kv("input_token_count", input_tokens),
        _kv("output_token_count", output_tokens),
        _kv("tool_token_count", tool_tokens),
    ]
    if cached_tokens is not None:
        attrs.append(_kv("cached_token_count", cached_tokens))
    if reasoning_tokens is not None:
        attrs.append(_kv("reasoning_token_count", reasoning_tokens))
    rec = LogRecord(
        time_unix_nano=now_ns,
        observed_time_unix_nano=now_ns,
        attributes=attrs,
        trace_id=trace_id,
        span_id=span_id,
    )
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(
            resource=_resource(service_name),
            scope_logs=[ScopeLogs(log_records=[rec])],
        )]
    )
    return req.SerializeToString()


def test_canonical_event_passes():
    events = list(parse_codex_response_events(_payload()))
    assert len(events) == 1
    e = events[0]
    assert e["model"] == "gpt-5.1"
    assert e["input_tokens"] == 1234
    assert e["output_tokens"] == 567
    assert e["cached_input_tokens"] == 100
    assert e["reasoning_output_tokens"] == 50
    assert e["tool_tokens"] == 10
    # 16 bytes -> 32 hex chars; 8 bytes -> 16 hex chars
    assert e["dedup_key"] == f"{TRACE_ID.hex()}:{SPAN_ID.hex()}"


def test_drops_wrong_event_name():
    assert list(parse_codex_response_events(_payload(event_name="codex.api_request"))) == []


def test_drops_wrong_event_kind():
    # codex.sse_event with a non-response.completed kind is a streaming chunk
    # event, not a usage record — must not be stored.
    assert list(parse_codex_response_events(_payload(event_kind="response.started"))) == []


def test_missing_model_yields_event_with_none_model():
    # The row still inserts; cost will be NULL via the pricing module.
    events = list(parse_codex_response_events(_payload(model="")))
    assert len(events) == 1
    assert events[0]["model"] is None


def test_optional_tokens_default_to_zero():
    events = list(parse_codex_response_events(
        _payload(cached_tokens=None, reasoning_tokens=None)
    ))
    assert len(events) == 1
    assert events[0]["cached_input_tokens"] == 0
    assert events[0]["reasoning_output_tokens"] == 0


def test_oversized_tokens_clamped():
    events = list(parse_codex_response_events(_payload(input_tokens=10**12)))
    assert events[0]["input_tokens"] == 2_000_000_000


def test_dedup_fallback_when_trace_context_missing():
    # Empty trace_id / span_id -> conversation_id + nanos fallback.
    raw = _payload(trace_id=b"", span_id=b"")
    events = list(parse_codex_response_events(raw))
    assert len(events) == 1
    key = events[0]["dedup_key"]
    assert key.startswith("conv-abc:")
    # nanos portion present
    assert key.split(":", 1)[1].isdigit()


def test_dropped_when_no_dedup_material():
    # No trace context AND no conversation.id -> dedup impossible -> drop.
    now_ns = time.time_ns()
    attrs = [
        _kv("event.name", "codex.sse_event"),
        _kv("event.kind", "response.completed"),
        _kv("model", "gpt-5.1"),
        _kv("input_token_count", 100),
        _kv("output_token_count", 100),
        _kv("tool_token_count", 0),
        # no conversation.id
    ]
    rec = LogRecord(
        time_unix_nano=now_ns,
        observed_time_unix_nano=now_ns,
        attributes=attrs,
        # empty trace/span ids
    )
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(
            resource=_resource(),
            scope_logs=[ScopeLogs(log_records=[rec])],
        )]
    )
    assert list(parse_codex_response_events(req.SerializeToString())) == []


def test_batch_passes_through():
    # Multiple records in one envelope -> all parsed, distinct dedup keys.
    now_ns = time.time_ns()
    records = []
    for i in range(50):
        records.append(LogRecord(
            time_unix_nano=now_ns + i,
            observed_time_unix_nano=now_ns + i,
            attributes=[
                _kv("event.name", "codex.sse_event"),
                _kv("event.kind", "response.completed"),
                _kv("model", "gpt-5.1"),
                _kv("conversation.id", f"conv-{i}"),
                _kv("input_token_count", 10),
                _kv("output_token_count", 10),
                _kv("tool_token_count", 0),
            ],
            trace_id=bytes([i]) * 16,
            span_id=bytes([i]) * 8,
        ))
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(
            resource=_resource(),
            scope_logs=[ScopeLogs(log_records=records)],
        )]
    )
    events = list(parse_codex_response_events(req.SerializeToString()))
    assert len(events) == 50
    assert len({e["dedup_key"] for e in events}) == 50
