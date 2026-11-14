# JoltBot

A Discord bot that any server can invite, with a web dashboard where server admins log in with Discord and manage JoltBot for their own server.

JoltBot runs as **two processes** that share one SQLite database:

| Process | Start with | What it does |
|---|---|---|
| Bot | `python -m joltbot.bot` | Connects to Discord; handles members, buttons, slash commands |
| Dashboard | `python -m joltbot.web` | Website on port 8000: login, server list, settings pages, `/health` |

> **Deploying:** Docker Compose is the main way to run JoltBot in production (section 7). The systemd setup in section 7.4 is kept as a fallback for running without Docker.

**Also see:** [CONTRIBUTING.md](CONTRIBUTING.md) (style, PRs, adding modules) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (startup, routing, storage) · [DEPLOYMENT.md](DEPLOYMENT.md) (how the live server is set up, updated and restored)

---

## Features

- **Multi-server bot** with per-guild settings stored in SQLite (no restart to apply dashboard changes)
- **Web dashboard** — Discord OAuth login, server picker, module settings, audit log, dismissible bot issues
- **Dashboard access roles** — let your moderators use a server's dashboard without giving them Manage Server (section 6)
- **Notices channel** — optionally, each server gets new problems posted once in a channel of its choice
- **Tickets & verification**
  1. New members click **Open Application** and answer the server's own questions (up to 5 typed, plus up to 4 dropdowns whose options can each give a role on accept)
  2. A private ticket channel opens for them
  3. Staff **Accept**, **Deny** (keep or kick), or **Close** with buttons
  4. Transcript, optional attachment re-upload, and decision go to a log channel; then the ticket channel is deleted
- **Recovery** — stuck tickets and unfinished closes resume every 5 minutes and at startup
- **Diagnostics** — `/tickets diagnostics` checks permissions, role hierarchy, and channel visibility
- **Running it** (section 9)
  - Alerts: errors from the bot and the dashboard go to a Discord webhook of yours
  - Health: `docker compose ps`, a dashboard banner and `/health/bot` all show whether the bot is connected
  - Backups and updates: `deploy/backup.sh` (nightly) and `deploy/update.sh`
- **Docker Compose** deploy (migrate → bot + web; optional Caddy HTTPS) and a systemd fallback
- **Legacy KOC Gatekeeper migration** — one-shot import of old config and open tickets (section 8)

---

## Commands

### Slash commands

All three are under `/tickets`, take no arguments and work only inside a server.

Discord hides them from members without **Manage Server** by default. A server's admins can show them to other roles under **Server Settings → Integrations → JoltBot**. Seeing a command is not enough to run it: JoltBot checks the "Who can run it" rule on every use. There are no cooldowns.

| Command | Who can run it | What JoltBot needs | What it does |
|---|---|---|---|
| `/tickets panel` | Manage Server, or staff | View Channel, Send Messages and Embed Links in the panel channel. Pin Messages to pin the panel (optional) | Posts the application panel in the panel channel and pins it when it can. The previous panel is deleted, so there is only ever one |
| `/tickets diagnostics` | Manage Server, or staff | Nothing extra | Private report on JoltBot's permissions, role order, category limits and anything not set up |
| `/tickets grant-existing` | Manage Server only | Manage Roles, and its own role above the accept role(s) | One-time bulk job. After a confirm button, gives the accept role(s) to members who have neither an accept role nor the pending role. Members with an open application are left out. One run per server at a time |

**Staff** means anyone holding one of the server's **Staff roles** (set on the dashboard). Members with Discord's Administrator permission count as staff too, while **Members with Administrator also count as staff** is ticked (it is by default). Manage Server on its own is not staff.

### Buttons

| Button | Where | Who can use it | What JoltBot needs |
|---|---|---|---|
| **Open Application** | The panel message | Any member. Turned away if they already hold an accept role or already have a ticket | Manage Channels and Manage Roles in the ticket category, to create the private ticket channel |
| **Accept** | Each ticket | Staff | Manage Roles, and its own role above every role it gives or removes |
| **Deny** | Each ticket | Staff | Kick Members and a role above the applicant's, only for "Deny and kick" |
| **Close** | Each ticket | Staff | Nothing extra |

Every decision also needs View Channel, Send Messages, Embed Links and Attach Files in the log channel, and Manage Channels to delete the ticket channel afterwards. Staff are re-checked on every click, so removing someone's staff role takes effect at once. Replies and confirmations are private to whoever clicked.

### Commands for whoever runs JoltBot

Run from the project folder. With Docker, `docker compose up -d` already runs the migrations and starts the bot and the dashboard, so only the one-off commands are typed by hand, with `docker compose run --rm bot` in front (for example `docker compose run --rm bot python -m joltbot.bot --test-alert`).

| Command | What it does |
|---|---|
| `python -m joltbot.bot` | Starts the bot |
| `python -m joltbot.bot --test-alert` | Posts one test message to `ALERT_WEBHOOK_URL` and exits |
| `python -m joltbot.bot.healthcheck` | Says whether the bot is connected to Discord (exit code 0 or 1). Docker's health check |
| `python -m joltbot.web [--host H] [--port P] [--reload]` | Starts the dashboard |
| `alembic upgrade head` | Creates or updates the database tables |
| `python -m scripts.backup [--out FILE]` | Makes one database backup (section 9) |
| `python -m scripts.preview_site [--port P] [--bot-down]` | Runs the website with example data, no Discord needed (section 4) |
| `python -m scripts.migrate_koc …` | The one-time King of Chaos import (section 8) |
| `./deploy/update.sh [--no-pull]` | Updates a Docker server (section 9) |
| `./deploy/backup.sh` | Backs up the database of a Docker server (section 9) |

---

## Tech stack

| Layer | Tech |
|---|---|
| Bot | Python 3.11+ (3.12 recommended), discord.py 2.x |
| Dashboard | FastAPI, uvicorn, Jinja2, htmx, Tailwind CSS |
| Data | SQLAlchemy 2 async + aiosqlite (SQLite WAL); Alembic migrations |
| Config | pydantic-settings from environment / `.env` |
| Deploy | Docker Compose; optional Caddy |

Pinned deps: [`requirements.txt`](requirements.txt). Full locks with a checksum for every file: `requirements.lock` (production) and `requirements-dev.lock` (plus test tools).

---

## Contents

