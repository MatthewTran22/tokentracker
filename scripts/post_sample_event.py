"""Post a synthetic claude_code.api_request OTLP log payload to the ingest server.

Usage:
    python scripts/post_sample_event.py <token>

Runs in-container via:
    docker compose exec app python /app/scripts/post_sample_event.py <token>
"""
import sys
import time
import urllib.request

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource


def _kv(key: str, value) -> KeyValue:
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


def build_payload() -> bytes:
    now_ns = time.time_ns()
    record = LogRecord(
        time_unix_nano=now_ns,
        observed_time_unix_nano=now_ns,
        attributes=[
            _kv("event.name", "api_request"),
            _kv("event.timestamp", "2026-04-23T12:00:00Z"),
            _kv("session.id", "sess-abc-123"),
            _kv("organization.id", "org-fake"),
            _kv("user.account_uuid", "acct-uuid-xyz"),
            _kv("user.account_id", "user_01BWBeN28FAKEFAKE"),
            _kv("user.id", "device-1"),
            _kv("user.email", "test@example.com"),
            _kv("terminal.type", "iTerm.app"),
            _kv("model", "claude-sonnet-4-6"),
            _kv("cost_usd", 0.0421),
            _kv("input_tokens", 1234),
            _kv("output_tokens", 567),
            _kv("cache_read_tokens", 8000),
            _kv("cache_creation_tokens", 120),
            _kv("duration_ms", 1800),
            _kv("request_id", f"req_test_{now_ns}"),
            _kv("query_source", "repl_main_thread"),
            _kv("speed", "normal"),
        ],
    )
    # The server requires resource.service.name == "claude-code" — the
    # value real Claude Code's OTLP SDK sets by default.
    resource = Resource(attributes=[
        _kv("service.name", "claude-code"),
        _kv("service.version", "sample-script"),
    ])
    req = ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource=resource,
                scope_logs=[ScopeLogs(log_records=[record])],
            )
        ]
    )
    return req.SerializeToString()


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: post_sample_event.py <bearer-token>", file=sys.stderr)
        sys.exit(2)
    token = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) >= 3 else "http://app:8000/v1/logs"

    body = build_payload()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(f"status: {resp.status}")
        print(f"body bytes: {len(resp.read())}")


if __name__ == "__main__":
    main()
