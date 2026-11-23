# JoltBot audit

Audited commit: `3129da6` (`main`, 2026-11-23). Branch: `refactor/architecture`.

This is Phase 0 of the refactor brief: a read-only audit. Nothing outside `docs/refactor/` was changed. The plan that follows from it is in [PLAN.md](PLAN.md).

## 1. Summary

The brief describes a codebase that needs layering, typed settings, per-guild config, tests, CI and a deployment. `main` already has all of these. The audit is therefore a gap analysis: what the code already guarantees, what it does not, and what is worth changing.

**Already in place**

- No hardcoded guild, channel or role IDs. Everything server-specific is a row in `module_settings`.
- Typed settings (`AppConfig`), validated per process at startup, with a complete `.env.example`.
- Every ticket and alliance decision goes through a conditional `UPDATE ... WHERE status = ...`, so two moderators clicking at once cannot both win.
- Persistent views are re-registered on startup, including the old bot's four `custom_id`s.
- All application SQL uses bound parameters.
- Every dashboard route that changes state requires login, a per-server authorization check and a CSRF token.
- No secret is committed, in the working tree or anywhere in git history. Nothing needs rotating.
- CI runs lint, format check, tests and a Docker build with a health check. The Lightsail deployment is live.

**What is missing or wrong**

| # | Finding | Kind | Section |
|---|---|---|---|
| 1 | A database error's text includes the values of the failed statement. For a failed login insert that is the user's Discord access token, and ERROR lines also go to the alert webhook. Reproduced. | Security, medium | 7 |
| 2 | No type checker anywhere: not in dev requirements, `pyproject.toml` or CI. | Tooling gap | 4 |
| 3 | Adding a module means editing shared files (`bot/client.py`, `core/models.py`, `core/permissions.py`, `modules/registry.py`). The docs say one registry entry is enough. | Design | 5 |
| 4 | `core/` imports from `modules/`, and each service imports its own views. The dependency arrows point both ways. | Design | 5 |
| 5 | The four legacy `custom_id`s and the ticket views' registration have no test. They are the part of the contract most likely to break silently. | Test gap | 8 |
| 6 | Two components have no error reply, so a failure shows the user Discord's generic "interaction failed". | Bug, low | 6 |
| 7 | No decision records, no smoke-test checklist, and the architecture diagram is ASCII only. | Docs gap | 4 |

## 2. Scope and method

**Read in full by me:** `core/{config,db,logsetup,alerts}.py`, `modules/{base,registry}.py`, `bot/client.py`, tickets `{service,settings,forms,embeds,diagnostics,transcript}.py`, `tickets/views/{staff,common}.py`, most of `tickets/cog.py`, alliances `{settings,context,forms,embeds}.py`, `web/{app,sessions}.py`, `web/routes/auth.py`, `web/static/app.js`, the base, form and three settings templates, `pyproject.toml`, `ci.yml`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, README lines 1-94.

**Covered by three read-only sweeps** (bot and modules; storage, config and secrets; dashboard, tests and deploy), each reporting `file:line`. I re-opened the cited lines for every finding in sections 5 to 8 unless section 10 says otherwise.

**Baseline checks**, run on this branch with the project's `.venv`:

```
$ ruff check .
All checks passed!
$ ruff format --check .
116 files already formatted
$ pytest -q
434 passed, 1 skipped in 23.45s
```

The skip is `tests/test_bot_startup.py:75` (SIGTERM handling; Windows event loops have no signal handlers). CI runs on Linux, where it is not skipped.

mypy is not installed in the `.venv`, so there is no trial type-check run and no count of type errors yet.

## 3. Behavior inventory

This is the contract. A refactor step that changes anything in this section needs explicit approval.

### 3.1 Slash commands

Six commands in two groups. No prefix commands, no context menus (`bot/client.py`: `command_prefix=commands.when_mentioned`, `help_command=None`).

Both groups carry `@app_commands.guild_only()` and `@app_commands.default_permissions(manage_guild=True)` (`tickets/cog.py:147-151`, `alliances/cog.py:60-64`). Discord's default visibility is not the access check; each command has its own.

