# JoltBot deployment

How the live JoltBot server is set up and how to look after it. The [README](README.md) explains the software in general; this page is about **this** server.

Every command says where to run it:

- **PC** means PowerShell on your own computer.
- **Server** means a shell on the server, after `ssh root@SERVER_IP`.

`SERVER_IP` is the server's IPv4 address (Hetzner Cloud Console → the server → Overview). `HOST` is the dashboard's hostname, which is the same address with dashes plus `.sslip.io`: for `203.0.113.7` it is `203-0-113-7.sslip.io`.

## How it is set up

| What | Where |
|---|---|
| Server | Hetzner Cloud CX23, Ubuntu 24.04 |
| Code | `/root/joltbot`, a git clone of `github.com/Jolteer/discord_bot` (branch `main`) |
| Secrets and settings | `/root/joltbot/.env`, readable by root only (mode 600), never in git |
| Processes | Docker Compose project `joltbot`: `bot`, `web` (dashboard) and `caddy` (HTTPS) |
| Database | SQLite file in the Docker volume `joltbot_joltbot-data` |
| Backups | `/root/joltbot-backups`, one file per night, kept for 7 days |
| Dashboard | `https://HOST`, certificate obtained and renewed by Caddy |

- **Restarts.** Every container has `restart: unless-stopped` and Docker starts at boot, so JoltBot comes back after a crash or a reboot.
- **Firewall.** Only TCP 22 (SSH), 80 and 443 are open, in two places: the Hetzner Cloud Firewall and `ufw` on the server. The Hetzner one is the one that counts, because Docker publishes container ports past `ufw`. The dashboard's own port 8000 listens on `127.0.0.1` only; the internet reaches it through Caddy.
- **GitHub access.** The server reads the private repository with a deploy key (`/root/.ssh/joltbot_deploy`). It is read-only and works for this one repository. To revoke it: GitHub → the repository → Settings → Deploy keys.
- **Runs as root.** Simple, but a break-in through a container escape would have the whole server. Moving it to an unprivileged user in the `docker` group is a possible later hardening step.

## Update to the newest version

1. Push your changes to `main` on GitHub.
2. **PC:**
   ```powershell
   ssh root@SERVER_IP "cd /root/joltbot && ./deploy/update.sh"
   ```

The script pulls the code, builds the new image while the old version keeps running, backs up the database, stops the bot and the dashboard, applies database migrations and starts everything again. Downtime is usually under a minute. If the build or the backup fails, it stops and JoltBot is left running as it was.

Check afterwards (**PC**):

```powershell
ssh root@SERVER_IP "cd /root/joltbot && docker compose --profile proxy ps"
```

`web` shows `healthy` within seconds, `bot` about two minutes after it connects to Discord.

## Backups

A cron job on the server runs every night at 03:15:

```
15 3 * * * /root/joltbot/deploy/backup.sh >> /root/joltbot-backups/backup.log 2>&1
```

- See it: `crontab -l` (**Server**). List the backups: `ls -lh /root/joltbot-backups` (**Server**).
- Make one now: `/root/joltbot/deploy/backup.sh` (**Server**).
- Files older than 7 days are deleted by the same script.
- A backup holds every server's settings and tickets, but no dashboard logins.
- The backups are on the same machine as JoltBot. Hetzner's server backups (or a copy to another machine) are what protect you if the whole server is lost.

## Restore a backup

This replaces the whole database with the copy. Everything saved after that backup is lost, and everyone logs in to the dashboard again.

**Server**, with the real file name in the third line:

```bash
cd /root/joltbot
docker compose stop bot web
gunzip -c /root/joltbot-backups/joltbot-YYYYMMDD-HHMMSS.db.gz | docker compose run --rm --no-deps -T bot \
    sh -c 'cat > /app/data/restore.db && rm -f /app/data/joltbot.db-wal /app/data/joltbot.db-shm && mv /app/data/restore.db /app/data/joltbot.db'
docker compose --profile proxy up -d
```

If you are going back because an update went wrong, the backup is from before that update's database changes, so it needs the older code too: run `git checkout <the commit you were on>` before the last line, and use `docker compose --profile proxy up -d --build` as the last line.

## Give or remove dashboard access for a moderator

Members with Manage Server or Administrator, and the server owner, can always use the dashboard. For everyone else:

- **Give access:** dashboard → the server → **Dashboard access** → tick a role (for example `Moderator`) → Save. Everyone holding that role can now log in at `https://HOST` and change JoltBot's settings for that server.
- **Remove one person:** take the role away from them in Discord. Their access ends within a minute.
- **Remove a whole role:** untick it on the Dashboard access page. Access ends at once.

Only Manage Server, Administrator or the owner can open the Dashboard access page. Every change to it is listed under **Audit log** with who made it.

## Change the domain

1. Point the new domain's DNS A record at `SERVER_IP` and wait until `nslookup the.new.domain` shows it.
2. **Server:** `nano /root/joltbot/.env` and change two lines:
   ```
   DOMAIN=the.new.domain
   PUBLIC_BASE_URL=https://the.new.domain
   ```
3. Discord Developer Portal → your application → OAuth2 → Redirects: add `https://the.new.domain/auth/callback`.
4. **Server:** `cd /root/joltbot && docker compose --profile proxy up -d`

Everyone logs in again afterwards. Once the new address works, remove the old redirect in the Developer Portal.

## Logs and alerts

- **Server:** `cd /root/joltbot && docker compose logs --tail 100 bot` (or `web`, `caddy`, `migrate`).
- Errors from the bot and the dashboard are posted to the Discord webhook in `ALERT_WEBHOOK_URL`.
- `https://HOST/health/bot` answers 200 while the bot is connected to Discord. A free uptime monitor pointed at it tells you when the bot or the whole server is down.

## Changing a secret

Never paste the bot token, the client secret or the webhook URL into a chat or a commit. Edit them on the server only:

**Server:** `nano /root/joltbot/.env`, then `cd /root/joltbot && docker compose --profile proxy up -d` so both processes read the new value.
