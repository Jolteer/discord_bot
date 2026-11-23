# JoltBot architecture

This document explains how the bot and dashboard fit together: startup, module
registration, event/command routing, data storage, and external APIs. For setup
and deploy steps, see the [README](../README.md). For contribution workflow, see
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Big picture

JoltBot is **two processes** that share one SQLite database:

| Process | Entry point | Role |
|---|---|---|
| Bot | `python -m joltbot.bot` | Discord gateway: members, buttons, slash commands, ticket lifecycle |
| Dashboard | `python -m joltbot.web` | FastAPI site: Discord OAuth login, per-server settings, audit log |

```
┌─────────────────┐     ┌─────────────────┐
│  joltbot.bot    │     │  joltbot.web    │
│  (discord.py)   │     │  (FastAPI)      │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │    shared SQLite      │
         │    (WAL + busy_timeout)│
         └───────────┬───────────┘
                     │
              data/joltbot.db
                     │
         both talk to Discord APIs
         (gateway vs REST)
```

`joltbot/core/` is shared code with **no** discord.py or FastAPI imports, so
either process can use config, DB, guild settings, permissions, and templating.

Feature modules live under `joltbot/modules/` and are listed once in
[`modules/registry.py`](../joltbot/modules/registry.py). The bot loads each
module's cog; the dashboard mounts each module's FastAPI router.

## Bot startup flow

1. `joltbot/bot/__main__.py:main()` loads `AppConfig`, sets up logging.
2. Missing `DISCORD_TOKEN` or a schema mismatch → exit code **3** (`EXIT_BAD_SETUP`).
3. Retry loop calls `run_once()` → `JoltBot` → `bot.start(token)`.
   - `LoginFailure` → exit **2** (bad token; systemd is told not to restart).
   - `PrivilegedIntentsRequired` → exit **3**.
   - Other errors → sleep with growing delay (10s → 300s), then retry.
4. `JoltBot.setup_hook()` (`bot/client.py`):
   - Load the `guild_sync` and `heartbeat` extensions.
   - For each registry entry with a `cog`, `load_extension(module.cog)`. A module
     that fails to load is logged at ERROR and skipped (`bot.failed_modules`);
     the other modules keep working.
   - Start background slash-command sync (hash-based; retries until Discord accepts it).
     Skipped when a module failed to load, so its commands are not unregistered.
   - SIGTERM (`docker stop`, `systemctl stop`) closes the bot cleanly; the
     process then exits 0.
5. `GuildSync.on_ready` upserts present guilds into the DB and schedules the
   daily 30-day purge of left guilds.
6. Tickets cog registers **persistent views**, starts the 5-minute recovery loop,
   and optionally sweeps pending roles once at first ready.

## Dashboard startup flow

1. `joltbot/web/__main__.py` runs uvicorn with factory `joltbot.web.app:create_app`.
2. Lifespan fails fast if `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`,
   `DISCORD_CLIENT_SECRET`, or `SESSION_SECRET` is missing; optionally checks schema.
3. `create_app()` mounts `/static`, security middleware, then routers:
   `health` → `pages` → `auth` → `guild` → each module `web` router from the registry.
4. Single uvicorn worker on purpose: Discord role/channel caches are in-process.

## Module registry

[`ModuleInfo`](../joltbot/modules/registry.py) describes each feature:

- `name` — DB key; never rename after release
- `settings_model` — Pydantic model (subclass of `ModuleSettingsModel`)
- `toggleable` — `False` for always-on modules like `general`
- `cog` / `web` — optional dotted paths loaded only by the matching process

Current modules: **general** (embed colour + module toggles), **tickets**
(verification flow), **alliances** (join requests decided by each alliance's
own leadership) and **modlog** (records joins, edits, deletes, bans and server
changes in log channels; its dashboard page is called Logging). The mod log
still lacks the cleanup that `retention_days` describes, so its entries are
kept until that exists, and it has no slash commands yet.

Adding a module: create `joltbot/modules/<name>/` with `settings.py`, optional
`cog.py` / `web.py` / `templates/`, then one `ModuleInfo` entry. No other
wiring. A module without `web` is bot-only: it gets an overview card and an
on/off switch on the General page, but no sidebar link. The README (section 11)
has a worked example, which `tests/test_example_module.py` runs.

## How Discord events and commands are routed

### Slash commands

- Defined on cogs with `discord.app_commands` (e.g. `Tickets` group cog).
- Tree uses a custom `Tree.on_error` for check failures and missing bot perms.
- Global sync is deferred and hashed in `bot_meta` so Discord is not hit on
  every restart. Optional clear of legacy per-guild commands after KOC migration.

