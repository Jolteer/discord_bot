#!/usr/bin/env bash
# Update JoltBot on the server (Docker Compose).
#
#   ./deploy/update.sh             # get the latest code, then update
#   ./deploy/update.sh --no-pull   # update to the code that is checked out right now
#                                  # (e.g. a branch you switched to for testing)
#
# What it does, in this order:
#   1. git pull (unless --no-pull)
#   2. build the new image         JoltBot keeps running on the old one meanwhile
#   3. back up the database        deploy/backup.sh; nothing has changed yet
#   4. stop the bot and dashboard  so old code never runs against a migrated database
#   5. docker compose up -d        runs the migrations, then starts both again
#
# If the build or the backup fails, the script stops and JoltBot is left running as it was.
# Downtime is step 4 to 5: usually well under a minute.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

pull=yes
for arg in "$@"; do
    case "$arg" in
        --no-pull) pull=no ;;
        -h | --help)
            sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

# If Caddy is running (started with `--profile proxy`), keep it in the picture.
compose=(docker compose)
if [ -n "$(docker compose --profile proxy ps -q caddy 2>/dev/null)" ]; then
    compose+=(--profile proxy)
fi

step() { printf '\n==> %s\n' "$*"; }

if [ "$pull" = yes ]; then
    step "Getting the latest code"
    git pull --ff-only
fi

step "Building the new image (JoltBot keeps running meanwhile)"
"${compose[@]}" build

step "Backing up the database"
./deploy/backup.sh

step "Stopping the bot and the dashboard"
"${compose[@]}" stop bot web

step "Applying database migrations and starting again"
if ! "${compose[@]}" up -d; then
    cat >&2 <<'EOF'

The update did NOT finish, and JoltBot is stopped.
  - See what went wrong:  docker compose logs migrate
  - The backup made a minute ago is the newest file in ~/joltbot-backups
    (or $BACKUP_DIR). The README (section 9) explains how to go back.
EOF
    exit 1
fi

"${compose[@]}" ps
echo
echo "Updated. The bot shows as 'healthy' about two minutes after it connects to Discord."
