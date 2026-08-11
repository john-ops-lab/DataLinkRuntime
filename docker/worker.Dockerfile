# DLR Worker Node image. Build context: repository root.
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
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# The worker agent connects outbound to the Control Node; it exposes no ports.
CMD ["python", "-m", "dlr.worker.agent"]