| Command | Defined | Parameters | Runtime check |
|---|---|---|---|
| `/tickets panel` | `tickets/cog.py:315` | none | `_setup_check` (`cog.py:45-66`): Manage Server, or staff per `checks.can_run_setup_commands` |
| `/tickets diagnostics` | `tickets/cog.py:423` | none | `_setup_check` |
| `/tickets grant-existing` | `tickets/cog.py:453` | none | `_manage_server_check` (`cog.py:69-86`): Manage Server only |
| `/alliances panel` | `alliances/cog.py:112` | none | `_manage_check` (`cog.py:36-57`): Manage Server, or an override role |
| `/alliances pending` | `alliances/cog.py:193` | none | `_manage_check` |
| `/alliances cancel` | `alliances/cog.py:233` | `member: discord.User`, required, "Whose pending request to cancel" | `_manage_check` |

Command descriptions, as registered with Discord:

- `tickets`: "Ticket and verification tools"
- `tickets panel`: "Post (or re-post) the application panel in the panel channel."
- `tickets diagnostics`: "Check JoltBot's permissions, role order and ticket setup."
- `tickets grant-existing`: "One time: give the accepted-member role(s) to everyone already here."
- `alliances`: "Alliance join request tools"
- `alliances panel`: "Post (or re-post) the alliance menu in the panel channel."
- `alliances pending`: "List the alliance join requests still waiting for a decision."
- `alliances cancel`: "Cancel a member's pending alliance join request."

"Staff" means a configured staff role, or Administrator while `allow_administrators` is on (`tickets/checks.py:13-29`).

Commands are registered globally, and only when a hash of the command payload changes (`bot/client.py:153`). Changing a name, description or parameter changes the hash and triggers a re-sync, so a refactor that leaves the hash unchanged has provably not changed the command surface.

### 3.2 Event listeners

| Event | Handler | What it does |
|---|---|---|
| `on_ready` | `bot/guild_sync.py:42` | Syncs the `guilds` table with the servers the bot is in |
| `on_ready` | `tickets/cog.py:205` | Once per process: gives the pending role to members who joined while offline, where the server enabled it |
| `on_guild_join` | `bot/guild_sync.py:62` | Creates the guild row and its module rows |
| `on_guild_remove` | `bot/guild_sync.py:75` | Marks the guild as left (purged after 30 days) |
| `on_guild_update` | `bot/guild_sync.py:89` | Stores a new name or icon |
| `on_connect`, `on_resumed`, `on_disconnect` | `bot/heartbeat.py:43,48,53` | Track whether the gateway is connected |
| `on_member_join` | `tickets/cog.py:250` | Gives the pending role, or records an issue |
| `on_raw_member_remove` | `tickets/cog.py:293` | Closes the member's open ticket as "left" |
| `on_raw_member_remove` | `alliances/cog.py:99` | Closes the member's pending alliance request |
| `on_guild_channel_delete` | `tickets/cog.py:305` | Closes or tidies the ticket whose channel was deleted |

Lifecycle hooks: `JoltBot.setup_hook` (`bot/client.py:104`), `JoltBot.close` (`:302`), `Tree.on_error` (`:44`), `Tickets.cog_load` (`tickets/cog.py:169`), `Alliances.cog_load` (`alliances/cog.py:79`).

`on_ready` and `on_raw_member_remove` each have two handlers in different cogs. Both must keep firing.

### 3.3 Components

**Persistent (survive restarts). These strings are frozen.**

| `custom_id` | Component | Defined | Registered at startup |
|---|---|---|---|
| `joltbot:tickets:open` | Panel button, label from settings (default "Open Application") | `tickets/views/common.py:11` | `tickets/cog.py:180` |
| `joltbot:tickets:accept` | Ticket: Accept | `common.py:12` | `tickets/cog.py:181` |
| `joltbot:tickets:deny` | Ticket: Deny | `common.py:12` | `tickets/cog.py:181` |
| `joltbot:tickets:close` | Ticket: Close | `common.py:12` | `tickets/cog.py:181` |
| `rokverify:open_application` | Legacy panel button | `common.py:15` | `tickets/cog.py:180` (`legacy=True`) |
| `rokticket:accept` | Legacy ticket: Accept | `common.py:16` | `tickets/cog.py:181` (`legacy=True`) |
| `rokticket:deny` | Legacy ticket: Deny | `common.py:17` | `tickets/cog.py:181` (`legacy=True`) |
| `rokticket:close` | Legacy ticket: Close | `common.py:18` | `tickets/cog.py:181` (`legacy=True`) |
| `joltbot:alliances:choose` | Alliance menu (select, placeholder "Choose your alliance") | `alliances/views.py:33` | `alliances/cog.py:93` |
| `joltbot:alliances:accept` | Join request: Accept | `alliances/views.py:34` | `alliances/cog.py:94` |
| `joltbot:alliances:reject` | Join request: Reject | `alliances/views.py:34` | `alliances/cog.py:94` |

