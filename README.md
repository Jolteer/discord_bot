# RoK Verification Ticket Bot

New members land in **#verify** with only the **Unverified** role. They click **Open Application**, fill in their governor details, and get a private ticket. Only the **King, R4s and Mods** can Accept, Deny or Close it. Every ticket's transcript, screenshots and decision are saved to **#ticket-logs**.

*Prepared by jolteer*

---

## Contents

1. [What it does](#1-what-it-does)
2. [Create the bot in the Developer Portal](#2-create-the-bot-in-the-discord-developer-portal)
3. [Invite it to your server](#3-invite-it-to-your-server)
4. [Roles, role order and channel permissions](#4-roles-role-order-and-channel-permissions)
5. [Install, configure and go live (in this order)](#5-install-configure-and-go-live-in-this-order)
6. [Host it 24/7](#6-host-it-247)
7. [Troubleshooting](#7-troubleshooting)
8. [Project layout](#8-project-layout)

---

## 1. What it does

| Step | What happens |
|---|---|
| Member joins | Bot gives **Unverified**. They can only see #verify (and an optional #welcome). |
| Clicks **📝 Open Application** | A form asks for governor name, governor ID, power, alliance, and anything else. |
| Submits | A private `ticket-<username>` channel opens under **Tickets**. Only the applicant, King, R4, Mod and the bot can see it. Staff are pinged (configurable). The applicant is asked to upload a governor-profile screenshot. |
| **✅ Accept** | Staff can pick an alliance role (optional), then confirm. The bot adds **Member** (+ alliance), removes **Unverified**, DMs a welcome, and closes the ticket. Staff see a warning if no screenshot was uploaded. |
| **❌ Deny** | Staff type a reason, then choose **Keep Unverified** or **Kick**. The applicant is DM'd the reason *before* any kick. |
| **🔒 Close** | Closes without a decision (asks to confirm). |
| Anyone else clicks a staff button | Ephemeral reply: *"Only King, R4s, and Mods can do this."* |
| Ticket closes | A `.txt` transcript and a summary embed (who decided, when, alliance role, reason) go to #ticket-logs. Screenshots are re-uploaded there too, because Discord deletes attachments when a channel is deleted. The ticket channel is then deleted after a short delay. |

Also handled:

- **Restarts**: buttons keep working (persistent views), and open tickets live in SQLite. Anything interrupted mid-close is finished on the next start.
- **Duplicates**: one open ticket per person, enforced by the database, so double-clicks can't create two.
- **Two staff clicking at once**: only the first decision counts; the other person is told it was already handled.
- **Applicant leaves the server**: their ticket auto-closes and is logged.
- **Ticket channel deleted by hand**: it's logged as such.
- **Permission problems**: errors explain exactly what to fix. `/verify-diagnostics` checks everything at once.
- **Logging can't post**: the ticket channel is **not** deleted, so nothing is lost.

### Slash commands (staff only)

| Command | Purpose |
|---|---|
| `/setup-verify` | Posts (or re-posts) the verification panel in #verify and pins it. Replaces the old panel. |
| `/verify-diagnostics` | Checks permissions, role order, channel IDs, ping settings, and lists any channel Unverified members can see that they shouldn't. |
| `/migrate-existing-members` | One time only: gives **Member** to everyone already in the server, so switching verification on doesn't lock them out. |

---

## 2. Create the bot in the Discord Developer Portal

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it (e.g. *KoC Gatekeeper*).
2. Open the **Bot** tab:
   - Click **Reset Token**, copy it, and keep it secret. It goes in `.env` later. If it ever leaks, reset it again.
   - Turn **Public Bot** **off**, so only you can add it to servers.
   - Under **Privileged Gateway Intents**, turn **on**:
     - ✅ **Server Members Intent** (*required*: join/leave events and role assignment)
     - ✅ **Message Content Intent** (*required for transcripts*: without it, logged messages come out blank)
     - Presence Intent is not needed.
   - Click **Save Changes**.
3. Open **General Information** and copy the **Application ID**. You need it for the invite link.

---

## 3. Invite it to your server

Replace `YOUR_APPLICATION_ID` and open this link while logged in as someone with **Manage Server**:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=2251800082377746&scope=bot%20applications.commands
```

Both scopes matter: `bot` adds the bot, and `applications.commands` lets the slash commands appear.

The permission number grants exactly this (nothing more, and **not** Administrator):

| Permission | Why |
|---|---|
| View Channels, Send Messages, Embed Links, Read Message History | Basic operation |
| Attach Files | Transcripts and screenshot re-uploads in #ticket-logs |
| Manage Channels | Create and delete ticket channels |
| Manage Roles | Assign Member/Unverified/alliance roles; set private permissions on each ticket |
| Manage Messages | Moderate inside tickets |
| Pin Messages | Pin the verify panel (Discord split this off from Manage Messages) |
| Kick Members | Only for Deny → Kick |
| Mention @everyone, @here, and All Roles | Lets the new-ticket ping notify staff even if their role isn't set to "mentionable". It does **not** let the bot ping @everyone: the code blocks that. |

If you'd rather use the portal: **OAuth2 → URL Generator**, tick `bot` and `applications.commands`, then tick the permissions above.

---

## 4. Roles, role order and channel permissions

### 4a. Create the roles

Create these if they don't exist: **King, R4, Mod, Member, Unverified**, and any alliance roles (e.g. **Alliance A**, **Alliance B**). Unverified needs **no permissions at all**; leave every toggle off.

### 4b. Role order (the #1 cause of "it doesn't work")

Discord only lets a bot assign roles that sit **below its own highest role**. When the bot joins, Discord creates a role with the bot's name. In **Server Settings → Roles**, drag it so the list looks like this (top to bottom):

```
King
R4
Mod
KoC Gatekeeper   ← the bot's role: ABOVE everything it assigns
Alliance A
Alliance B
Member
Unverified
@everyone
```

The bot only needs to be above **Member, Unverified and the alliance roles**. Keeping it below King/R4/Mod also means it can never kick leadership.

### 4c. Make Unverified see only #verify

The reliable approach is **locked by default**: nobody sees anything unless a role grants it. Then Unverified, which grants nothing, only sees the channels where it's explicitly allowed. This also fails safe: if the bot is offline when someone joins, they see nothing at all instead of everything.

> ⚠️ **Do these steps in the order given in section 5.** Turning off @everyone's View Channels before existing members have the Member role locks everyone out.

**Server level** (Server Settings → Roles):

| Role | View Channels |
|---|---|
| @everyone | ❌ **off** |
| Member | ✅ on |
| King / R4 / Mod | ✅ on (or just make sure they also have Member) |
| Unverified | ❌ off (everything off) |

**Remove @everyone "allow" overrides.** A green ✅ for @everyone on any category or channel overrides the server setting and makes that channel visible to Unverified. Common culprits are rules, announcements and event channels. Open each category's **Edit Category → Permissions** and set @everyone's View Channel to **/** (neutral) or ❌. `/verify-diagnostics` lists every channel Unverified can still see, so you don't have to hunt.

**#verify** channel overrides:

| Who | View Channel | Read Message History | Send Messages | Add Reactions |
|---|---|---|---|---|
| Unverified | ✅ | ✅ | ❌ | ❌ |
| Member | ❌ (hides #verify once approved; optional) | | | |
| Bot role | ✅ | ✅ | ✅ (plus Embed Links, Pin Messages) | |

Applicants only need to click the button, so they don't need Send Messages there.

**#welcome** (optional): Unverified gets View ✅ and Send ❌. Put its ID in `channels.welcome` so diagnostics treats it as allowed.

**Tickets** category:

| Who | Permissions |
|---|---|
| @everyone | View Channel ❌ |
| Bot role | View Channel, Manage Channels, Manage Permissions, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages: all ✅ |

You do **not** need to add King/R4/Mod here. The bot gives each ticket its own private permissions for the applicant, the staff roles and itself. Discord only lets a bot grant permissions it has itself, which is why the bot role needs the full list above.

**#ticket-logs** channel:

| Who | Permissions |
|---|---|
| @everyone | View Channel ❌ |
| King / R4 / Mod | View Channel ✅, Read Message History ✅ (Send ❌ keeps the log clean) |
| Bot role | View Channel, Send Messages, Embed Links, Attach Files, Read Message History ✅ |

**Check your work:** Server Settings → Roles → **Unverified** → ⋯ → **View Server As Role**. You should see only #verify (and #welcome). Then run `/verify-diagnostics`.

> If your server uses Discord's **Community Onboarding**, it has its own requirements about default channels being visible to @everyone, which conflict with a locked-down server. If Discord won't let you save these settings, turn Onboarding off or adjust it.

---

## 5. Install, configure and go live (in this order)

### 5a. Get the IDs

Discord **User Settings → Advanced → Developer Mode: on**. Then right-click a server, role or channel → **Copy ID**. Role IDs: Server Settings → Roles → right-click the role.

### 5b. Install

Requires **Python 3.10+** (3.11 or 3.12 recommended).

```bash
cd rok-ticket-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then paste your token into .env
cp config.example.json config.json # then fill in the IDs
```

### 5c. config.json

| Key | Meaning |
|---|---|
| `guild_id` | Your server ID |
| `roles.approval` | Name → role ID for everyone who can Accept/Deny/Close. Add or remove entries freely (e.g. add `"R5"`). |
| `roles.unverified` / `roles.member` | Role IDs |
| `roles.alliances` | Dropdown options shown on Accept: `label`, `role_id`, optional `emoji` (a plain emoji like `"🛡️"`). Use `[]` for none. Max 24. |
| `channels.verify` / `channels.welcome` / `channels.ticket_logs` | Channel IDs (`welcome` may be `""`) |
| `channels.tickets_category` | The **category** ID |
| `ping_on_new_ticket.enabled` | `false` turns pings off |
| `ping_on_new_ticket.role_ids` | Leave `[]` to ping the approval roles, or list e.g. a `[Staff]` role ID. A custom ping role gets read access to tickets (so the ping actually notifies) but **no** button powers. |
| `tickets.delete_delay_seconds` | Wait before deleting a closed ticket (default 10) |
| `tickets.reupload_attachments` | Copy screenshots into #ticket-logs before deletion (default `true`; recommended) |
| `allow_administrators` | `true` lets anyone with Administrator use staff buttons. Default `false` (strictly King/R4/Mod). |
| `assign_unverified_on_startup` | `true` = on startup, give Unverified to anyone who joined while the bot was offline. Turn this on **after** the migration (step 5d). |
| `messages` | Optional text overrides. Keys: `not_staff`, `already_verified`, `panel_title`, `panel_description`, `ticket_intro`, `welcome_dm`, `deny_dm`, `deny_dm_kicked_suffix`, `deny_dm_kept_suffix`. Placeholders: `{guild}`, `{user}`, `{reason}`, `{alliance}`, `{alliance_line}`. |

IDs can be written as strings (`"123..."`) or numbers. If anything is wrong, the bot lists **every** problem at startup instead of crashing on the first one.

### 5d. Go live safely

1. **Start the bot:** `python run.py`. The console prints a diagnostics report. Fix any ❌.
2. **Run `/verify-diagnostics`** in Discord and fix anything it flags (role order especially).
3. **Run `/migrate-existing-members`.** Everyone currently in the server gets **Member**. Large servers take a few minutes because of Discord rate limits.
4. **Lock the server** (section 4c): give Member View Channels, turn @everyone's View Channels off, and remove @everyone allow overrides.
5. **Run `/setup-verify`** to post and pin the panel.
6. Set `"assign_unverified_on_startup": true` in config.json and restart the bot.
7. **Test with an alt account:** join, apply, upload a screenshot, then accept from your main. Check #ticket-logs.

**Optional:** hide the staff commands from everyone else in the command picker. Go to **Server Settings → Integrations → (bot) → Commands** and allow only King/R4/Mod. The bot also checks roles itself, so this is cosmetic.

---

## 6. Host it 24/7

The bot is tiny: one Python process, a SQLite file, and well under a gigabyte of RAM. Any small Linux server works. It needs a **persistent disk** for `data/tickets.db`, so avoid hosts that wipe the filesystem on redeploy or put apps to sleep (many "free bot hosting" and free PaaS tiers do one or the other).

### Option A: Oracle Cloud "Always Free" ($0)

- Oracle's Always Free tier includes Arm (Ampere A1) VMs. In mid-2026 Oracle **halved** the free A1 allowance to **2 OCPUs / 12 GB RAM** in total. That is still far more than this bot needs. There are also two small free AMD VMs (`VM.Standard.E2.1.Micro`), which are enough on their own.
- Signup needs a credit/debit card for identity verification.
- Popular regions often say "out of capacity" for A1. Pick a less busy home region, retry, or use the AMD micro shape.
- Oracle can reclaim Always Free instances that look idle, and a small bot looks idle. Many people upgrade the account to Pay As You Go (still $0 if you stay inside the free limits) to avoid this. Check Oracle's current Always Free page before relying on it.

### Option B: a cheap VPS (about the price of a coffee per month; most predictable)

- **Hetzner Cloud** is the usual budget pick and has US locations (Ashburn, VA and Hillsboro, OR). Hetzner raised cloud prices in 2026 and reshuffled its plan lineup, so check the current price of the smallest shared-vCPU plan on hetzner.com/cloud.
- DigitalOcean, Vultr, Linode/Akamai and similar providers also have small plans that work fine. Choose the cheapest 1 GB+ plan with Ubuntu.

### Option C: a PC or Raspberry Pi at home ($0)

This works if it's always on. Downtime equals your home power or internet outages.

### Server setup (Ubuntu, works for A or B)

```bash
# as root / sudo, once
sudo apt update && sudo apt install -y python3 python3-venv git
sudo adduser --disabled-password --gecos "" botuser
sudo -iu botuser

# as botuser: copy the project up (scp from your PC, or git clone your private repo)
#   scp -r rok-ticket-bot botuser@SERVER_IP:~/
cd ~/rok-ticket-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
nano .env          # paste DISCORD_TOKEN=...
nano config.json   # paste your config
.venv/bin/python run.py   # test it; Ctrl+C when it says "Logged in as ..."
exit

# back as sudo: run it as a service that restarts on crash and reboot
sudo cp /home/botuser/rok-ticket-bot/deploy/rok-ticket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rok-ticket-bot
journalctl -u rok-ticket-bot -f          # live logs
```

Day-to-day:

```bash
sudo systemctl restart rok-ticket-bot    # after editing config.json or updating code
sudo systemctl status rok-ticket-bot
```

**Backups:** `data/tickets.db` holds ticket state; #ticket-logs holds the permanent record. For a quick nightly copy, use cron as botuser: `0 4 * * * sqlite3 ~/rok-ticket-bot/data/tickets.db ".backup ~/tickets-backup.db"` (install `sqlite3` first).

**Security:** never commit `.env` or put the token in config.json (`.gitignore` already excludes both). Keep the server patched: `sudo apt upgrade`.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Startup says privileged intents not enabled | Developer Portal → Bot → turn on **Server Members** and **Message Content** intents, save, restart. |
| "Discord rejected the token" | Token is wrong or was reset. Reset it, paste the new one into `.env`. |
| Slash commands don't appear | Re-invite with the link in section 3 (it needs the `applications.commands` scope). Check `guild_id`. Restart the bot. Discord can take a minute. |
| "My highest role … is not above Member" | Drag the bot's role above Member, Unverified and the alliance roles (section 4b). |
| New members can see everything | @everyone still has View Channels somewhere. Run `/verify-diagnostics` and fix the listed channels (section 4c). |
| New members can't see #verify | Give Unverified View Channel + Read History on #verify. |
| "I couldn't create your ticket (missing permissions)" | The bot role needs the full list on the **Tickets** category (section 4c), including Manage Permissions. |
| Ping shows but nobody is notified | Make the ping role mentionable, or keep the "Mention All Roles" permission from the invite. |
| Transcript messages are blank | Message Content intent is off. |
| Ticket closed but channel not deleted | The bot couldn't post to #ticket-logs. Fix its permissions there, then delete the channel by hand (the transcript is still in it). |
| Accept/Deny says "already handled" | Another staff member clicked first; check #ticket-logs. |
| Welcome/deny DM failed | The applicant has DMs from server members turned off. This is noted in the log; nothing else to do. |
| Category full | Discord allows 50 channels per category. Close old tickets or make a second category and point config at it. |

---

## 8. Project layout

```
rok-ticket-bot/
├── run.py                    # entry point (logging, friendly startup errors)
├── requirements.txt
├── config.example.json       # copy → config.json
├── .env.example              # copy → .env (token)
├── deploy/rok-ticket-bot.service   # systemd unit for 24/7 hosting
└── bot/
    ├── client.py             # Bot class: intents, persistent views, slash-command sync
    ├── config.py             # loads + validates config.json/.env
    ├── database.py           # SQLite: tickets, atomic claim, one-open-ticket index
    ├── tickets.py            # all ticket logic: create, accept, deny, close, finalize, recovery
    ├── cogs/
    │   ├── onboarding.py     # on_member_join → Unverified, /setup-verify
    │   ├── lifecycle.py      # startup recovery, applicant left, channel deleted
    │   └── admin.py          # /verify-diagnostics, /migrate-existing-members
    ├── views/
    │   ├── verify_panel.py   # Open Application button + application form
    │   └── ticket_controls.py# Accept / Deny / Close + follow-up prompts
    └── utils/
        ├── checks.py         # is_staff, role-hierarchy and kick checks
        ├── diagnostics.py    # the self-check report
        ├── embeds.py         # every embed and DM in one place
        └── transcript.py     # channel history → .txt
```

Logs are written to the console and to `logs/bot.log` (rotated automatically).
