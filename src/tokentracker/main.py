import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .db import lifespan
from .routes import router

_STATIC = Path(__file__).parent / "static"
_INDEX = _STATIC / "index.html"

# "Production-ish" indicator: true whenever we expect to be behind TLS.
# Gates HSTS emission and disables FastAPI's auto-generated docs pages.
_PROD = os.environ.get("COOKIE_SECURE") == "1"

# 5 MB cap on any request body. OTLP batches from Claude Code are ~KB;
# JSON login/register bodies are tiny. 5 MB leaves plenty of headroom for
# honest clients while ruthlessly shutting down body-bomb DoS attempts.
_MAX_BODY_BYTES = 5 * 1024 * 1024


class MaxBodySize(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Reject chunked transfer outright. Without Content-Length the size
        # cap below cannot be enforced before reading the full body, so an
        # attacker can pin memory by streaming bytes indefinitely. Honest
        # OTLP/JSON clients always send Content-Length.
        if request.headers.get("transfer-encoding"):
            return JSONResponse(
                {"detail": "Transfer-Encoding not allowed; send Content-Length"},
                status_code=411,
            )
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        {"detail": "payload too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid content-length"}, status_code=400
                )
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), interest-cohort=()"
        )
        if _PROD:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        return response


app = FastAPI(
    lifespan=lifespan,
    title="tokentracker",
    docs_url=None if _PROD else "/docs",
    redoc_url=None if _PROD else "/redoc",
    openapi_url=None if _PROD else "/openapi.json",
)
app.add_middleware(MaxBodySize)
app.add_middleware(SecurityHeaders)
app.include_router(router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@app.get("/style.css", include_in_schema=False)
async def stylesheet() -> FileResponse:
    return FileResponse(_STATIC / "style.css", media_type="text/css")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    return FileResponse(_STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/favicon-192.png", include_in_schema=False)
async def favicon_192() -> FileResponse:
    return FileResponse(_STATIC / "favicon-192.png", media_type="image/png")


@app.get("/favicon-512.png", include_in_schema=False)
async def favicon_512() -> FileResponse:
    return FileResponse(_STATIC / "favicon-512.png", media_type="image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon() -> FileResponse:
    return FileResponse(_STATIC / "apple-touch-icon.png", media_type="image/png")
