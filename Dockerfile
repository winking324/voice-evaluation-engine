# syntax=docker/dockerfile:1.7

FROM node:26.13.1-bookworm-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11.18-slim-bookworm AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python-base AS dependencies
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra audio --extra providers --no-install-project
COPY src/ ./src/
RUN uv sync --locked --no-dev --extra audio --extra providers --no-editable

FROM python-base AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home app \
    && mkdir -p /app /data/manifests /data/audio /data/runs /data/cache \
    && chown -R app:app /app /home/app /data/runs /data/cache
WORKDIR /app
COPY --from=dependencies --chown=app:app /opt/venv /opt/venv
COPY --from=web-build --chown=app:app /web/dist /app/web/dist
COPY --chown=app:app src/ ./src/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/home/app \
    WEB_DIST_DIR=/app/web/dist
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "voice_eval.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS test
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra audio --extra providers --extra test --no-install-project
COPY src/ ./src/
COPY tests/ ./tests/
RUN uv sync --locked --extra audio --extra providers --extra test --no-editable \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home app \
    && chown -R app:app /app /opt/venv /home/app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/home/app
USER 10001:10001
ENTRYPOINT ["pytest"]
CMD ["-q", "-p", "no:cacheprovider"]