### Persistent components

- Panel and ticket control views (`modules/tickets/views/`) use fixed `custom_id`s and `timeout=None`.
- Registered in `cog_load` so buttons on old messages still work after restart.
- Legacy `rokverify:` / `rokticket:` IDs remain registered for Gatekeeper panels.
- The alliance menu and the Accept / Reject buttons (`modules/alliances/views.py`) work the same
  way. A click finds its request by the ID of the message it was made on.

### Events

Tickets cog listens for join (pending role), raw member remove (close ticket if
applicant left), and channel delete. Guild sync listens for join/remove/ready.

### Settings freshness

The bot calls `load_module()` from the DB on **every** event. Dashboard saves
apply immediately; there is no settings cache to invalidate.

## Ticket lifecycle

```
applicant opens panel
        │
        ▼
   reserve row (open)
        │
        ▼
  create private channel
        │
        ▼
 staff Accept / Deny / Close
        │
        ▼
 claim: open → closing   (atomic UPDATE; prevents double-handling)
        │
        ▼
 finalize: mute → transcript → log channel → mark closed
        │
        ▼
 delay → delete channel   (only if log succeeded)
```

Key rules (see `modules/tickets/service.py` and `store.py`):

- Partial unique index: one `open`/`closing` ticket per `(guild_id, user_id)`.
- `claim()` is a conditional UPDATE; only one worker wins.
- Failures after claim should `release` or finish finalize so tickets do not stick.
- Recovery every 5 minutes resumes stuck `closing` tickets, missing channels, etc.
- Channel is not deleted if the log post failed (preserve evidence). Recovery
  posts the log again once the log channel works, then deletes the channel.
- Dropdown questions (`TicketSettings.choice_questions`) are asked in a private
  message before the form. A pick is stored with the answers as a `role_id`,
  but it is only a request: on accept, a role is given only if it is still an
  option's role in the current settings (`TicketSettings.picked_roles`).
- `TicketSettings.assignable_roles()` is the one list of roles JoltBot gives.
  Role-order checks, diagnostics and the dashboard's powerful-role rule all read it.

## Alliance join requests

```
member picks an alliance from the menu  ->  form: in-game name + governor ID
        │
        ▼
   insert row (pending)  ->  post it in that alliance's request channel
        │
        ├── Accept:  claim pending → deciding   (atomic UPDATE)
        │            set the member's roles in one call (new alliance role in, others out)
        │            deciding → accepted        (or back to pending if Discord refused)
        ├── Reject:  pending → rejected         (atomic UPDATE, optional reason)
        ├── member leaves:           pending → closed
        └── /alliances cancel:       pending → cancelled
        │
        ▼
 edit the request message (buttons greyed out) → DM the member → post in the log channel
```

Key rules (see `modules/alliances/service.py` and `store.py`):

- **Who may decide** is checked in the button handler on every click
  (`checks.can_review`): a reviewer role of that alliance, or an override role.
  Channel visibility, Administrator and Manage Server are not part of it. Nobody
  decides their own request.
- An alliance is identified by its member role (`AllianceRequest.alliance_role_id`),
  so renaming one keeps its waiting requests. A request whose alliance was removed
  from the settings can't be accepted, only cancelled.
- Partial unique index: one `pending`/`deciding` request per `(guild_id, user_id)`.
- Every way out of `pending` is a conditional UPDATE, so of two reviewers clicking
  at once exactly one wins and the other is told who handled it.
- On accept the member's whole role list is sent in one request (`Member.edit(roles=…)`),
  so there is no moment with two alliance roles, or with the old one gone and the
  new one not given.
- A request left in `deciding` by a restart is reopened when the bot starts
  (`store.release_stuck`). Accepting it again gives the same result.
- The cooldown after a rejection is per alliance, read from the member's last
  rejected request to that alliance.
- `AllianceSettings.assignable_roles()` lists the member roles, which puts them
  under the dashboard's powerful-role rule and keeps them from being dashboard
  access roles.

## Dashboard auth and access control

1. **OAuth** (`identify guilds`): `/login` → Discord → `/auth/callback` with
   one-time `state` cookie.
2. **Session**: opaque cookie token; DB stores HMAC-SHA256(`SESSION_SECRET`, token).
   Discord access token is stored on the session row (needed to refresh guild list).
