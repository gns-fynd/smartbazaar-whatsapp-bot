# One always-on container: the webhook, the order watcher and the session keep-alive share
# one process, so run exactly one instance (see docs/operations-reliability.md, Render section).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.16 /uv /usr/local/bin/uv

# DejaVu has the glyphs the order-summary PDF needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app app

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/var/data/bot.sqlite3

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY smartbazaar ./smartbazaar
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
