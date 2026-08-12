# DLR Worker Node image. Build context: repository root.
FROM ghcr.io/astral-sh/uv:latest AS uv
FROM node:22-slim AS node
FROM maven:3.9-eclipse-temurin-21 AS java

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=java /opt/java/openjdk /opt/java/openjdk
COPY --from=java /usr/share/maven /usr/share/maven
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Official Worker image exposes all M3.3 runtimes. Custom/remote Workers may
# install only a subset; the agent detects binaries and reports capabilities.
ENV JAVA_HOME=/opt/java/openjdk

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install locked dependencies first for better layer caching.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:/opt/java/openjdk/bin:/usr/share/maven/bin:$PATH"

# The worker agent connects outbound to the Control Node; it exposes no ports.
CMD ["python", "-m", "dlr.worker.agent"]
