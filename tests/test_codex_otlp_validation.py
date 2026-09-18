"""Codex OTLP parser tests.

The parser yields one dict per codex.sse_event response.completed log record.
All other Codex events (codex.api_request, codex.user_prompt, codex.tool_result, ...)
are dropped — that filter is also the content guard.
"""
import time

import pytest

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
    # Stable hash of response identity, not the shared trace/span.
    assert e["dedup_key"].startswith("codex:")


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


def test_oversized_tokens_rejected():
    events = list(parse_codex_response_events(_payload(input_tokens=10**12)))
    assert events == []


def test_dedup_fallback_when_trace_context_missing():
    # Empty trace_id / span_id -> conversation_id + nanos fallback.
    raw = _payload(trace_id=b"", span_id=b"")
    events = list(parse_codex_response_events(raw))
    assert len(events) == 1
    key = events[0]["dedup_key"]
    assert key.startswith("codex:")
    assert list(parse_codex_response_events(raw))[0]["dedup_key"] == key


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


def _edit(raw, *, attrs=None, timestamp=None, observed=None, remove=()):
    req = ExportLogsServiceRequest.FromString(raw)
    rec = req.resource_logs[0].scope_logs[0].log_records[0]
    kept = [kv for kv in rec.attributes if kv.key not in remove and kv.key not in (attrs or {})]
    del rec.attributes[:]
    rec.attributes.extend(kept)
    rec.attributes.extend(_kv(k, v) for k, v in (attrs or {}).items())
    if timestamp is not None:
        rec.time_unix_nano = timestamp
    if observed is not None:
        rec.observed_time_unix_nano = observed
    return req.SerializeToString()


def test_distinct_responses_sharing_trace_span_survive():
    raw = _payload()
    one = list(parse_codex_response_events(_edit(raw, timestamp=1_700_000_000_000_000_000)))[0]
    two = list(parse_codex_response_events(_edit(raw, timestamp=1_700_000_000_000_000_001)))[0]
    assert one['dedup_key'] != two['dedup_key']


def test_response_id_deduplicates_reexport_with_changed_timestamp():
    raw = _edit(_payload(), attrs={'response.id': 'resp_123'})
    one = list(parse_codex_response_events(raw))[0]
    two = list(parse_codex_response_events(_edit(raw, timestamp=1_700_000_000_000_000_000)))[0]
    assert one['dedup_key'] == two['dedup_key']


@pytest.mark.parametrize('value', [-1, 1.5, float('nan'), float('inf'), True, 'NaN', 'bad', 2_000_000_001])
def test_invalid_counts_drop_event(value):
    assert list(parse_codex_response_events(_payload(input_tokens=value))) == []


def test_inconsistent_subsets_drop_event():
    assert list(parse_codex_response_events(_payload(cached_tokens=1235))) == []
    assert list(parse_codex_response_events(_payload(reasoning_tokens=568))) == []


def test_websocket_completed_with_explicit_usage():
    assert len(list(parse_codex_response_events(_payload(event_name='codex.websocket_event')))) == 1


@pytest.mark.parametrize('event_name', ['codex.sse_event', 'codex.websocket_event'])
def test_completed_without_required_usage_is_not_counted(event_name):
    raw = _edit(_payload(event_name=event_name), remove=('input_token_count', 'output_token_count'))
    assert list(parse_codex_response_events(raw)) == []


def test_missing_event_timestamp_uses_verified_client_observed_time():
    raw = _edit(_payload(trace_id=b'', span_id=b'', input_tokens='18006', output_tokens='5', cached_tokens=0, reasoning_tokens=0), timestamp=0)
    events = list(parse_codex_response_events(raw))
    assert len(events) == 1
    assert events[0]['input_tokens'] == 18006
    assert events[0]['dedup_key'] == list(parse_codex_response_events(raw))[0]['dedup_key']


def test_missing_timestamp_with_response_id_uses_observed_time():
    raw = _edit(_payload(), timestamp=0, attrs={'response.id': 'resp_123'})
    assert list(parse_codex_response_events(raw))[0]['occurred_at'] is not None


def test_no_valid_timestamp_is_rejected():
    raw = _edit(_payload(), timestamp=0, observed=0, attrs={'response.id': 'resp_123'})
    assert list(parse_codex_response_events(raw)) == []


def test_only_usage_metadata_is_returned():
    raw = _edit(_payload(), attrs={'prompt': 'private prompt', 'tool.output': 'secret', 'response.id': 'resp_123'})
    req = ExportLogsServiceRequest.FromString(raw)
    req.resource_logs[0].scope_logs[0].log_records[0].body.string_value = 'private body'
    result = list(parse_codex_response_events(req.SerializeToString()))[0]
    assert set(result) == {'model', 'input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens', 'tool_tokens', 'dedup_key', 'occurred_at', 'cache_write_tokens'}
    assert 'private' not in str(result) and 'secret' not in str(result)


def test_model_string_is_bounded_metadata():
    assert list(parse_codex_response_events(_payload(model='private prompt with spaces')))[0]['model'] is None
    assert list(parse_codex_response_events(_payload(model='x'*1000)))[0]['model'] is None


def test_cache_write_tokens_are_preserved_and_default_zero():
    raw = _edit(_payload(), attrs={'cache_write_token_count': '300'})
    assert list(parse_codex_response_events(raw))[0]['cache_write_tokens'] == 300
    assert list(parse_codex_response_events(_payload()))[0]['cache_write_tokens'] == 0


@pytest.mark.parametrize('value', [float('inf'), -1, 2000])
def test_invalid_cache_write_tokens_rejected(value):
    raw = _edit(_payload(), attrs={'cache_write_token_count': value})
    assert list(parse_codex_response_events(raw)) == []


def test_cached_and_written_counts_cannot_together_exceed_input():
    raw = _edit(_payload(cached_tokens=1000), attrs={'cache_write_token_count': 300})
    assert list(parse_codex_response_events(raw)) == []


def test_implausible_future_timestamp_is_rejected():
    raw = _edit(_payload(), timestamp=2**64 - 1, observed=2**64 - 1)
    assert list(parse_codex_response_events(raw)) == []


def test_different_response_ids_in_same_span_are_distinct():
    raw = _payload()
    one = list(parse_codex_response_events(_edit(raw, attrs={'response.id': 'resp_1'})))[0]
    two = list(parse_codex_response_events(_edit(raw, attrs={'response.id': 'resp_2'})))[0]
    assert one['dedup_key'] != two['dedup_key']
