# Artifact 13.3: one image for the API, the worker and the migration job (ADR-51: one
# codebase, several processes). Compose picks the command. Python 3.12, dependencies from
# uv.lock exactly (--locked), no development tools, runs as an unprivileged user.
FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12.14-slim

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first, so a source change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY db ./db
COPY alembic.ini ./
RUN uv sync --locked --no-dev --no-editable

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin aisre
USER aisre

CMD ["python", "-c", "print('choose a command: see compose.yaml')"]
