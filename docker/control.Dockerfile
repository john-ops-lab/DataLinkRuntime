# DLR Control Node image. Build context: repository root.
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install locked dependencies first for better layer caching.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "dlr.control.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
