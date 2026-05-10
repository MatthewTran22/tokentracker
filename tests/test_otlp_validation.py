"""Anti-spoof checks in the OTLP parser.

Real Claude Code emits a narrow event shape; the parser drops anything that
doesn't match. None of these checks defeat a determined forger (the user
controls the wire), but together they reject every casual spoof and the
"just inflate cost_usd" attempt.

The canonical happy-path payload mirrors what scripts/post_sample_event.py
sends — same attribute set, same value shapes.
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


def _resource(service_name: str | None = "claude-code"):
    """Build a Resource matching real Claude Code's resource attributes."""
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    attrs = []
    if service_name is not None:
        attrs.append(_kv("service.name", service_name))
        attrs.append(_kv("service.version", "1.0.0-test"))
    return Resource(attributes=attrs)


def _payload(*, resource_service_name: str | None = "claude-code", **overrides) -> bytes:
    """Build a single-event OTLP payload with canonical real-Claude-Code shape.

    Pass overrides to mutate / drop individual log-record attributes. None removes
    the key. Pass resource_service_name=None or another string to override the
    Resource-level service.name attribute (defaults to the real CC value).
    """
    now_ns = time.time_ns()
    base = {
        "event.name": "api_request",
        "session.id": "sess-abc-123",
        "organization.id": "org-test",
        "model": "claude-sonnet-4-6",
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
            resource=_resource(resource_service_name),
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


def test_drops_when_service_name_missing():
    """Spoof scripts that don't set Resource attributes get rejected wholesale."""
    assert list(parse_api_request_events(_payload(resource_service_name=None))) == []


def test_drops_when_service_name_wrong():
    """Generic OTLP clients (Datadog Agent, Vector, etc.) advertising a
    different service.name are not Claude Code and get rejected."""
    assert list(parse_api_request_events(_payload(resource_service_name="datadog-agent"))) == []
    assert list(parse_api_request_events(_payload(resource_service_name="my-app"))) == []


def test_drops_when_session_id_missing():
    assert list(parse_api_request_events(_payload(**{"session.id": None}))) == []


def test_drops_when_organization_id_missing():
    assert list(parse_api_request_events(_payload(**{"organization.id": None}))) == []


def test_drops_when_request_id_wrong_format():
    # The spoof script used `spoof_<ns>` instead of `req_*`.
    assert list(parse_api_request_events(_payload(request_id="spoof_1234567890"))) == []


def test_drops_when_request_id_too_short():
    assert list(parse_api_request_events(_payload(request_id="req_xy"))) == []


def test_drops_when_model_is_garbage():
    assert list(
        parse_api_request_events(_payload(model="claude-opus-bezos-edition"))
    ) == []


def test_drops_when_model_lacks_claude_prefix():
    assert list(parse_api_request_events(_payload(model="gpt-4-turbo"))) == []


def test_accepts_known_model_families():
    for m in ("claude-sonnet-4-6", "claude-opus-4-1", "claude-haiku-3-5"):
        events = list(parse_api_request_events(_payload(model=m)))
        assert len(events) == 1, f"expected {m!r} to pass"


def test_drops_when_billed_tokens_above_cap():
    # 1M output + 600K cache_creation = 1.6M > 1.5M cap
    payload = _payload(output_tokens=1_000_000, cache_creation_tokens=600_000)
    assert list(parse_api_request_events(payload)) == []


def test_drops_when_cost_above_cap():
    # Spoofs claiming hundreds of dollars per event must be rejected;
    # max real event today is ~$17 (Opus 4.7 1M context + 64K output + 1h cache).
    assert list(parse_api_request_events(_payload(cost_usd=999_999.99))) == []
    # Even a "balanced" claim above $25 is rejected.
    payload = _payload(
        input_tokens=1_000_000, output_tokens=64_000, cache_creation_tokens=1_000_000,
        model="claude-opus-4-7",
        cost_usd=30.0,  # > $25 cap
    )
    assert list(parse_api_request_events(payload)) == []


def test_accepts_realistic_opus_4_7_max():
    """Real Opus 4.7 with 1M context, 64K output, 1h cache writes ≈ $17.
    Must pass — this is what a heavy 12-hour-vibe-coder turn looks like."""
    payload = _payload(
        input_tokens=1_000_000,         # $5/Mtok = $5.00
        output_tokens=64_000,           # $25/Mtok = $1.60
        cache_creation_tokens=1_000_000, # $10/Mtok (1h) = $10.00 actual; $6.25 expected (5m)
        cache_read_tokens=0,
        model="claude-opus-4-7",
        cost_usd=16.60,                 # ratio vs. 5m expected ≈ 1.5x, fits 2x tolerance
    )
    events = list(parse_api_request_events(payload))
    assert len(events) == 1
    assert events[0]["billed_tokens"] == 1_064_000


def test_accepts_realistic_high_value_event():
    """A realistic large turn (200K cache_creation, ~$1.60 cost) must still pass —
    the caps shouldn't false-positive on legitimate heavy usage. Cost is
    derived from sonnet rates: 200K input + 16K output + 200K cache_creation."""
    payload = _payload(
        input_tokens=200_000,        # $3/Mtok = $0.60
        output_tokens=16_000,        # $15/Mtok = $0.24
        cache_creation_tokens=200_000,  # $3.75/Mtok = $0.75
        cache_read_tokens=0,
        cost_usd=1.60,               # ≈ expected $1.59
    )
    events = list(parse_api_request_events(payload))
    assert len(events) == 1
    assert events[0]["billed_tokens"] == 216_000


def test_drops_event_with_wrong_event_name():
    # Defensive: only api_request events are stored. Other claude_code.* events
    # carry prompt/tool content and must never be persisted.
    assert list(parse_api_request_events(_payload(**{"event.name": "tool_result"}))) == []


def _payload_n_records(n: int) -> bytes:
    """Build a payload containing N api_request records, all canonical-shape."""
    now_ns = time.time_ns()
    records = []
    for i in range(n):
        attrs = [
            _kv("event.name", "api_request"),
            _kv("session.id", f"sess-{i}"),
            _kv("organization.id", "org-test"),
            _kv("model", "claude-sonnet-4-6"),
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
            resource=_resource("claude-code"),
            scope_logs=[ScopeLogs(log_records=records)],
        )]
    )
    return req.SerializeToString()


def test_batch_under_cap_passes():
    # 100 records is the cap, ≤ 100 must pass.
    events = list(parse_api_request_events(_payload_n_records(100)))
    assert len(events) == 100


def test_batch_over_cap_drops_entire_payload():
    # 101 records → whole payload dropped (not truncated).
    assert list(parse_api_request_events(_payload_n_records(101))) == []


def test_batch_far_over_cap_drops_entire_payload():
    # The bypass attempt (1500 events in one body) must yield nothing.
    assert list(parse_api_request_events(_payload_n_records(1500))) == []


# Cost-vs-tokens consistency check was removed — see otlp.py for rationale.
# Per-event caps + batch cap + shape checks are pricing-agnostic and don't
# need to track Anthropic's rate sheet.
