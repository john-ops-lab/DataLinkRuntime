# DLR Control Node image. Build context: repository root.
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Supervised Control children start fresh interpreters. Cache bytecode for the
# locked dependencies and the base Python standard library in reusable layers.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode
RUN python -m compileall -q -x '/(site-packages|test|tests)/' \
    "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"

COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
RUN uv sync --frozen --no-dev \
    && /app/.venv/bin/python -m compileall -q -x '/template_catalog/variants/' src/dlr

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "dlr.control.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
