# JoltBot deployment

How the live JoltBot server is set up and how to look after it. The [README](README.md) explains the software in general; this page is about **this** server.

Every command says where to run it:

- **PC** means PowerShell on your own computer.
- **Server** means a shell on the server, after `ssh ubuntu@18.221.166.235`.

The server's address is `18.221.166.235`. It is a Lightsail static IP, so it stays the same when the server is stopped and started. The dashboard is at `https://joltbot.jolteer.com`.

Until 23 November 2026 JoltBot ran on a Hetzner server, with the dashboard at `2-31-91-83.sslip.io`. That address no longer works.

## How it is set up

| What | Where |
|---|---|
| Server | AWS Lightsail instance `joltbot`, Ohio (us-east-2), the 2 GB RAM / 2 vCPU / 60 GB plan, Ubuntu 24.04 |
| Addresses | IPv4 `18.221.166.235` (static IP `joltbot-ip`), IPv6 `2600:1f16:1e2c:6700:7ffb:e977:3578:4e1c` |
| Login | User `ubuntu` with your SSH key; passwords are switched off. `sudo` for system work |
| Code | `/home/ubuntu/joltbot`, a git clone of `github.com/Jolteer/discord_bot` (branch `main`) |
| Secrets and settings | `/home/ubuntu/joltbot/.env`, readable by `ubuntu` only (mode 600), never in git |
| Processes | Docker Compose project `joltbot`: `bot`, `web` (dashboard) and `caddy` (HTTPS) |
| Database | SQLite file in the Docker volume `joltbot_joltbot-data` |
| Backups | `/home/ubuntu/joltbot-backups`, one file per night, kept for 7 days |
| Dashboard | `https://joltbot.jolteer.com`, certificate obtained and renewed by Caddy |
| DNS | Cloudflare, domain `jolteer.com`: an A and an AAAA record named `joltbot`, both set to "DNS only" (grey cloud) |

- **Restarts.** Every container has `restart: unless-stopped` and Docker starts at boot, so JoltBot comes back after a crash or a reboot.
- **Firewall.** Lightsail console → instance `joltbot` → **Networking** → **Firewall rules**. Open: TCP 22 (SSH), TCP 80, TCP 443 and UDP 443, each from any IPv4 or IPv6 address. This is the only firewall. `ufw` on the server is switched off, because Docker publishes container ports past `ufw` and it would not protect them. The dashboard's own port 8000 listens on `127.0.0.1` only; the internet reaches it through Caddy.
- **GitHub access.** The server reads the private repository with a deploy key (`/home/ubuntu/.ssh/joltbot_deploy`, listed on GitHub as `lightsail`). It is read-only and works for this one repository. To revoke it: GitHub → the repository → Settings → Deploy keys.
- **Runs as `ubuntu`, not root.** JoltBot's files, its cron job and the SSH login all belong to the ordinary user `ubuntu`. That user is in the `docker` group and may use `sudo`, so it can still become root; the gain is that nothing runs or logs in as root from day to day.
- **System upkeep.** Security updates install themselves (`unattended-upgrades`) but never reboot the server. Reboot it yourself now and then with `sudo reboot` (**Server**); JoltBot is back within a minute. There is a 2 GB swap file, and the server's clock is set to America/New_York.

## Update to the newest version

1. Push your changes to `main` on GitHub.
2. **PC:**
   ```powershell
   ssh ubuntu@18.221.166.235 "cd ~/joltbot && ./deploy/update.sh"
   ```

The script pulls the code, builds the new image while the old version keeps running, backs up the database, stops the bot and the dashboard, applies database migrations and starts everything again. Downtime is usually under a minute. If the build or the backup fails, it stops and JoltBot is left running as it was.

Check afterwards (**PC**):

```powershell
ssh ubuntu@18.221.166.235 "cd ~/joltbot && docker compose --profile proxy ps"
```

`web` shows `healthy` within seconds, `bot` about two minutes after it connects to Discord.

