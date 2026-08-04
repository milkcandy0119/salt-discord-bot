# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.1 AS uv
FROM restic/restic:0.18.1 AS restic
FROM python:3.12.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME=/tmp

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=restic /usr/bin/restic /usr/local/bin/restic

# 先只用鎖定檔安裝正式依賴，讓程式碼變動不必重做整層解析。
COPY pyproject.toml uv.lock README.md .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY personas ./personas
COPY app ./app

RUN mkdir -p /app/data /app/runtime /backups /restore \
    && chown -R 10001:10001 /app /backups /restore

USER 10001:10001

CMD ["python", "-m", "app.main"]