That is 7 current and 4 legacy IDs. All use `timeout=None` and are registered with `bot.add_view` inside `cog_load`. There is no `DynamicItem`: a click finds its ticket by channel ID and its alliance request by message ID.

Consequence worth knowing: registration happens in `cog_load`, so if a module's cog fails to load at startup, its buttons on old messages stop responding until the bot restarts with the fault fixed.

**Short-lived (lost on restart by design). No explicit `custom_id`; discord.py generates one.**

| Component | Defined | Timeout |
|---|---|---|
| `GrantConfirmView` ("Give them the role(s)", "Cancel") | `tickets/cog.py:115` | 120 s |
| `ChoiceStepView` (one select per dropdown question, "Continue") | `tickets/views/applicant.py:133` | 300 s |
| `ApplicationModal` (up to 5 text inputs from settings) | `tickets/views/applicant.py:39` | 900 s |
| `AcceptView` ("Extra role (optional)" select, "Confirm accept", "Cancel") | `tickets/views/staff.py:224` | 300 s |
| `DenyReasonModal` ("Deny application"; "Reason (sent to the applicant)", max 1000) | `tickets/views/staff.py:275` | 600 s |
| `DenyActionView` ("Deny, don't kick", "Deny and kick", "Cancel") | `tickets/views/staff.py:311` | 300 s |
| `CloseConfirmView` ("Yes, close it", "Cancel") | `tickets/views/staff.py:352` | 300 s |
| `JoinRequestModal` ("Join {alliance}"; "In-game name", "Governor ID") | `alliances/views.py:52` | 900 s |
| `RejectReasonModal` ("Reject request"; optional reason, max 500) | `alliances/views.py:195` | 600 s |

Every member-facing text of the ticket module is per-server data with defaults in `TicketMessages` (`tickets/settings.py:148-188`). The fixed strings (for example "Someone else already handled this ticket.", `tickets/service.py:59`) live next to the code that sends them.

### 3.4 Background tasks

| Task | Where | Interval |
|---|---|---|
| Slash-command sync with retry | `bot/client.py:132` | Once after ready; on failure retries from 30 s, doubling to 900 s |
| Heartbeat write to `bot_meta` | `bot/heartbeat.py:57` | 60 s, only while connected |
| Purge of guilds left more than 30 days ago | `bot/guild_sync.py:100` | 24 h |
| Ticket recovery | `tickets/cog.py:190` | 5 min, first run after ready |
| Per-ticket finalize, log retry, channel delete | `tickets/service.py:84` (`_spawn`) | On demand; sleeps `delete_delay_seconds` before deleting the channel |
| Process restart loop | `bot/__main__.py:116` | Backoff 10 s to 300 s; exit codes 2 and 3 stop it |
| SIGTERM handler | `bot/__main__.py:71` | Closes the bot cleanly |
| Bot watchdog (dashboard process) | `web/watchdog.py`, started `web/app.py:107` | 60 s; one ERROR after the bot has been gone 5 min |
| Alert sender thread | `core/alerts.py:228` | Queue-driven; de-duplicated and rate-limited |

### 3.5 Storage

Engine: SQLite through `aiosqlite`, WAL mode, foreign keys on, 5 s busy timeout (`core/db.py:23-35`). The bot and the dashboard open the same file. Schema is owned by Alembic (`migrations/versions/0001`, `0002`); both processes refuse to start if the database is not at head. A test (`tests/test_migrations.py`) fails if models and migrations disagree.