## Backups

A cron job of the user `ubuntu` runs every night at 03:15 Eastern time:

```
15 3 * * * /home/ubuntu/joltbot/deploy/backup.sh >> /home/ubuntu/joltbot-backups/backup.log 2>&1
```

- See it: `crontab -l` (**Server**). List the backups: `ls -lh ~/joltbot-backups` (**Server**).
- Make one now: `~/joltbot/deploy/backup.sh` (**Server**).
- Files older than 7 days are deleted by the same script.
- A backup holds every server's settings and tickets, but no dashboard logins.
- The backups are on the same machine as JoltBot. Lightsail's automatic snapshots (or a copy to another machine) are what protect you if the whole server is lost. Switch them on in the Lightsail console → instance `joltbot` → **Snapshots** → **Enable automatic snapshots**. Lightsail keeps the newest seven daily snapshots and charges $0.05 per GB stored per month.

## Restore a backup

This replaces the whole database with the copy. Everything saved after that backup is lost, and everyone logs in to the dashboard again.

**Server**, with the real file name in the third line:

```bash
cd ~/joltbot
docker compose stop bot web
gunzip -c ~/joltbot-backups/joltbot-YYYYMMDD-HHMMSS.db.gz | docker compose run --rm --no-deps -T bot \
    sh -c 'cat > /app/data/restore.db && rm -f /app/data/joltbot.db-wal /app/data/joltbot.db-shm && mv /app/data/restore.db /app/data/joltbot.db'
docker compose --profile proxy up -d
```

If you are going back because an update went wrong, the backup is from before that update's database changes, so it needs the older code too: run `git checkout <the commit you were on>` before the last line, and use `docker compose --profile proxy up -d --build` as the last line.

## Give or remove dashboard access for a moderator

Members with Manage Server or Administrator, and the server owner, can always use the dashboard. For everyone else:

- **Give access:** dashboard → the server → **Dashboard access** → tick a role (for example `Moderator`) → Save. Everyone holding that role can now log in at `https://joltbot.jolteer.com` and change JoltBot's settings for that server.
- **Remove one person:** take the role away from them in Discord. Their access ends within a minute.
- **Remove a whole role:** untick it on the Dashboard access page. Access ends at once.

Only Manage Server, Administrator or the owner can open the Dashboard access page. Every change to it is listed under **Audit log** with who made it.

## Change the domain

1. Point the new name at the server and wait until `nslookup the.new.domain` shows `18.221.166.235`. For another name under `jolteer.com`, that is a new A record (and AAAA record) in Cloudflare → `jolteer.com` → DNS → Records, set to "DNS only".
2. **Server:** `nano ~/joltbot/.env` and change two lines:
   ```
   DOMAIN=the.new.domain
   PUBLIC_BASE_URL=https://the.new.domain
   ```
3. Discord Developer Portal → your application → OAuth2 → Redirects: add `https://the.new.domain/auth/callback`.
4. **Server:** `cd ~/joltbot && docker compose --profile proxy up -d`

Everyone logs in again afterwards. Once the new address works, remove the old redirect in the Developer Portal.

If the server's IP address ever changes (for example the static IP is detached or a new instance is made), update both DNS records in Cloudflare to the new addresses. Nothing on the server needs changing for that.

## Logs and alerts

- **Server:** `cd ~/joltbot && docker compose logs --tail 100 bot` (or `web`, `caddy`, `migrate`).
- Errors from the bot and the dashboard are posted to the Discord webhook in `ALERT_WEBHOOK_URL`.
- `https://joltbot.jolteer.com/health/bot` answers 200 while the bot is connected to Discord. A free uptime monitor pointed at it tells you when the bot or the whole server is down.

## Changing a secret

Never paste the bot token, the client secret or the webhook URL into a chat or a commit. Edit them on the server only:

**Server:** `nano ~/joltbot/.env`, then `cd ~/joltbot && docker compose --profile proxy up -d` so both processes read the new value.
