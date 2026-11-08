# JoltBot image. One image runs all three jobs (see docker-compose.yml):
#   python -m joltbot.web   dashboard (default)
#   python -m joltbot.bot   Discord bot
#   alembic upgrade head    database migrations

# ---- 1. Frontend files: build Tailwind CSS and fetch htmx -------------------------
FROM python:3.12-slim AS assets
ARG TAILWIND_VERSION=4.1.11
ARG HTMX_VERSION=2.0.4
ARG TARGETARCH
WORKDIR /src
# Tailwind scans the templates for class names, so it needs the source tree.
COPY joltbot/ joltbot/
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64 ;; \
      arm64) arch=arm64 ;; \
      *) echo "Unsupported CPU: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    fetch() { python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2"; }; \
    fetch "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-${arch}" \
        /usr/local/bin/tailwindcss; \
    chmod +x /usr/local/bin/tailwindcss; \
    mkdir /out; \
    tailwindcss -i joltbot/web/styles/input.css -o /out/app.css --minify; \
    fetch "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" /out/htmx.min.js

# ---- 2. Python dependencies, installed into a virtualenv ---------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock .
RUN /opt/venv/bin/pip install -r requirements.lock

# ---- 3. The final, small image ------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_DIR="" \
    DATABASE_URL="sqlite+aiosqlite:////app/data/joltbot.db"

# Run as an unprivileged user, never root.
RUN groupadd --system --gid 10001 joltbot \
 && useradd --system --uid 10001 --gid joltbot --home-dir /app --no-create-home joltbot

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations/ migrations/
COPY scripts/ scripts/
COPY joltbot/ joltbot/
COPY --from=assets /out/app.css joltbot/web/static/app.css
COPY --from=assets /out/htmx.min.js joltbot/web/static/vendor/htmx.min.js

# The SQLite database lives here; docker-compose mounts a volume on it.
RUN mkdir -p /app/data && chown joltbot:joltbot /app/data
VOLUME /app/data

USER joltbot
EXPOSE 8000
CMD ["python", "-m", "joltbot.web"]
