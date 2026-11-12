# JoltBot image. One image runs all three jobs (see docker-compose.yml):
#   python -m joltbot.web   dashboard (default)
#   python -m joltbot.bot   Discord bot
#   alembic upgrade head    database migrations

# ---- 1. Frontend files: build Tailwind CSS and fetch htmx -------------------------
FROM python:3.12-slim AS assets
ARG TAILWIND_VERSION=4.1.11
ARG HTMX_VERSION=2.0.4
# SHA-256 of each download. The build stops if a file doesn't match, so a swapped or
# corrupted download can never end up in the image. When you change a version above,
# update its checksum too:
#   Tailwind: sha256sums.txt on the release page,
#             https://github.com/tailwindlabs/tailwindcss/releases/tag/v<version>
#   htmx:     curl -sL https://unpkg.com/htmx.org@<version>/dist/htmx.min.js | sha256sum
ARG TAILWIND_SHA256_AMD64=64805b84af4292e043ea6f86d242f191c0ac75359c1a498455dfe6c642afdbab
ARG TAILWIND_SHA256_ARM64=0409aa4222969f47fa6f4160fe5387e79bf7269e7afe0e8b22f7532c98e1d314
ARG HTMX_SHA256=e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447
ARG TARGETARCH
WORKDIR /src
# Tailwind scans the templates for class names, so it needs the source tree.
COPY joltbot/ joltbot/
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64;   tailwind_sha256="${TAILWIND_SHA256_AMD64}" ;; \
      arm64) arch=arm64; tailwind_sha256="${TAILWIND_SHA256_ARM64}" ;; \
      *) echo "Unsupported CPU: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    fetch() { python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2"; }; \
    mkdir /out; \
    fetch "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-${arch}" \
        /usr/local/bin/tailwindcss; \
    fetch "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" /out/htmx.min.js; \
    # Check both downloads before anything is run or kept.
    echo "${tailwind_sha256}  /usr/local/bin/tailwindcss" | sha256sum -c -; \
    echo "${HTMX_SHA256}  /out/htmx.min.js" | sha256sum -c -; \
    chmod +x /usr/local/bin/tailwindcss; \
    tailwindcss -i joltbot/web/styles/input.css -o /out/app.css --minify

# ---- 2. Python dependencies, installed into a virtualenv ---------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock .
# --require-hashes: every package file must match a checksum in the lock file.
RUN /opt/venv/bin/pip install --require-hashes -r requirements.lock

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
