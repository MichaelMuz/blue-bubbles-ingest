FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
ENV HOME=/tmp \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv
USER 65532:65532
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "blue_bubbles_ingest.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