| Table | Model | Purpose | Notable constraints |
|---|---|---|---|
| `guilds` | `core/models.py:82` | Servers the bot has joined | PK `id` |
| `module_settings` | `:98` | Per-guild JSON config, `enabled`, `version` | PK `(guild_id, module)`; FK cascade |
| `tickets` | `:115` | Application lifecycle | Unique `channel_id`; partial unique index: one `open`/`closing` ticket per `(guild_id, user_id)` (`:147`) |
| `alliance_requests` | `:164` | Alliance join requests | Unique `message_id`; partial unique index: one `pending`/`deciding` request per `(guild_id, user_id)` (`:198`) |
| `panel_messages` | `:209` | Last posted panel per module | PK `(guild_id, module)` |
| `audit_log` | `:220` | Who changed which setting | Index `(guild_id, created_at)` |
| `guild_issues` | `:238` | Problems shown on the dashboard | Unique `(guild_id, key)` |
| `web_sessions` | `:255` | Dashboard logins | PK is an HMAC of the cookie token |
| `bot_meta` | `:286` | Command hash, legacy-command flag, heartbeat | PK `key` |

All guild-owned tables cascade on guild delete. Other storage: rotating log files in `LOG_DIR`, and backups written by `scripts/backup.py` with `web_sessions` removed. No JSON files or pickles are written at runtime.

### 3.6 Environment variables

All read through `AppConfig` (`core/config.py:19`). No code calls `os.environ` or `os.getenv`.

| Variable | Default | Required by | Validation |
|---|---|---|---|
| `DISCORD_TOKEN` | empty | bot, dashboard | non-empty at startup |
| `DISCORD_CLIENT_ID` | empty | dashboard | non-empty |
| `DISCORD_CLIENT_SECRET` | empty | dashboard | non-empty |
| `SESSION_SECRET` | empty | dashboard | non-empty |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | no | trailing slash stripped; `https://` turns on secure cookies and HSTS |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/joltbot.db` | no | parsed by SQLAlchemy |
| `HOST` | `127.0.0.1` | no | none |
| `PORT` | `8000` | no | integer 1 to 65535 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | no | passed to uvicorn |
| `LOG_LEVEL` | `INFO` | no | one of five levels, any case |
| `LOG_DIR` | `logs` | no | empty means console only |
| `LOG_FORMAT` | `text` | no | `text` or `json`, any case |
| `ALERT_WEBHOOK_URL` | empty | no | must start with `https://`, otherwise alerts are off with a warning |

Read by shell and Compose only: `DOMAIN`, `BACKUP_DIR`, `KEEP_DAYS`. `.env.example` lists all 13 application variables plus `DOMAIN`.

### 3.7 Hardcoded IDs

None in runtime code. Pattern searched: `\b[0-9]{17,20}\b` over the repository, excluding tests, lock files and the virtual environment.

- `web/templates/landing.html:59`: a made-up ID shown as text in a marketing mock-up.
- `scripts/preview_site.py:47`: made-up IDs for the offline site preview.
- `core/permissions.py:50`: a comment giving the permission bitfield value (`2251800082377746`). A permission value, not an ID.

No role, channel or guild is looked up by a hardcoded name either.

### 3.8 Dashboard routes

