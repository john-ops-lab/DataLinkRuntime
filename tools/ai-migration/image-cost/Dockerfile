FROM ghcr.io/astral-sh/uv:latest AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
ARG DLR_REQUIREMENTS=tools/ai-migration/.image-cost/baseline.txt
COPY ${DLR_REQUIREMENTS} /tmp/requirements.txt
RUN --mount=type=bind,source=tools/ai-migration/.image-cost/wheels,target=/wheels uv venv /app/.venv && uv pip install --python /app/.venv/bin/python --no-index --find-links /wheels --requirements /tmp/requirements.txt
COPY backend/src /app/src
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src"