1. [Project layout](#1-project-layout)
2. [Discord Developer Portal setup](#2-discord-developer-portal-setup)
3. [Environment variables](#3-environment-variables)
4. [Local development](#4-local-development)
5. [Running with Docker](#5-running-with-docker)
6. [Setting up JoltBot in a server](#6-setting-up-joltbot-in-a-server)
7. [Deploying](#7-deploying)
8. [Moving King of Chaos from the old bot](#8-moving-king-of-chaos-from-the-old-bot)
9. [Updating, backups and logs](#9-updating-backups-and-logs)
10. [Troubleshooting](#10-troubleshooting)
11. [How it works (for contributors)](#11-how-it-works-for-contributors)

---

## 1. Project layout

```
joltbot/
├── core/                   # shared by bot AND dashboard (no discord.py / FastAPI here)
│   ├── config.py           # every setting, read from environment variables / .env
│   ├── db.py, models.py    # SQLAlchemy database + tables (SQLite in WAL mode)
│   ├── schema.py           # startup check: is the database up to date?
│   ├── guild_settings.py   # load/save a server's settings + audit log
│   ├── guilds.py           # join / leave / startup sync / 30-day purge
│   ├── issues.py           # problems shown to admins on the dashboard
│   ├── health.py           # the bot's heartbeat: is it connected to Discord?
│   ├── alerts.py           # ERROR-level log lines -> your Discord webhook
│   ├── discord_rest.py     # Discord REST client for the dashboard (cached roles/channels)
│   ├── logsetup.py         # logging (console + optional rotating file; text or JSON)
│   ├── permissions.py      # permission bits + invite link
│   ├── limits.py           # Discord's size limits (message, embed, form, dropdown), named once
│   ├── templating.py       # {user}/{server} placeholders
│   └── types.py            # Discord ID types, utcnow()
├── modules/
│   ├── registry.py         # the list of modules; add new ones here (section 11)
│   ├── base.py             # base class of every module's settings
│   ├── general/            # embed colour, notices channel, dashboard access roles, module switches
│   │   ├── settings.py     #   the per-server settings
│   │   ├── web.py          #   the dashboard page
│   │   └── templates/
│   └── tickets/
│       ├── settings.py     #   the per-server settings (section 6 lists them)
│       ├── constants.py    #   ticket statuses, outcomes and issue keys stored in the database
│       ├── cog.py          #   bot: /tickets commands, join/leave events, recovery loop
│       ├── views/          #   bot: buttons and forms (applicant.py, staff.py, common.py)
│       ├── service.py      #   ticket lifecycle: create, accept, deny, close, finalize, recover
│       ├── store.py        #   ticket database operations
│       ├── channels.py     #   the ticket channel's name and permissions
│       ├── log_post.py     #   posting the decision, transcript and files to the log channel
│       ├── transcript.py   #   channel history -> text transcript
│       ├── embeds.py       #   every embed and DM
│       ├── checks.py       #   who is staff; can JoltBot give this role or kick this member?
│       ├── diagnostics.py  #   the /tickets diagnostics report
│       ├── context.py      #   loads a server's settings for one event
│       ├── forms.py        #   dashboard: reads and checks the settings form
│       ├── web.py          #   dashboard: the Tickets page
│       └── templates/
├── bot/                    # the bot process
│   ├── __main__.py         #   python -m joltbot.bot: startup checks, retry loop, exit codes
│   ├── client.py           #   the JoltBot class: module loading, slash-command sync, issue reporting
│   ├── guild_sync.py       #   keeps the list of servers current
│   ├── heartbeat.py        #   writes the heartbeat once a minute
│   ├── healthcheck.py      #   Docker's health check for the bot
│   └── interactions.py     #   helper for answering a click or command
└── web/                    # the dashboard process
    ├── __main__.py         #   python -m joltbot.web: runs uvicorn
    ├── app.py              #   builds the app: routes, security headers, error pages
    ├── security.py         #   login, per-server access and CSRF checks
    ├── sessions.py         #   login sessions
    ├── rendering.py        #   Jinja2 templates
    ├── form_helpers.py     #   pieces every settings page shares
    ├── options.py          #   roles and channels shaped for dropdowns
    ├── watchdog.py         #   alerts you when the bot stops reporting in
    ├── routes/             #   health.py, pages.py, auth.py (login), guild.py (overview, audit log, access)
    ├── templates/          #   pages, plus _styles.css (the design system), _form.html, _icons.html
    ├── static/             #   app.js, fonts, icons; app.css and vendor/htmx.min.js are built, not committed
    └── styles/             #   input.css (Tailwind entry) and the source of the share image
migrations/                 # Alembic database migrations
scripts/migrate_koc.py      # one-time King of Chaos migration
scripts/backup.py           # makes one database backup (used by deploy/backup.sh)
scripts/preview_site.py     # runs the website with example data, no Discord needed (section 4)
docs/ARCHITECTURE.md        # startup, routing, storage, external APIs
deploy/Caddyfile            # HTTPS proxy for Docker Compose
deploy/backup.sh            # nightly database backup (Docker)
deploy/update.sh            # updates a Docker server
deploy/systemd/             # fallback: systemd units + Caddyfile for running without Docker
tests/                      # pytest; fakes.py stands in for Discord, example_module/ is the README's example
CONTRIBUTING.md             # style, PRs, how to add a module
DEPLOYMENT.md               # how the live server is set up, with its exact commands
Dockerfile, docker-compose.yml, .dockerignore
.env.example                # copy to .env and fill in (section 3)
alembic.ini, pyproject.toml # migration and lint/test settings
requirements.txt            # direct dependencies, exact versions (edit this one)
requirements.lock           # every dependency, exact versions + checksums (generated; used by Docker)
requirements-dev.txt        # requirements.txt + pytest + ruff (edit this one)
requirements-dev.lock       # the same, fully pinned + checksums (generated; used by CI and for development)
.github/workflows/ci.yml    # lint, format check, tests, Docker build on every push / PR
```

---

## 2. Discord Developer Portal setup

Open <https://discord.com/developers/applications> and create an application, or open the one you already have. Renaming an existing application keeps the same bot account and token, and the bot stays in the servers it is in.

**General Information**
- **Name:** `JoltBot`
- **App Icon:** upload the JoltBot logo
- **Description:** e.g. "Verification tickets with a web dashboard."

**Bot**
- **Username:** `JoltBot`. Discord only allows a couple of username changes per hour.
- **Avatar:** upload the same logo.
- **Public Bot: ON**, so other servers can invite JoltBot.
- **Privileged Gateway Intents:**
  - ✅ **Server Members Intent**: needed for join/leave events and giving roles
  - ✅ **Message Content Intent**: needed for transcripts; without it they come out empty
- **Token:** if you need a new one, click **Reset Token** and put it in `.env` as `DISCORD_TOKEN`.

**OAuth2**
- Copy the **Client ID** → `DISCORD_CLIENT_ID`.
- Click **Reset Secret** and copy it → `DISCORD_CLIENT_SECRET`. Keep it as private as the token.
- Under **Redirects**, add both:
  - `http://localhost:8000/auth/callback` (development, and the SSH tunnel in section 7.3)
  - `https://joltbot.example.com/auth/callback`, using your real domain once you have one
- These must match `PUBLIC_BASE_URL` + `/auth/callback` exactly.

> Once JoltBot is in 100 servers, Discord requires **bot verification** and approval for both privileged intents. The reason to give for Message Content is "ticket transcripts".

### Invite link and permissions

The dashboard builds invite links for you. The general one looks like this:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=2251800082377746
```

| Permission | Why |
|---|---|
| View Channels, Send Messages, Embed Links, Read Message History | Basic operation |
| Attach Files | Transcripts and re-uploaded files in the log channel |
| Manage Channels | Create and delete ticket channels |
| Manage Roles | Give roles; set private permissions on each ticket |
| Manage Messages | Moderate inside tickets |
| Pin Messages | Pin the application panel |
| Kick Members | Only for "Deny and kick" |
| Mention All Roles | Lets the new-ticket ping notify staff roles that aren't set to mentionable. JoltBot never pings @everyone. |

JoltBot deliberately does **not** ask for Administrator.

---

## 3. Environment variables

All settings come from environment variables, or from a `.env` file in the project folder. Copy `.env.example` to `.env` and fill it in. **Never commit `.env`**: `.gitignore` keeps it out of git, and `.dockerignore` keeps it out of the image.

| Variable | Required | Default | What it's for |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token. Used by the bot and the dashboard. |
| `DISCORD_CLIENT_ID` | dashboard | | OAuth2 client ID ("Login with Discord"). |
| `DISCORD_CLIENT_SECRET` | dashboard | | OAuth2 client secret. |
| `SESSION_SECRET` | dashboard | | Long random string. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `PUBLIC_BASE_URL` | | `http://localhost:8000` | Where people open the dashboard. `https://…` turns on secure cookies and HSTS. |
| `DATABASE_URL` | | `sqlite+aiosqlite:///data/joltbot.db` | Docker sets `/app/data/joltbot.db` (in the `joltbot-data` volume). |
| `HOST` | | `127.0.0.1` | Dashboard listen address. Docker Compose sets `0.0.0.0`. |
| `PORT` | | `8000` | Dashboard port. |
| `FORWARDED_ALLOW_IPS` | | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted. Docker Compose sets `*` (see the comment there). |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LOG_DIR` | | `logs` | Folder for rotating log files. Empty = console only (Docker sets this). |
| `LOG_FORMAT` | | `text` | `text` for readable lines, `json` for one JSON object per line (`ts`, `level`, `logger`, `message`, plus fields such as `guild_id`). |
| `ALERT_WEBHOOK_URL` | | | Discord webhook that receives ERROR-level log lines from both processes (section 9). A secret. Empty = no alerts. |
| `DOMAIN` | Caddy | | Docker Compose + Caddy only: the domain to get an HTTPS certificate for. |

If a required variable is missing, the process stops at startup and names it. Logs never contain the token, the secrets or the one-time login code.

Two more variables are read only by `deploy/backup.sh`, from the shell that runs it, not from `.env`:

| Variable | Default | What it's for |
|---|---|---|
| `BACKUP_DIR` | `~/joltbot-backups` | Folder the backups are written to. |
| `KEEP_DAYS` | `7` | Backups older than this many days are deleted. |

These are the only global settings. Everything about one Discord server is set per server on the dashboard; section 6 lists those settings.

### Values fixed in the code

These are not configurable. Each is a named constant in the file shown, if you ever need to change one.

| What | Value | Where |
|---|---|---|
| Stuck or half-finished tickets are picked up again | every 5 minutes; a ticket counts as stuck after 2 minutes | `modules/tickets/cog.py`, `modules/tickets/service.py` |
| Files copied to the log channel per ticket | at most 25 | `modules/tickets/log_post.py` |
| Bot heartbeat | every 60 seconds; shown as offline after 3 minutes | `core/health.py` |
| Alert that the bot is gone | after 5 minutes | `web/watchdog.py` |
| Wait before the bot restarts itself after a crash | 10 seconds, doubling up to 5 minutes | `bot/__main__.py` |
| Alerts to the webhook | the same error at most once per 10 minutes; at most 10 alerts per 10 minutes | `core/alerts.py` |
| Data of a server JoltBot left | deleted after 30 days | `core/guilds.py` |
| Dashboard login | lasts 7 days | `web/sessions.py` |
| A user's server list and permissions | re-checked every 5 minutes; reused for up to 30 minutes if Discord is down | `web/sessions.py` |
| A server's roles, channels and a member's roles | cached for 60 seconds | `core/discord_rest.py` |
| Log files (when `LOG_DIR` is set) | 2 MB each, 5 older files kept | `core/logsetup.py` |

---

## 4. Local development

You need **Python 3.12** (3.11+ works). In the project folder (PowerShell on Windows):

```powershell
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.lock
copy .env.example .env                 # macOS/Linux: cp .env.example .env
```

Fill in `.env` (section 3). Leave `PUBLIC_BASE_URL=http://localhost:8000`.

Create the database, then start each process in its own terminal:

```powershell
alembic upgrade head             # creates data\joltbot.db
python -m joltbot.bot            # terminal 1
python -m joltbot.web --reload   # terminal 2; then open http://localhost:8000
```

> Use a separate test Discord server, and ideally a separate test application, while developing, so you don't disturb the servers that use the live bot.

### Tests, lint and formatting

```powershell
pytest                     # all tests; they need no Discord access
ruff check .               # lint (add --fix to fix the simple ones)
ruff format .              # format the code (CI runs `ruff format --check .`)
```

GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs all three on every push and pull request, then builds the Docker image and checks that `/health` answers.

### Frontend files (Tailwind CSS and htmx)

**The Docker image builds both itself**, so you only need this to see the real styles when running without Docker.

- Until `joltbot/web/static/app.css` exists, pages load Tailwind from a pinned CDN build. That's fine for development. As soon as `app.css` exists, the CDN is no longer used **or allowed** (the Content-Security-Policy drops it).
- Without `vendor/htmx.min.js`, "Load more" in the audit log falls back to a normal link.

```powershell
# htmx
Invoke-WebRequest https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -OutFile joltbot\web\static\vendor\htmx.min.js
# Tailwind standalone CLI (no Node.js needed; .gitignore keeps it out of git)
Invoke-WebRequest https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.11/tailwindcss-windows-x64.exe -OutFile tailwindcss.exe
.\tailwindcss.exe -i joltbot/web/styles/input.css -o joltbot/web/static/app.css --minify   # add --watch while editing
```

`app.css` is a build output, so it's in `.gitignore`.

**Where the look of the site lives:**

- `joltbot/web/templates/_styles.css` is the design system: the colour, type, radius and shadow tokens (CSS variables) and every shared component (`jb-btn`, `jb-card`, `jb-input`, the Discord message mock-ups, …). The real build imports it through `styles/input.css`, and the CDN fallback includes the very same file, so there is one place to change.
- `joltbot/web/templates/_icons.html` holds the icons as inline SVG, and `_form.html` the form pieces every settings page uses.
- `joltbot/web/static/fonts/` holds the two fonts (Inter and Bricolage Grotesque, both under the SIL Open Font License), served from the site itself.
- `joltbot/web/styles/og-image.html` is the source of `static/og-image.png`, the picture shown when a link to the site is shared. The comment at the top says how to render it again.

### Previewing the site without Discord

```powershell
python -m scripts.preview_site           # then open http://localhost:8000
```

This starts the dashboard with a stand-in for Discord and a throwaway database of example data. "Login with Discord" logs you in at once as an example admin, so every page can be clicked through and every form saved. It needs no `.env`, and it reads and changes nothing real. Add `--bot-down` to see the dashboard as it looks while the bot is disconnected. It is for working on templates and styles only: it listens on this computer alone, and "Add to Discord" leads nowhere useful because there is no real application behind it.

### Changing dependencies

1. Edit the version in `requirements.txt` (or `requirements-dev.txt` for a test or lint tool).
2. Regenerate both lock files. They pin every sub-dependency for Windows, Linux and macOS, with a checksum for every file:
   ```powershell
   pip install uv
   uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes -o requirements.lock
   uv pip compile requirements-dev.txt --universal --python-version 3.11 --generate-hashes -c requirements.lock -o requirements-dev.lock
   pip install --require-hashes -r requirements-dev.lock
   pytest
   ```
3. Commit the file you edited and both lock files. A test fails if they disagree.

Installs check those checksums (`--require-hashes` in the Dockerfile and CI), so a package that was swapped or tampered with after the lock was made is refused. The Dockerfile checks its two direct downloads (the Tailwind CLI and htmx) the same way; the checksums sit next to the version numbers at the top of the file.

---

## 5. Running with Docker

You need Docker with Compose v2 (Docker Desktop on Windows/macOS).

```bash
cp .env.example .env               # fill it in (section 3)
docker compose up -d --build       # builds the image, runs migrations, starts bot + dashboard
docker compose ps                  # migrate: exited (0); bot: running; web: running (healthy)
docker compose logs -f bot web     # follow the logs
docker compose down                # stop (the database volume is kept)
```

Open <http://localhost:8000>. What happens on `up`:

1. **migrate** runs `alembic upgrade head` once and exits.
2. **bot** and **web** start only after migrate **succeeded** (`depends_on: condition: service_completed_successfully`). If a migration fails, neither starts, and `docker compose logs migrate` shows why.
3. Both share the SQLite file in the `joltbot-data` volume. WAL mode plus a 5-second busy timeout lets them write at the same time.
4. **web** has a health check (`GET /health`, which also checks the database). **bot** has no web server, so while it is connected to Discord it writes a heartbeat to the database every minute, and its health check reads that. `docker compose ps` shows the bot as `healthy` about two minutes after start, and `unhealthy` once it has been disconnected for three minutes. Docker restarts the bot if it exits, but not when it is only unhealthy.

The image:
- is built in three stages: frontend files, Python packages, final image
- runs as an unprivileged user (uid 10001)
- serves the dashboard with uvicorn, a production ASGI server

`docker compose down -v` also **deletes the database volume**. Only use it to start over.

---

## 6. Setting up JoltBot in a server

These steps are the same for every server, and each server's settings are its own. Whoever does them needs **Manage Server** there. Nothing is switched on until an admin sets it up.

1. **Invite JoltBot.** Log in on the dashboard, find the server under **Not added yet** and click **Invite JoltBot**.
2. **Role order (the #1 cause of "it doesn't work").** Discord only lets a bot give roles that sit **below its own highest role**.
   - In **Server Settings → Roles**, drag the **JoltBot** role above every role it gives: the accepted role (e.g. Member), the pending role (e.g. Unverified) and any extra roles.
   - Keeping it below your leadership roles means it can never kick them.
3. **Dashboard → Tickets & verification.** Pick the staff roles, the panel channel, the ticket category, the log channel and the role(s) given on accept.
   - Optionally pick a pending role, extra role choices, your own form questions and messages.
   - Optionally add **dropdown questions** (for example "Team" or "Region"). Applicants pick from a list before the form, and each option can give a role when staff accept. Those roles must sit below JoltBot's role too. Each option can show an icon: type the name of one of the server's own emojis (for example `infantry`) or paste a normal emoji.
   - Tick **Ticket module is ON** and save. The page warns you about missing permissions or role-order problems.
4. **In Discord, run `/tickets panel`** to post and pin the panel, then **`/tickets diagnostics`** for a full check.
   - Optional: on **Dashboard → General**, pick a **Notices channel**. When JoltBot hits a problem in your server (it can't give a role, can't post a transcript, a channel was deleted), it posts it there once, without pinging anyone. The same problems always show as warnings on the dashboard overview.
5. **Already have members?** Run **`/tickets grant-existing`** once. It gives the accepted role to everyone already there, so locking the server (next step) doesn't lock them out.
6. **Optional: lock new members to the panel channel.**
   - Turn off **View Channels** for @everyone and turn it on for the accepted role.
   - Remove any green ✅ @everyone overrides on channels and categories.
   - Allow the pending role to view the panel channel.
   - `/tickets diagnostics` lists every channel a pending member can still see.
   - Check your work with **Server Settings → Roles → (pending role) → View Server As Role**.

The `/tickets` commands are only visible to members with **Manage Server** by default. To let other roles see them, go to **Server Settings → Integrations → JoltBot**. Members with a staff role can also run `panel` and `diagnostics`; `grant-existing` always needs Manage Server.

### Letting moderators use the dashboard

By default only the owner and members with **Manage Server** or **Administrator** can open a server's dashboard. To let others in without giving them those permissions:

1. Open **Dashboard → Dashboard access** (only managers see this page).
2. Tick the role or roles, for example `Moderator`, and save.

Anyone holding one of those roles can now log in, sees the server in their list, and can change JoltBot's settings there. What they **cannot** do:

- open or change the Dashboard access page;
- make JoltBot give out a role with moderation permissions, or one of the access roles themselves (only the owner or an Administrator can).

To remove someone's access, take the role away from them in Discord (it stops working within a minute), or untick the role on the page (it stops at once). Every change to the list is in the **Audit log** with who made it.

Give an access role only to people you would trust with every setting on the dashboard: they can, for example, change which roles count as ticket staff.

### Per-server settings reference

Every setting below belongs to one server and is changed on its dashboard. Changes apply at once, with no restart. The **Key** column is the name the **Audit log** shows when the setting changes.

**General** (Dashboard → General; the access roles are on Dashboard → Dashboard access)

| Setting | Key | Default | Notes |
|---|---|---|---|
| Embed colour | `embed_color` | `#FFB800` | `#RRGGBB`. Used for JoltBot's embeds in this server |
| Notices channel | `notice_channel_id` | none | A text channel of this server. Each new problem is posted there once, without pinging anyone |
| Dashboard access roles | `dashboard_role_ids` | none | Up to 25 roles. Only Manage Server, Administrator or the owner can change this |
| Module switches | `enabled` (per module) | off | A module can only be switched on once its own setup is complete |

**Tickets & verification** (Dashboard → Tickets & verification)

| Setting | Key | Default | Notes |
|---|---|---|---|
| Ticket module is ON | `enabled` | off | Needs the four settings marked "required" |
| Staff roles | `staff_role_ids` | none | Up to 25. Who may accept, deny and close |
| Members with Administrator also count as staff | `allow_administrators` | on | With this off, at least one staff role is required |
| Panel channel | `panel_channel_id` | none | Required. Where `/tickets panel` posts the panel |
| Ticket category | `ticket_category_id` | none | Required. New ticket channels are created here (Discord allows 50 channels per category) |
| Log channel | `log_channel_id` | none | Required. Decisions and transcripts are posted here. Keep it staff-only |
| Welcome channel | `welcome_channel_id` | none | Optional. Only tells `/tickets diagnostics` that pending members may see this channel |
| Pending role | `pending_role_id` | none | Optional. Given on join, removed on accept. Can't also be a role given on accept |
| Give the pending role to people who joined while JoltBot was offline | `assign_pending_on_startup` | off | Skipped, with a warning, when many members have neither role (run `/tickets grant-existing` first) |
| Roles given on accept | `accept_role_ids` | none | Required: at least 1, up to 10 |
| Extra role choices | `extra_role_choices` | none | Up to 24. Each has a label, a role and an optional emoji. Staff may pick one when accepting |
| Ping staff when a ticket opens | `ping_enabled` | on | |
| Ping roles | `ping_role_ids` | none | Up to 10. Empty means the staff roles are pinged. Ping roles can see tickets but can't use the staff buttons |
| Application questions | `form_questions` | 2 questions | 1 to 5. Each has a label (45 characters), a hint (100), required or not, one line or several, and a maximum answer length (1 to 1000, default 200) |
| Warn staff on Accept if the applicant hasn't uploaded a file | `require_attachment` | off | A warning only; staff can still accept |
| Dropdown questions | `choice_questions` | none | Up to 4, each with 2 to 10 options. An option has a label, an optional role given on accept, and an optional icon |
| Delete delay | `delete_delay_seconds` | 10 | 0 to 3600 seconds between closing a ticket and deleting its channel |
| Copy uploaded files to the log channel before deleting the ticket | `reupload_attachments` | on | Discord deletes files with their channel |
| Messages | `messages.<name>` | see below | The texts members see |

Roles and channels must belong to the server, and every role JoltBot gives must sit below JoltBot's own role. A role with moderation permissions, or a dashboard access role, can only be made giveable by the owner or an Administrator.

**Messages.** Placeholders are written in curly brackets and filled in when the message is sent. One that a message doesn't support is left as typed. `{guild}` still works as an old name for `{server}`.

| Message | Key | Longest | Placeholders |
|---|---|---|---|
| Panel title | `panel_title` | 256 | `{server}` |
| Panel text | `panel_description` | 4000 | `{server}` |
| Button label | `panel_button_label` | 80 | none |
| Form title | `form_title` | 45 | none |
| Ticket opened (private reply) | `ticket_opened` | 2000 | `{user}` `{user_name}` `{server}` `{channel}` |
| Welcome message in the ticket | `ticket_intro` | 2000 | `{user}` `{user_name}` `{server}` |
| Accepted DM | `welcome_dm` | 3000 | `{user}` `{user_name}` `{server}` `{role}` `{role_line}` |
| Denied DM | `deny_dm` | 3000 | `{user}` `{user_name}` `{server}` `{reason}` |
| Denied DM: kicked ending | `deny_dm_kicked_suffix` | 500 | `{server}` |
| Denied DM: not kicked ending | `deny_dm_kept_suffix` | 500 | `{server}` |
| Not staff reply | `not_staff` | 2000 | `{server}` |
| Already verified reply | `already_verified` | 2000 | `{user}` `{user_name}` `{server}` |
| Deny reason hint | `deny_reason_hint` | 100 | none |

The two "Denied DM" endings are added to the Denied DM after a blank line; leave one empty to add nothing.

`{user}` is a mention and `{user_name}` the plain name. `{role}` is the extra role staff picked on accept, and `{role_line}` a ready-made "Role: …" line that is empty when none was picked.

---

## 7. Deploying

### 7.1 Which hosts work

The bot and the dashboard share one SQLite file, so **both containers must run on the same machine with a persistent disk**:
- **Works:** any VPS (Hetzner, DigitalOcean, AWS EC2/Lightsail, …) with Docker Compose. This is the recommended setup.
- **Doesn't work as-is:** platforms that run each service on its own machine or with a temporary disk (Render, Railway, Fly.io with separate apps). Those need a shared database such as Postgres first (section 11).

### 7.2 Hetzner VPS with Docker (main path)

These steps assume Ubuntu 24.04 and a user called `botuser`. The live JoltBot server is set up a little differently (it runs from `/root/joltbot`, with Caddy); [DEPLOYMENT.md](DEPLOYMENT.md) has its exact commands.

```bash
# as root / sudo: install Docker (official convenience script)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker botuser          # lets botuser run docker; log out and back in
sudo -iu botuser

# as botuser
git clone YOUR_REPO_URL ~/joltbot
cd ~/joltbot
cp .env.example .env && nano .env        # see below
```

What goes in `.env` on the server:
- The same token, client ID and client secret as in development (or a production app's).
- A **new** `SESSION_SECRET`.
- With no domain yet: `PUBLIC_BASE_URL=http://localhost:8000` (use the SSH tunnel in 7.3).
- With a domain: `PUBLIC_BASE_URL=https://joltbot.yourname.com` and `DOMAIN=joltbot.yourname.com`.

> Moving a server over from the old KOC Gatekeeper bot? Do section 8 **before** the first start below.

Start it:

```bash
docker compose up -d --build                    # no domain yet
docker compose --profile proxy up -d --build    # with a domain: also starts Caddy (HTTPS)
docker compose ps
```

In the **Hetzner Cloud Console → Firewalls**, allow inbound TCP **22** (SSH), plus **80** and **443** (and UDP 443) once you use Caddy. Port 8000 does **not** need to be open: it's published on 127.0.0.1 only.

Containers have `restart: unless-stopped`, and Docker starts on boot, so JoltBot comes back after a reboot.

Once it is running, three short jobs make it safe to leave alone. Section 9 has the details for each:

1. **Alerts:** set `ALERT_WEBHOOK_URL` so errors reach you in Discord.
2. **Backups:** add the nightly `deploy/backup.sh` line to cron.
3. **Uptime:** point a free uptime monitor at `/health/bot` (needs a domain).

From then on, update with `./deploy/update.sh`.

### 7.3 Pointing a domain at the server

**Until you have a domain**, use the dashboard through an SSH tunnel. Leave `PUBLIC_BASE_URL=http://localhost:8000` on the server, then on your PC:

```powershell
ssh -L 8000:127.0.0.1:8000 botuser@YOUR_SERVER_IP
```

While that window is open, <http://localhost:8000> on your PC is the server's dashboard. The `localhost` redirect URL from section 2 makes login work.

**When you get a domain** (e.g. `joltbot.yourname.com`):

1. At your domain registrar's DNS settings, add an **A record**:
   - name `joltbot` (or `@` for the bare domain)
   - value = the server's **IPv4** address, from the Hetzner Cloud Console
   - Optionally add an **AAAA** record with the IPv6 address.
2. Wait until `nslookup joltbot.yourname.com` shows the server's IP. Usually minutes, at most a few hours.
3. In `.env`, set `PUBLIC_BASE_URL=https://joltbot.yourname.com` and `DOMAIN=joltbot.yourname.com`.
4. In the Developer Portal → OAuth2 → Redirects, add `https://joltbot.yourname.com/auth/callback`.
5. Run `docker compose --profile proxy up -d`. Caddy gets the HTTPS certificate automatically (ports 80 and 443 must be open).

### 7.4 Fallback: without Docker (systemd)

Use this only if you can't run Docker. The files are in [deploy/systemd/](deploy/systemd/).

```bash
# as root / sudo
sudo apt update && sudo apt install -y python3 python3-venv git sqlite3
sudo -iu botuser

# as botuser
git clone YOUR_REPO_URL ~/joltbot && cd ~/joltbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env && nano .env
.venv/bin/alembic upgrade head
exit

sudo cp /home/botuser/joltbot/deploy/systemd/joltbot-bot.service /home/botuser/joltbot/deploy/systemd/joltbot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now joltbot-bot joltbot-web
```

- The bot unit restarts forever with growing delays, except for exit code 2 (bad token) and 3 (bad setup). For those, fix the problem and run `sudo systemctl restart joltbot-bot`.
- For HTTPS, install Caddy from its apt repository, copy `deploy/systemd/Caddyfile` to `/etc/caddy/Caddyfile`, put your domain in it and run `sudo systemctl reload caddy`.
- Logs: `journalctl -u joltbot-bot -f`, and `~/joltbot/logs/`.
- To update, stop both first so old code never runs against a migrated database: `sudo systemctl stop joltbot-bot joltbot-web`, back up (`.venv/bin/python -m scripts.backup`), `git pull`, `.venv/bin/pip install -r requirements.lock`, `.venv/bin/alembic upgrade head`, then `sudo systemctl start joltbot-bot joltbot-web`.
- `deploy/backup.sh` and `deploy/update.sh` are for Docker only. The alert webhook, `/health/bot` and the dashboard's offline banner work the same without Docker.

---

## 8. Moving King of Chaos from the old bot

> This was a one-time step for the King of Chaos server and has been done. It is kept for reference, and because the script and the old button IDs it relies on are still in the code. A new server never needs it: section 6 is all there is.

`scripts/migrate_koc.py` copies the old bot's `config.json` and `data/tickets.db` into the JoltBot database. It brings over:
- the same roles and channels
- your Rise of Kingdoms wording, stored as KOC's own messages
- the governor questions
- every old ticket, including open ones, with the same ticket numbers
- the panel location

Old panel buttons and open tickets keep working.

```bash
sudo systemctl stop rok-ticket-bot          # the old bot must not run at the same time
sudo systemctl disable rok-ticket-bot
sudo -iu botuser
cd ~/joltbot
docker compose stop bot                     # if JoltBot is already running

# Copy the old files somewhere the container's user (uid 10001) may write,
# because the script saves a backup of tickets.db next to it.
mkdir -p ~/koc-import
cp ~/rok-ticket-bot/config.json ~/rok-ticket-bot/data/tickets.db* ~/koc-import/
chmod -R a+rwX ~/koc-import

# 1. Check first (saves nothing). This also runs the database migrations.
docker compose run --rm -v ~/koc-import:/import bot \
    python -m scripts.migrate_koc --config /import/config.json --old-db /import/tickets.db --dry-run

# 2. Do it
docker compose run --rm -v ~/koc-import:/import bot \
    python -m scripts.migrate_koc --config /import/config.json --old-db /import/tickets.db

docker compose up -d                        # (add --profile proxy if you use Caddy)
exit
sudo rm /etc/systemd/system/rok-ticket-bot.service && sudo systemctl daemon-reload
```

Keep `~/koc-import` (it holds the backup) until everything works, then delete it. Without Docker, run the same script with `.venv/bin/python -m scripts.migrate_koc --config ~/rok-ticket-bot/config.json --old-db ~/rok-ticket-bot/data/tickets.db`.

Then, in King of Chaos:
1. On first start, JoltBot removes the old per-server slash commands (`/setup-verify` etc.) and registers `/tickets`. Discord can take a minute to update the command list.
2. Run `/tickets diagnostics`.
3. Run `/tickets panel` once to post a fresh panel with the new button IDs.
4. Test with an alt account: apply, upload a screenshot, and accept from your main account.
5. Once it all works, turn off Carl-bot's verification so the two bots don't both act on new members.

The script refuses to run twice. `--force` re-runs it safely: tickets already copied are skipped.

---

## 9. Updating, backups and logs

**Update:**

```bash
cd ~/joltbot
./deploy/update.sh
```

The script does this, in order, and stops at the first failure:

1. `git pull`
2. builds the new image, while JoltBot keeps running on the old one
3. backs up the database (`deploy/backup.sh`, below)
4. stops the bot and the dashboard, so old code never runs against a migrated database
5. `docker compose up -d`: the `migrate` service applies new migrations, then both start again

Downtime is steps 4 to 5, usually well under a minute. If the build or the backup fails, JoltBot is left running as it was. Caddy is included automatically when it is already running. `./deploy/update.sh --no-pull` updates to whatever is checked out, which is how to try a branch.

The first time, the script isn't on the server yet: run `git pull` once by hand, then `./deploy/update.sh`. To change the database schema, see section 11.

**Restart:** on the server, in the project folder. Add `--profile proxy` after `docker compose` if you use Caddy.

```bash
cd ~/joltbot
docker compose restart bot          # restart only the bot
docker compose restart web          # restart only the dashboard; logins are kept
docker compose up -d                # after editing .env: re-creates what changed, with the new values
docker compose stop bot web         # stop both; `docker compose up -d` starts them again
docker compose ps                   # check: web "healthy" within seconds, bot about two minutes after it connects
```

- `restart` does **not** re-read `.env`. After changing a setting or a secret there, use `docker compose up -d`.
- Settings changed on the dashboard need no restart at all.
- A restart is safe at any time: tickets that were half-way through closing are finished within 5 minutes of the bot coming back.
- Without Docker: `sudo systemctl restart joltbot-bot joltbot-web`.
- From your own PC: `ssh USER@YOUR_SERVER "cd ~/joltbot && docker compose restart bot"`.

**Logs:** `docker compose logs -f bot` / `web` / `migrate`. Docker keeps them per container, capped at 5 files of 10 MB each (`x-logging` in `docker-compose.yml`), so they can't fill the disk. The cap applies to containers created after it was added: run `docker compose up -d --force-recreate` once. Set `LOG_FORMAT=json` in `.env` for machine-readable lines.

**Alerts:** to be told when something breaks, create a webhook in a private channel of your own server (Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL) and put it in `.env` as `ALERT_WEBHOOK_URL`. Then:

```bash
docker compose run --rm bot python -m joltbot.bot --test-alert   # posts one test message
docker compose up -d                                             # restart so both processes use it
```

- Every ERROR or CRITICAL log line from the bot or the dashboard is posted there, with its traceback.
- The same error is posted at most once per 10 minutes, and at most 10 alerts go out per 10 minutes, so an outage can't flood the channel.
- It works when the bot is offline too, because a webhook needs no bot connection. A wrong token or a missing setting is reported on every restart attempt (about once a minute under Docker) until it's fixed.
- Server admins don't see these. Problems in their own server appear as warnings on their dashboard.

**Is the bot up?** Four places show it, all reading the same heartbeat:

- `docker compose ps`: the bot is `healthy` or `unhealthy`.
- The dashboard overview shows "JoltBot is offline right now" to server admins.
- With `ALERT_WEBHOOK_URL` set, the dashboard posts one alert when the bot has been disconnected for 5 minutes (a hung bot can't report itself).
- `GET /health/bot` on the dashboard answers 200 while the bot is connected and 503 otherwise. Point a free uptime monitor at `https://your-domain/health/bot` to be told when the bot goes down, including when the whole server is down (which the alert webhook can't report).

**Backups:** the `joltbot-data` volume holds all settings and ticket state. `deploy/backup.sh` copies it out safely while JoltBot keeps running:

```bash
./deploy/backup.sh          # -> ~/joltbot-backups/joltbot-<UTC time>.db.gz
```

- Run it every night with cron. `crontab -e` (as the user that runs JoltBot), then add:
  ```
  15 3 * * * /home/botuser/joltbot/deploy/backup.sh >> /home/botuser/joltbot-backups/backup.log 2>&1
  ```
- Backups older than 7 days are deleted. Change that or the folder with `KEEP_DAYS=30 BACKUP_DIR=/some/folder ./deploy/backup.sh`.
- A backup holds every server's settings and tickets but **no dashboard logins**: sessions are removed from the copy, so the file never contains anyone's Discord login token. Keep the files private all the same.
- A failed backup is logged at ERROR, so it reaches `ALERT_WEBHOOK_URL` if you set one.
- The backups sit on the same server as JoltBot. To survive losing the server, also copy `~/joltbot-backups` elsewhere (for example `rsync` to another machine, or switch on Hetzner's server backups).
- Each server's log channel also keeps the permanent ticket record.
- Without Docker: `.venv/bin/python -m scripts.backup --out ~/joltbot-backups/joltbot-$(date -u +%F).db`.

**Restoring a backup** replaces the whole database with the copy. Everything saved after that backup is lost, and everyone has to log in to the dashboard again.

```bash
cd ~/joltbot
docker compose stop bot web
gunzip -c ~/joltbot-backups/joltbot-YYYYMMDD-HHMMSS.db.gz | docker compose run --rm --no-deps -T bot \
    sh -c 'cat > /app/data/restore.db && rm -f /app/data/joltbot.db-wal /app/data/joltbot.db-shm && mv /app/data/restore.db /app/data/joltbot.db'
docker compose up -d
```

If you are going back because an update failed, the backup is from before that update's migrations, so it needs the code from before the update too: run `git checkout <the commit you were on>` first, and use `docker compose up -d --build` as the last line.

**Security:** never commit `.env`, keep the server patched (`sudo apt upgrade`), and rebuild now and then (`docker compose build --pull`) for base-image security fixes.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose ps` shows bot "Restarting (2)" | Discord rejected the token. Reset it in the Developer Portal, update `.env`, then `docker compose up -d`. |
| bot "Restarting (3)" | Setup problem: missing variable or privileged intents off. `docker compose logs bot` says which. |
| "Privileged intents are off" | Developer Portal → Bot → turn on Server Members + Message Content, save, restart. |
| bot/web never start; migrate "Exited (1)" | A migration failed. Read `docker compose logs migrate`. |
| "The database schema is out of date" / "has no tables" | Docker: `docker compose up -d` (runs migrate). Without Docker: `.venv/bin/alembic upgrade head`. |
| web is "unhealthy" | `docker compose logs web`. `/health` returns 503 when the database can't be opened. |
| bot is "unhealthy" | It is running but has not been connected to Discord for 3 minutes. `docker compose logs bot` shows why (Discord outage, network, or it is still retrying its start-up). `docker inspect joltbot-bot-1 --format '{{json .State.Health.Log}}'` shows the last checks. |
| Dashboard says "JoltBot is offline right now" | Same cause: no heartbeat from the bot for 3 minutes. Check `docker compose ps` and the bot's logs. |
| Login says "redirect_uri" is invalid | The Developer Portal redirect must be exactly `PUBLIC_BASE_URL/auth/callback`. |
| Caddy can't get a certificate | DNS must point at the server, ports 80/443 must be open, and `DOMAIN` must be set in `.env`. |
| Dashboard says JoltBot isn't in the server | Invite it from the server list, then reload. |
| A moderator gets "Not allowed", or doesn't see the server | They need a role ticked under **Dashboard access** (section 6), and must hold it in Discord. A role given a moment ago can take up to a minute to count. |
| "… gives access to this dashboard. Only the server owner or an Administrator can …" | The role you picked is a dashboard access role, so JoltBot giving it out would hand out dashboard access. Ask the owner or an Administrator to save it, or pick another role. |
| Dashboard can't load roles/channels | The bot token in `.env` is wrong, or Discord is briefly down. Use **↻ Refresh**. |
| New roles/channels don't show up in the dashboard | Click **↻ Refresh roles & channels** (it caches for 60 seconds). |
| "… has powerful permissions … Only the server owner or an Administrator can …" | You have Manage Server but not Administrator, and the role you picked can kick, ban, manage roles or similar. Ask the owner or an Administrator to save that setting. Roles they already saved can stay. |
| "My highest role … is not above …" | Drag JoltBot's role higher (section 6, step 2). |
| Panel says applications aren't set up | The module is off or setup isn't finished. Check the dashboard. |
| No message in the notices channel | JoltBot needs View Channel and Send Messages there; the dashboard overview says so if it can't post. Each problem is posted once and not repeated until it has been fixed or dismissed. |
| Closed ticket channel not deleted | JoltBot couldn't post to the log channel. The dashboard overview shows why. Fix it; within 5 minutes JoltBot posts the transcript and deletes the channel itself. |
| Transcript messages are blank | The Message Content intent is off. |
| `/tickets` commands don't appear | Wait a minute after the first start. Re-invite with the `applications.commands` scope. Members without Manage Server don't see them by default. |

---

## 11. How it works (for contributors)

Full write-up: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. How to branch, style, and add a module: **[CONTRIBUTING.md](CONTRIBUTING.md)**.

Short version:

- **Per-server settings.** Each module has a Pydantic model with defaults (e.g. `modules/tickets/settings.py`), stored as JSON in `module_settings`. Unconfigured servers get defaults with the module **off**.
- **Live changes.** The bot reads settings from the DB on every event — no restart, no settings cache.
- **Dashboard access.** `web/security.py` checks on every request that the user has Manage Server / Admin / owner, or holds one of the server's dashboard access roles (asked from Discord with the bot token, cached 60 seconds). People who aren't in the server are refused before any guild DB lookup. Guild list cached 5 minutes (used for up to 30 minutes if Discord can't be reached, then server pages answer 503). Role/channel IDs in forms must belong to that server. Only the owner or an Administrator can make JoltBot give out a role with moderation or management permissions, or a dashboard access role.
- **Sessions and CSRF.** Cookie is opaque; DB stores HMAC of it. Discord OAuth access token is stored on the session (scopes `identify guilds` only). POSTs need CSRF; Origin must match when present.
- **Reliability.** Bot startup retries; ticket recovery every 5 minutes; claim/release; dashboard "issues" for problems with no interaction to reply on.
- **One dashboard worker.** In-memory Discord caches; single uvicorn worker by design.
- **Adding a module:** a folder under `joltbot/modules/<name>/` plus one `ModuleInfo` in `modules/registry.py`. Worked example below.
- **Schema changes:** edit `core/models.py` → `alembic revision --autogenerate` → review → commit. Deploys run `migrate` automatically.
- **Postgres later** (for multi-host PaaS): add `asyncpg`, point `DATABASE_URL` at Postgres. Models already support it, but it **hasn't been tested yet**.

### Adding a module (a new cog)

A module is one feature: its own per-server settings, optionally bot code (a cog), and optionally a dashboard page. Adding one takes three small pieces and no other wiring.

The example is a tiny `hello` module. The same files live in [tests/example_module/](tests/example_module/) and are run by the tests, so this guide can't drift from the code.

**1. Settings:** `joltbot/modules/hello/settings.py` (next to an empty `__init__.py`)

```python
from pydantic import Field

from joltbot.modules.base import ModuleSettingsModel


class HelloSettings(ModuleSettingsModel):
    """What one server can set for the hello module."""

    # {user} and {server} are filled in when the message is sent.
    greeting: str = Field("Hello {user}, welcome to {server}!", min_length=1, max_length=200)
```

Every field needs a default: that is what lets a server that never configured the module load cleanly. Settings are stored as JSON per server, so adding a field needs no database migration.

**2. Bot code:** `joltbot/modules/hello/cog.py`

```python
import discord
from discord import app_commands
from discord.ext import commands

from joltbot.core.guild_settings import load_module
from joltbot.core.templating import render


class Hello(commands.Cog):
    """The /hello command."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(name="hello", description="Get a hello from JoltBot.")
    @app_commands.guild_only()
    async def hello(self, interaction: discord.Interaction) -> None:
        """Reply privately with the server's greeting. Any member can use it, inside a server only."""
        # Read the settings fresh on every use, so dashboard changes apply at once.
        async with self.bot.db.session() as session:
            state = await load_module(session, interaction.guild_id, "hello")
        if not state.enabled:
            await interaction.response.send_message("That feature is switched off on this server.", ephemeral=True)
            return
        text = render(state.config.greeting, user=interaction.user.mention, server=interaction.guild.name)
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot) -> None:
    """discord.py calls this when the bot loads the module."""
    await bot.add_cog(Hello(bot))
```

**3. One registry entry:** in [joltbot/modules/registry.py](joltbot/modules/registry.py), import `HelloSettings` and add to `MODULES`:

```python
(
    ModuleInfo(
        name="hello",  # stored in the database: never rename it once released
        title="Hello",
        description="A friendly /hello command.",
        settings_model=HelloSettings,
        cog="joltbot.modules.hello.cog",
    ),
)
```

That's all. Without writing anything else:

- The bot loads the cog at startup. If it fails to load, the bot logs an error, skips it and keeps the other modules running.
- `/hello` is registered with Discord on the next start (it can take a minute to show up).
- Every server gets the module **switched off**, with an on/off switch on **Dashboard → General** and a card on the overview.
- Switching it on or off is recorded in that server's audit log.

**Rules that keep a module well-behaved** (the tickets module is the reference for all of them):

- **Settings per event.** Call `load_module(session, guild_id, "<name>")` in every handler and do nothing when `state.enabled` is false. Never cache settings and never hard-code a server, channel or role ID.
- **Permissions.** Hide admin commands with `@app_commands.default_permissions(...)`, and check again in code, because server admins can change who sees a command. See `_setup_check` in `modules/tickets/cog.py`.
- **Buttons that survive a restart.** Give the view `timeout=None` and fixed `custom_id`s, and register it with `bot.add_view(...)` in `cog_load`. See `modules/tickets/views/`. Never change a `custom_id` once a message with that button has been posted.
- **Names for fixed values.** Discord's size limits are in `core/limits.py`. Strings that are stored in the database (statuses, issue keys, the module's own name) get a named constant, as in `modules/tickets/constants.py`, and are never changed afterwards.
- **Docstrings.** Every module, class, command, event handler and button gets one, and commands and buttons say who may use them. CONTRIBUTING.md has the style.
- **Problems with nobody to reply to.** `await bot.report_issue(guild_id, "<name>.<what>", "what the admin should do")`, and `bot.resolve_issue(...)` once it works again. The admin sees it on the dashboard and in their notices channel.
- **New bot permissions** (for example Ban Members) go in `BOT_PERMISSIONS` in `core/permissions.py`, which builds the invite link. Servers that already have the bot must grant the permission themselves.
- **A settings page.** Add `web.py` with a FastAPI `router` (prefix `/servers/{guild_id}/<name>`), templates in `templates/<name>/`, and `web="joltbot.modules.<name>.web"` in the registry entry. Start from `modules/general/web.py`. Every route takes `access: RequireGuild`, every POST adds `dependencies=[CsrfProtected]`, and any role or channel ID from a form must be checked against that server.
- **Its own tables.** Add models to `core/models.py` with a `guild_id` foreign key (so a server's data is deleted with it), then `alembic revision --autogenerate`.
- **Tests.** Start from [tests/test_example_module.py](tests/test_example_module.py). [tests/fakes.py](tests/fakes.py) has stand-ins for members, roles, channels and interactions.