| Method and path | Handler | Login | Per-server check | CSRF |
|---|---|---|---|---|
| GET, HEAD `/health` | `web/routes/health.py:29` | no | n/a | n/a |
| GET, HEAD `/health/bot` | `web/routes/health.py:41` | no | n/a | n/a |
| GET `/` | `web/routes/pages.py:20` | no | n/a | n/a |
| GET `/servers` | `web/routes/pages.py:26` | yes | lists only the user's own servers | n/a |
| GET `/login` | `web/routes/auth.py:34` | no | n/a | OAuth `state` cookie |
| GET `/auth/callback` | `web/routes/auth.py:68` | no | n/a | `state` compared in constant time (`:88`) |
| POST `/logout` | `web/routes/auth.py:124` | yes | n/a | yes |
| GET `/servers/{guild_id}` | `web/routes/guild.py:62` | yes | `RequireGuild` | n/a |
| GET `/servers/{guild_id}/audit` | `web/routes/guild.py:118` | yes | `RequireGuild` | n/a |
| POST `/servers/{guild_id}/refresh` | `web/routes/guild.py:175` | yes | `RequireGuild` | yes |
| POST `/servers/{guild_id}/issues/{issue_id}/dismiss` | `web/routes/guild.py:196` | yes | `RequireGuild`; delete also filters on `guild_id` | yes |
| GET, POST `/servers/{guild_id}/access` | `web/routes/guild.py:273,332` | yes | `RequireManager` | yes on POST |
| GET, POST `/servers/{guild_id}/general` | `modules/general/web.py:73,154` | yes | `RequireGuild` | yes on POST |
| GET, POST `/servers/{guild_id}/tickets` | `modules/tickets/web.py:79,110` | yes | `RequireGuild` | yes on POST |
| GET, POST `/servers/{guild_id}/alliances` | `modules/alliances/web.py:76,108` | yes | `RequireGuild` | yes on POST |

`RequireGuild` is `web/security.py:223`: logged in, a member of that server according to Discord, and either a manager (owner, Administrator, Manage Server) or the holder of a dashboard access role checked live against Discord. If Discord cannot be asked, the answer is 503, never a guess (`:257-263`).

### 3.9 Mod log (uncommitted work on `feat/modlog`, not live)

Not part of the audited commit. Listed so the plan can account for it.

- Adds 22 listeners in `modules/modlog/cog.py`, no commands, no components, one table (`modlog_entries`, migration `0003`).
- Adds a third handler for `on_raw_member_remove`, and second handlers for `on_member_join`, `on_guild_channel_delete` and `on_guild_update`.
- Adds View Audit Log to the bot's invite permissions, so the invite value changes and servers already using JoltBot must grant it by hand.
- Raises discord.py's message cache from 1000 to 5000 messages.
- Notes for when it lands (these are in-progress gaps, not bugs in live code):
  - `retention_days` describes a daily cleanup (`modlog/settings.py:33`), but nothing deletes `modlog_entries` rows.
  - The module has no dashboard page, while `setup_problems` requires a log channel before it can be switched on.

## 4. The brief's goals against the code

| Goal | State | Evidence |
|---|---|---|
| Consistent error handling | Mostly | `Tree.on_error` for slash commands (`bot/client.py:44`); `on_error` on 11 of 13 component classes. Two missing (section 6). |
| Structured logging | Partly | JSON format exists (`core/logsetup.py:43`). Only two call sites pass structured fields; guild IDs are otherwise inside the message text. No process, user or interaction field. |
| Views survive restarts | Yes | Section 3.3. Untested for tickets (section 8). |
| No double-processing | Yes | `tickets/store.py:220` (`claim`), `alliances/store.py:158,212` (`claim`, `decide`), partial unique indexes. Every way out of `open` or `pending` goes through one of these. |
| New feature = new module | Partly | The registry works for cog, routes and templates. Tables, bot permissions, intents and the service handle still live in shared files (section 5). |
| No hardcoded IDs | Yes | Section 3.7. |
| Per-guild config in existing storage | Yes | `module_settings`; no schema change needed. |
| Thin Discord layer | Mostly | Views and cogs call services. Exceptions in section 5. |
| Service layer light on discord | No, by design | All three services import `discord`; they orchestrate Discord calls. Pure rules already sit in `checks.py`, `settings.py`, `forms.py`. PLAN.md argues for keeping this. |
| Data-access layer | Mostly | `store.py` per module, `core/` for shared tables. Leaks in section 5. |
| Typed settings, `.env.example` | Yes | Section 3.6. |
| Explicit dependencies | Mostly | Config and database are passed in. `get_config()` is a cached module-level singleton used at entry points. |
| Lint, type check, tests | Lint and tests only | Ruff and pytest. No type checker. |
| README, architecture doc, diagram, decision records, CI | Partly | README (970 lines), `docs/ARCHITECTURE.md` with ASCII diagrams, CI. No decision records. |
| Smoke-test checklist | No | None in the repo. |
| Dockerfile, compose, restart policy, health check, deploy guide | Yes | Live. Out of scope until phases 1 to 6 are approved. |

