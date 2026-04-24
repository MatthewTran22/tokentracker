FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN uv sync \
 && groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid app --shell /usr/sbin/nologin app \
 && chown -R app:app /app

USER app

EXPOSE 8000

# Production default: no --reload. docker-compose (dev) overrides via `command:`.
CMD ["uvicorn", "tokentracker.main:app", "--host", "0.0.0.0", "--port", "8000"]
