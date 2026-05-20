"""OTLP parser tests.

The parser yields one dict per claude_code.api_request log record. It filters
to event.name == "api_request" (other events carry prompt/tool content and
must never be persisted) and requires a request_id (which is the dedup key
via UNIQUE (user_id, request_id)).
"""
import time

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs

from tokentracker.otlp import parse_api_request_events


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


def _resource():
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    return Resource(attributes=[_kv("service.name", "claude-code")])


def _payload(**overrides) -> bytes:
    now_ns = time.time_ns()
    base = {
        "event.name": "api_request",
        "request_id": f"req_test_{now_ns}",
        "output_tokens": 567,
        "cache_creation_tokens": 120,
        "cost_usd": 0.0421,
    }
    base.update(overrides)
    attrs = [_kv(k, v) for k, v in base.items() if v is not None]
    rec = LogRecord(time_unix_nano=now_ns, observed_time_unix_nano=now_ns, attributes=attrs)
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(
            resource=_resource(),
            scope_logs=[ScopeLogs(log_records=[rec])],
        )]
    )
    return req.SerializeToString()


def test_canonical_event_passes():
    events = list(parse_api_request_events(_payload()))
    assert len(events) == 1
    e = events[0]
    assert e["billed_tokens"] == 567 + 120
    assert e["cost_usd"] == 0.0421
    assert e["request_id"].startswith("req_test_")


def test_drops_event_with_wrong_event_name():
    # Only api_request events are stored. Other claude_code.* events carry
    # prompt/tool content and must never be persisted.
    assert list(parse_api_request_events(_payload(**{"event.name": "tool_result"}))) == []


def test_drops_when_request_id_missing():
    # request_id is the dedup key (UNIQUE (user_id, request_id)); without it
    # multiple NULL request_ids would defeat the constraint.
    assert list(parse_api_request_events(_payload(request_id=None))) == []


def test_negative_cost_rejected():
    # Negative cost would let a user drag their own leaderboard rank below zero.
    events = list(parse_api_request_events(_payload(cost_usd=-1.0)))
    assert len(events) == 1
    assert events[0]["cost_usd"] is None


def _payload_n_records(n: int) -> bytes:
    now_ns = time.time_ns()
    records = []
    for i in range(n):
        attrs = [
            _kv("event.name", "api_request"),
            _kv("request_id", f"req_test_{now_ns}_{i}_xxxxxxxx"),
            _kv("output_tokens", 100),
            _kv("cache_creation_tokens", 50),
            _kv("cost_usd", 0.01),
        ]
        records.append(LogRecord(
            time_unix_nano=now_ns + i,
            observed_time_unix_nano=now_ns + i,
            attributes=attrs,
        ))
    req = ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(
            resource=_resource(),
            scope_logs=[ScopeLogs(log_records=records)],
        )]
    )
    return req.SerializeToString()


def test_large_batch_passes():
    # No batch cap — OTLP BatchLogProcessor can flush up to its max_export_batch_size
    # (512 by default) in one POST, and a reconnect-after-offline flush goes higher.
    events = list(parse_api_request_events(_payload_n_records(1500)))
    assert len(events) == 1500