## 5. Design problems

| # | Where | Problem | Principle |
|---|---|---|---|
| D1 | `bot/client.py:77-78` | Each module's service is a typed attribute on the bot class (`tickets`, `alliances`). Every new module with a service edits this file. | Open/closed |
| D2 | `core/models.py:115,164` | Module-owned tables (`tickets`, `alliance_requests`) are declared in `core/`. A new table means editing a shared file. | Separation of concerns |
| D3 | `core/permissions.py:37` | The invite permission list is one hand-kept dict. A module that needs a new permission edits it. | Open/closed |
| D4 | `core/guild_settings.py:24-25`, `core/guilds.py:11` | `core/` imports `modules.registry`, which imports every module's settings. The shared layer depends on the feature layer. | Dependency direction |
| D5 | `tickets/service.py:50`, `alliances/service.py:41` | Services import their own views. The alliances service imports message constants from `views.py`. | Dependency direction |
| D6 | `tickets/cog.py:16,392,415` | The cog queries `PanelMessage` with SQLAlchemy directly. Alliances does the same job through `store.get_panel` / `set_panel`. | Layering; duplication |
| D7 | `alliances/cog.py:17-18`, `alliances/service.py:26-27`, `alliances/embeds.py:18`, `alliances/settings.py:17`, `alliances/web.py:22` | Alliances imports helpers that live in the tickets module (`hierarchy_problems`, `roles_by_id`, `ts`, `clip`, `AssignableRole`, `form_options`). Removing tickets would break alliances. | Coupling |
| D8 | `web/routes/guild.py:130,216`, `web/routes/pages.py:47`, `web/security.py:212,253` | Route handlers build queries inline. | Layering |
| D9 | `tickets/forms.py:73-84` and `alliances/forms.py:39-50`; `powerful_role_errors` at `tickets/forms.py:285` and `alliances/forms.py:150` | `_text`, `_ids` and the powerful-role rule are written twice. | Duplication |
| D10 | `bot/__main__.py:86,89` | `run_once` calls `get_config()` twice instead of receiving the config `main` already loaded. | Explicit dependencies |
| D11 | `tickets/views/staff.py:108`, `alliances/views.py:219,259` | Views read the store directly for a pre-check. Read-only and backed by the real guard in the service, so low priority. | Layering |
| D12 | `web/sessions.py` | The session store lives in `web/`, not `core/`. Only the dashboard uses it, so this is a note, not a recommendation. | Layering |

## 6. Bug register

Recorded, not fixed. Each fix would be its own commit after approval.