3. **CSRF**: per-session token on POSTs; Origin must match `PUBLIC_BASE_URL` when present.
4. **Guild access** (`RequireGuild`, `web/security.guild_access`), checked on every
   request to a `/servers/{guild_id}/...` page. Two ways in:
   - **Manager**: Manage Server / Admin / owner in Discord for that guild. Then
     the guild must exist and be `active`.
   - **Dashboard access role**: the user holds one of the guild's
     `dashboard_role_ids` (General settings). Their current roles come from
     `DiscordClient.member_role_ids` (bot token, `GET /guilds/{id}/members/{user}`),
     cached 60 seconds per member. If Discord can't be asked, the answer is 503:
     roles are never guessed.

   Someone who is not a member of the guild gets 403 **before** any DB lookup
   (avoids leaking “is the bot here?”), and a member without access gets the
   same 403 whether or not the bot is there.
5. **Who has access** (`RequireManager`): only a manager may open or save
   `/servers/{guild_id}/access`. Saving goes through `save_module`, so every
   change is in the audit log with who made it. The General form never changes
   the list.
6. Guild list cached up to 5 minutes; role/channel lists for forms cached ~60s
   (refresh button clears them).
   If Discord can't be reached, the cached guild list is used for up to 30
   minutes, then guild pages answer 503.
7. **Powerful roles** (`forms.powerful_role_errors`): Manage Server is enough to
   use the dashboard, but only the owner or an Administrator may add a role with
   moderation/management permissions (`core/permissions.ELEVATED_PERMISSIONS`) as
   an accept, pending or extra role. In Discord, Manage Server alone can't assign
   roles, so the bot must not become a way around that. A dashboard access role
   counts as powerful too: otherwise someone let in by a role could have the bot
   hand the same access to every accepted member.

## Data storage

| Table | Purpose |
|---|---|
| `guilds` | Servers JoltBot has joined; `left_at` + 30-day purge |
| `module_settings` | Per-guild JSON config + `enabled` + optimistic `version` |
| `tickets` | Application lifecycle |
| `alliance_requests` | Alliance join requests and their decisions |
| `modlog_entries` | What the mod log recorded: event, who did it, to whom, details |
| `panel_messages` | Last posted panel message per module |
| `audit_log` | Who changed settings |
| `guild_issues` | Bot problems with no interaction to reply on |
| `web_sessions` | Dashboard logins |
| `bot_meta` | Slash sync hash, migration flags, bot heartbeat |

Schema is owned by Alembic (`migrations/`). Startup refuses to run if the DB
version is not at head (`core/schema.py`). SQLite uses WAL and a 5s busy
timeout so bot and web can write concurrently.

Guild-owned rows use `ON DELETE CASCADE` when a left guild is purged.

## External services

- **Discord Gateway** (bot): privileged intents — Server Members, Message Content.
- **Discord REST** (dashboard via bot token): list guild roles/channels for forms.
- **Discord OAuth2** (dashboard): user login and guild permission checks.
- No other third-party APIs in the core path. Optional Caddy terminates HTTPS
  in Docker Compose (`--profile proxy`).

## Reliability patterns

- Bot process retry loop with exit codes for permanent failures.
- Operator alerts (`core/alerts.py`): with `ALERT_WEBHOOK_URL` set, ERROR-level
  log lines from either process go to a Discord webhook on a background thread,
  de-duplicated and rate-limited.
- Ticket recovery loop + claim/release.
- Issues table for admin-visible problems. `JoltBot.report_issue` also posts each
  *new* issue once to the guild's notices channel (`general.notice_channel_id`),
  if an admin picked one. Repeats only bump the count. If that channel is unusable,
  the issue `general.notice_channel` is recorded for the dashboard and never posted.
- Bot heartbeat (`core/health.py`, `bot/heartbeat.py`): written to `bot_meta`
  every minute while the gateway connection is up; stale after 3 minutes. Read by
  `/health/bot`, the overview banner and the bot's Docker health check
  (`python -m joltbot.bot.healthcheck`). `/health` stays independent of the bot.
  The dashboard process also watches it (`web/watchdog.py`) and logs one ERROR,
  which becomes an operator alert, when the bot has been gone for 5 minutes.
- Optimistic concurrency on settings saves (`StaleSettingsError`).
- Backups (`scripts/backup.py`, `deploy/backup.sh`): SQLite online backup, with
  login sessions removed from the copy and an integrity check. `deploy/update.sh`
  takes one before every update and stops the bot before migrations run.
- Foreign role/channel IDs from other guilds rejected on ticket settings save.
