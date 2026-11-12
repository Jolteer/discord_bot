#!/usr/bin/env bash
# Back up the JoltBot database from Docker to a folder on this server.
# Safe to run while JoltBot is running.
#
#   ./deploy/backup.sh                                 # -> ~/joltbot-backups/joltbot-<UTC time>.db.gz
#   BACKUP_DIR=/mnt/backups KEEP_DAYS=30 ./deploy/backup.sh
#
# Nightly, from cron (run `crontab -e` as the user that runs JoltBot):
#   15 3 * * * /home/botuser/joltbot/deploy/backup.sh >> /home/botuser/joltbot-backups/backup.log 2>&1
#
# The backup contains every server's settings and tickets, but no dashboard login
# sessions (see scripts/backup.py). Backups older than KEEP_DAYS days are deleted.
# These files live on the same server as JoltBot: copy them somewhere else too
# (see the README) if you want to survive losing the server.
set -euo pipefail
umask 077 # backups are readable by this user only

cd "$(dirname "${BASH_SOURCE[0]}")/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME/joltbot-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

target="$BACKUP_DIR/joltbot-$(date -u +%Y%m%d-%H%M%S).db.gz"
tmp="$target.part"
trap 'rm -f "$tmp"' EXIT

# One throwaway container: make the backup inside it, then stream the file out.
# The backup script's log lines go to stderr, so only the database travels on stdout.
docker compose run --rm --no-deps -T bot \
    sh -c 'python -m scripts.backup --out /tmp/backup.db >&2 && cat /tmp/backup.db' | gzip >"$tmp"

# Never keep something that isn't a real database (e.g. an error message that got gzipped).
if [ "$(gzip -dc "$tmp" 2>/dev/null | head -c 15)" != "SQLite format 3" ]; then
    echo "Backup FAILED: what came out of Docker is not a SQLite database." >&2
    exit 1
fi

mv "$tmp" "$target"
find "$BACKUP_DIR" -maxdepth 1 -name 'joltbot-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backup saved: $target ($(du -h "$target" | cut -f1))"
