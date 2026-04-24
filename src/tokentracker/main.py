import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .db import lifespan
from .routes import router

_INDEX = Path(__file__).parent / "static" / "index.html"

# "Production-ish" indicator: true whenever we expect to be behind TLS.
# Gates HSTS emission and disables FastAPI's auto-generated docs pages.
_PROD = os.environ.get("COOKIE_SECURE") == "1"

# 5 MB cap on any request body. OTLP batches from Claude Code are ~KB;
# JSON login/register bodies are tiny. 5 MB leaves plenty of headroom for
# honest clients while ruthlessly shutting down body-bomb DoS attempts.
_MAX_BODY_BYTES = 5 * 1024 * 1024


class MaxBodySize(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
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
                "style-src 'self' 'unsafe-inline'; "
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