| # | Where | What happens | Severity |
|---|---|---|---|
| B1 | `tickets/cog.py:115` (`GrantConfirmView`), `tickets/views/staff.py:275` (`DenyReasonModal`) | No `on_error`. If the handler raises, discord.py logs it and the user sees only "This interaction failed". Every other component replies with a message. | Low |
| B2 | `tickets/cog.py:490` against `:543` | `grant-existing` tests the lock with `.locked()` but takes it only after the confirm click. Two admins can both get a confirm prompt; the second grant then runs after the first with an out-of-date member list. Harmless in effect (giving a role twice does nothing) but the "This is already running." message is not reliable. | Low |
| B3 | `tickets/web.py:153-178`, `alliances/web.py:152-167` | The powerful-role check reads the dashboard access roles in one session and saves in another. A change to the access roles between the two is not detected, because the version check covers only that module's own row. Needs two people saving within the same moment. | Low |
| B4 | `web/routes/auth.py:115` against `web/sessions.py:84` | The cookie always lives 7 days, while the server-side session can expire sooner (it follows Discord's token lifetime). The user is simply sent to log in again. | Cosmetic |
| B5 | `web/sessions.py:71` | Expired session rows are deleted only when somebody logs in. With few logins, expired rows (and their tokens) stay in the table. | Low |
| B6 | `tickets/views/staff.py:133` | Existing TODO: `require_attachment` only warns on Accept. The dashboard label says "Warn staff", so behavior and label agree; the setting name does not. | Naming |
| B7 | `web/form_helpers.py` (`form_version`) | Found by mypy in Phase 1. If a settings form is posted with a file uploaded under the name `version`, `int()` raises `TypeError`, which the `except ValueError` does not catch, so the page answers 500 instead of "someone else saved". Only a logged-in dashboard user with a valid CSRF token can send such a form, and only to their own server. | Low |

## 7. Security findings

No finding lets someone act without authorization. They are ordered by how much they are worth fixing.

**S1. Database errors carry statement values into logs and the alert webhook. Medium.**
`core/db.py:55` creates the engine without `hide_parameters=True`. SQLAlchemy then appends the bound values to every database error message. `web/sessions.py:72-86` inserts the user's Discord access token and CSRF token at login. If that insert fails (for example "database is locked" after the 5 s busy timeout), the unhandled exception is logged at ERROR with the values in it, written to the log file, and forwarded to `ALERT_WEBHOOK_URL` (`core/alerts.py`).

Reproduced with a throwaway script against this commit: an `IntegrityError` on `web_sessions` contained the marker token in `str(error)` and in the formatted JSON log line. Exposure is limited: the token has `identify guilds` scope and expires within 7 days. The fix is one argument and changes log text only.

**S2. Discord access tokens are stored in plain text. Low to medium.**
`core/models.py:265-268`, an existing TODO. Anyone who can read the live database can read users' server lists until the tokens expire. Mitigations already present: backups delete `web_sessions`; the cookie token itself is stored only as an HMAC. Encrypting at rest needs an authenticated cipher, which the standard library does not have, so it requires a runtime dependency. PLAN.md lists it as a decision.

**S3. No request rate limiting on the dashboard. Low.**
Neither the app nor `deploy/Caddyfile` throttles `/login`, `/auth/callback` or the POST routes. `/auth/callback` rejects a bad `state` before calling Discord, which removes the cheapest abuse.

**S4. CI actions and base images are pinned by tag. Low.**
`actions/checkout@v5`, `actions/setup-python@v6` (`.github/workflows/ci.yml:15-16`), `python:3.12-slim`, `caddy:2.8.4-alpine`. Python dependencies, the Tailwind binary and htmx are already verified by checksum.

**S5. `SESSION_SECRET` is only checked for being non-empty. Informational.**
The placeholder `change-me` would be accepted. Impact is small: the secret keys an HMAC over a 256-bit random token, so a weak secret does not make sessions guessable.

**S6. All ERROR text leaves the host through the alert webhook. Informational.**
By design. The URL must be `https://` but its host is not checked to be Discord. Fixing S1 removes the main reason this matters.

**S7. Smaller notes.**
- `docker-compose.yml:72` sets `FORWARDED_ALLOW_IPS: "*"`. Safe only because the port is published on `127.0.0.1`; worth a comment so nobody widens the port binding.
- `web/app.py:52,57`: the CSP allows `fonts.googleapis.com` and `fonts.gstatic.com`, but both fonts are self-hosted (`web/templates/base.html:26-27`). The allowance can go.
- `modules/general/templates/general/settings.html:31`: after a failed save, the raw `embed_color` form value is echoed into a `style` attribute. Autoescape prevents breaking out of the attribute and it is only shown to the person who typed it.
- `web/templates/base.html:33`: the development-only Tailwind CDN script has no integrity hash. Not served in production builds.
- No Permissions-Policy header; HSTS has no `includeSubDomains`.
- Local machine, not the repo: `data/joltbot.before-0003.bak.db` is a manual copy of the test database made outside `scripts/backup.py`, so it still holds session rows.

**Checked and found sound**

- Secrets: none committed. Searched the tree and all history for Discord token shapes, webhook URLs, client secrets, private keys and credentialed URLs. `.env` is ignored and was never committed.
- SQL: no string-built SQL in `joltbot/`, `scripts/` or `migrations/`. The few `text()` calls are constants.
- Admin commands: all six have a runtime permission check (section 3.1).
- Dashboard: every state-changing route has login, server authorization and CSRF (section 3.8). CSRF compares in constant time and checks `Origin` (`web/security.py:296-320`). The session cookie is HttpOnly, SameSite=Lax, Secure and `__Host-` prefixed on HTTPS. A new login always gets a new token.
- Open redirect: the only return parameter must start with `/servers/{guild_id}` and contain no `//` (`web/routes/guild.py:192`).
- Templates: autoescape is on; the single `|safe` renders a constant icon table (`web/templates/_icons.html:42`). The live preview escapes before writing `innerHTML` (`web/static/app.js:34-38,69-71`).
- Cross-server safety: role and channel IDs from another server are rejected on save (`tickets/forms.py:211`, `alliances/forms.py:113`); a ticket row is never acted on from another guild (`tickets/views/staff.py:110`).
- Privilege escalation through the bot: only the owner or an Administrator can make JoltBot hand out a role with elevated permissions or dashboard access (`tickets/forms.py:285`, `alliances/forms.py:150`).

## 8. Test gaps

434 tests cover the ticket and alliance lifecycles, recovery, settings validation, dashboard access and deploy plumbing. What they do not pin:

| # | Gap | Why it matters |
|---|---|---|
| T1 | Ticket persistent views and the four legacy `custom_id`s. Only alliances asserts `bot.persistent_views` (`tests/test_alliance_flow.py:781`). | A rename would pass CI and break every posted panel and ticket. |
| T2 | The command tree (names, parameters, descriptions, default permissions) is not snapshotted. | Same: the contract in 3.1 is only enforced by review. |
| T3 | Two moderators clicking at once is tested at store level only (`tests/test_ticket_recovery.py:42-48`); alliances has a sequential "second reviewer" test. Nothing concurrent goes through the views. | The guard is in the store, but the user-visible result is produced by the view. |
| T4 | Component `on_error` handlers. Only `Tree.on_error` is tested. | B1 went unnoticed. |
| T5 | CSRF negatives exist only for POST `/tickets`; cross-server POST only for tickets. | All routes share one dependency, so a parametrized test over every POST is cheap. |
| T6 | OAuth scope refusal, the `error=` parameter and Discord failing during the callback. | Login failure paths. |
| T7 | Security headers, apart from `frame-ancestors`. | Regression guard. |

## 9. Documentation drift

- `docs/ARCHITECTURE.md:84-86` and the header of `modules/registry.py` say a new module needs "one `ModuleInfo` entry. No other wiring." Tables, permissions and the service handle say otherwise (D1 to D3).
- `docs/ARCHITECTURE.md:33`: "`joltbot/core/` is shared code with no discord.py or FastAPI imports." True, but it does import the modules package (D4).
- `DEPLOYMENT.md:29`: "every container has `restart: unless-stopped`". The `migrate` service is `restart: "no"`, correctly.
- `README.md:431`: the bot is "unhealthy once it has been disconnected for three minutes". The heartbeat goes stale at 180 s, and Docker then needs two failed checks 60 s apart, so it is closer to five.
- `deploy/backup.sh:9`, `deploy/systemd/*.service` and the README's deploy sections use `botuser` paths; the live server runs as `ubuntu`. `README.md:594` explains the substitution, so this is a note, not an error.

## 10. Not verified

- **Files I did not read in full.** README lines 95-970 beyond a path and command check; `DEPLOYMENT.md`; the landing, servers, overview, access and audit templates (searched for `|safe`, inline scripts and unescaped attribute values only); the uncommitted mod log files `diffs.py`, `embeds.py`, `events.py`, `rules.py`; test bodies.
- **Read by a sweep, cited lines not all re-opened by me.** Alliances `service.py`, `cog.py`, `views.py`, `store.py`; tickets `store.py`, `views/applicant.py`, `channels.py`, `checks.py`, `log_post.py`; `bot/{guild_sync,heartbeat,healthcheck}.py`; `core/{guild_settings,guilds,health,issues,schema,discord_rest}.py`; `web/routes/{guild,pages,health}.py`; `scripts/`; `Dockerfile`, `docker-compose.yml`, `deploy/`. Inventory rows that cite these files come from the sweeps.
- **S1** was reproduced for an `IntegrityError`. I did not provoke a real "database is locked" at login; SQLAlchemy formats both error types the same way.
- **The SQLite database files** were not opened.
- **Type errors**: unknown until mypy is installed (PLAN.md, Phase 1).
- **Live behavior on Discord**: nothing here was exercised against Discord. The audit is from code and tests.
