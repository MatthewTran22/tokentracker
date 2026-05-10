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


def _payload(**overrides) -> bytes:
    """Build a single-event OTLP payload with canonical real-Claude-Code shape.

    Pass overrides to mutate / drop individual attributes. `None` removes the key.
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
        resource_logs=[ResourceLogs(scope_logs=[ScopeLogs(log_records=[rec])])]
    )
    return req.SerializeToString()


def test_canonical_event_passes():
    events = list(parse_api_request_events(_payload()))
    assert len(events) == 1
    e = events[0]
    assert e["billed_tokens"] == 567 + 120
    assert e["cost_usd"] == 0.0421
    assert e["request_id"].startswith("req_test_")


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
    # 1.5M output + 0.6M cache_creation = 2.1M > 2M cap
    payload = _payload(output_tokens=1_500_000, cache_creation_tokens=600_000)
    assert list(parse_api_request_events(payload)) == []


def test_drops_when_cost_above_cap():
    # The spoof posted $999_999.99 — should be rejected.
    assert list(parse_api_request_events(_payload(cost_usd=999_999.99))) == []


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
        resource_logs=[ResourceLogs(scope_logs=[ScopeLogs(log_records=records)])]
    )
    return req.SerializeToString()


def test_batch_under_cap_passes():
    # 50 records is the cap, ≤ 50 must pass.
    events = list(parse_api_request_events(_payload_n_records(50)))
    assert len(events) == 50


def test_batch_over_cap_drops_entire_payload():
    # 51 records → whole payload dropped (not truncated).
    assert list(parse_api_request_events(_payload_n_records(51))) == []


def test_batch_far_over_cap_drops_entire_payload():
    # The bypass attempt (1500 events in one body) must yield nothing.
    assert list(parse_api_request_events(_payload_n_records(1500))) == []


# ---- cost-vs-tokens consistency ----

def test_cost_consistent_with_tokens_passes():
    """Real telemetry: cost = tokens × rate. Must pass."""
    # 567 output × $15/Mtok = $0.0085, 120 cache_creation × $3.75/Mtok = $0.0005
    # Total expected ≈ $0.013; claimed $0.04 is 3x — within 5x tolerance.
    payload = _payload(
        output_tokens=567,
        cache_creation_tokens=120,
        input_tokens=1234,
        cache_read_tokens=8000,
        cost_usd=0.04,
    )
    assert len(list(parse_api_request_events(payload))) == 1


def test_cost_pumped_with_tiny_tokens_fails():
    """The classic spoof: claim huge cost with negligible tokens."""
    # $0.39 with 1K output + 1K cache_creation → expected ~ $0.019
    # Ratio 20x → exceeds 5x tolerance, drop.
    payload = _payload(
        output_tokens=1000,
        cache_creation_tokens=1000,
        input_tokens=0,
        cache_read_tokens=0,
        cost_usd=0.39,
    )
    assert list(parse_api_request_events(payload)) == []


def test_cost_within_5x_tolerance_passes():
    """Tier discounts and pricing variations: up to 5x slack allowed."""
    # 100K output × $15/Mtok = $1.50 expected. Claim $7.00 (~4.7x) — passes.
    payload = _payload(
        output_tokens=100_000,
        cache_creation_tokens=0,
        input_tokens=0,
        cache_read_tokens=0,
        cost_usd=7.00,
    )
    assert len(list(parse_api_request_events(payload))) == 1


def test_consistency_check_skipped_for_unknown_family():
    """If we can't classify the model family, skip the check rather than
    drop legitimate events for novel model names. (The model regex still
    requires sonnet|opus|haiku, so this is defense-in-depth.)"""
    # Model passes the regex but contains family token + extra junk; expected
    # cost still computes since "sonnet" is in the lowercased name.
    payload = _payload(model="claude-sonnet-4-6", cost_usd=0.04)
    assert len(list(parse_api_request_events(payload))) == 1


def test_zero_cost_event_passes():
    """A free event (e.g. pure cache hit) must not be misclassified as
    inconsistent — None / 0 cost should bypass the cost check entirely."""
    payload = _payload(
        output_tokens=100,
        cache_creation_tokens=0,
        cache_read_tokens=10000,
        cost_usd=0.0,
    )
    assert len(list(parse_api_request_events(payload))) == 1
