"""MaxBodySize middleware tests.

Risk addressed: an attacker can bypass the 5 MB body cap by sending a request
without `Content-Length` (e.g. `Transfer-Encoding: chunked`) — the middleware
only checks the cap when Content-Length is present. We want chunked requests
rejected outright since Claude Code's OTLP exporter always sends Content-Length.
"""
import pytest
import httpx
from fastapi import FastAPI, Request
from httpx import ASGITransport

from tokentracker.main import MaxBodySize, _MAX_BODY_BYTES


@pytest.fixture
def app():
    a = FastAPI()
    a.add_middleware(MaxBodySize)

    @a.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    return a


async def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_under_cap_with_content_length_passes(app):
    async with await _client(app) as c:
        r = await c.post("/echo", content=b"hello")
    assert r.status_code == 200
    assert r.json() == {"len": 5}


async def test_over_cap_with_content_length_rejected(app):
    big = b"x" * (_MAX_BODY_BYTES + 1)
    async with await _client(app) as c:
        r = await c.post("/echo", content=big)
    assert r.status_code == 413


async def test_invalid_content_length_rejected(app):
    async with await _client(app) as c:
        r = await c.post(
            "/echo",
            content=b"hi",
            headers={"content-length": "not-a-number"},
        )
    assert r.status_code == 400


async def test_chunked_request_without_content_length_rejected(app):
    """The bypass case: chunked transfer with no Content-Length."""
    async def chunks():
        yield b"hello"
        yield b"world"

    async with await _client(app) as c:
        r = await c.post("/echo", content=chunks())
    # 411 Length Required is the correct semantic for refusing missing CL.
    assert r.status_code == 411
