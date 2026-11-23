# Smoke test

A manual pass through everything JoltBot does on Discord and on the dashboard. It is built from the behavior inventory in [AUDIT.md](AUDIT.md), section 3. The automated tests cover the rules; this covers what only a real Discord connection shows.

**Where to run it**

There is no test bot at the moment (the JoltBot Test application was deleted on 2026-11-23), so this is run on the live server, right after a deploy:

- Always: sections 1 and 2. They only open prompts and cancel them, so nothing changes for anyone.
- After a deploy that changed code on the ticket or alliance paths: also sections 4 to 7, with your own second account as the applicant, in a server where that is harmless.

Never start a bot on your PC with the live token: it would be a second copy of the live bot, answering real members.

## 0. Setup

On the live server there is nothing to set up. For section 1, read the logs on the server:

```bash
ssh ubuntu@18.221.166.235 "cd ~/joltbot && docker compose ps && docker compose logs --since 15m bot"
```

The health pages are `https://joltbot.jolteer.com/health` and `/health/bot`.

If a test application exists again, run everything there first instead: put its token, client ID and client secret in `.env`, run `alembic upgrade head`, and start `python -m joltbot.bot` and `python -m joltbot.web` in two terminals. Section 5 needs a second account with the staff role.

## 1. Startup

| # | Do | Expect |
|---|---|---|
| 1.1 | Read the bot's first log lines | No traceback. No "failed to load" line for any module |
| 1.2 | Look for the slash-command line | "Slash commands unchanged; not re-registering", or "Registered 2 global slash command(s)" on the first start with a new database. Any other count means the command list changed |
| 1.3 | Open the two health pages | Both answer `200`. `/health/bot` can take up to a minute after the bot connects |
| 1.4 | `docker compose ps` | `bot` and `web` are `healthy` (the bot about two minutes after it connects) |

## 2. Old messages still work

This is the check that a `custom_id` did not change. Use a panel and a ticket that were posted **before** this phase's code was started.

| # | Do | Expect |
|---|---|---|
| 2.1 | Click **Open Application** on the existing panel | The application form opens |
| 2.2 | In an existing open ticket, click **Accept**, then **Cancel** on the prompt | The private "Accept …?" prompt appears, then "Cancelled." |
| 2.3 | In the same ticket click **Deny**, then close the form | The "Deny application" form opens |
| 2.4 | In the same ticket click **Close**, then **Cancel** | The private confirm prompt appears, then "Cancelled." |
| 2.5 | On an existing alliance panel, pick an alliance, then close the form | The "Join …" form opens |
| 2.6 | On an existing join request, click **Reject**, then close the form | The "Reject request" form opens |

The four buttons of the old Gatekeeper bot (`rokverify:…`, `rokticket:…`) only exist in the live server. They are covered by `tests/test_contract.py`, not here.

## 3. Slash commands

| # | Do | Expect |
|---|---|---|
| 3.1 | Type `/tickets` and `/alliances` | Exactly: `panel`, `diagnostics`, `grant-existing`; and `panel`, `pending`, `cancel member:` |
| 3.2 | `/tickets diagnostics` as a manager | A private embed with findings and a footer counting errors and warnings |
| 3.3 | `/tickets panel` | "Posted the application panel in #…", pinned if the bot may pin; the previous panel is gone |
| 3.4 | `/tickets grant-existing`, then **Cancel** | A private prompt with the member count, then "Cancelled." |
| 3.5 | `/tickets grant-existing` as a staff member without Manage Server | Refused |
| 3.6 | `/alliances panel` | The menu is posted; the previous one is gone |
| 3.7 | `/alliances pending` | "No pending…" or a list with a link per request |
| 3.8 | Any `/tickets` command as a plain member (make it visible first under Integrations) | A private refusal. Nothing happens |

## 4. Ticket flow

Use a test account that has only the pending role.

| # | Do | Expect |
|---|---|---|
| 4.1 | Join the server with the test account | It gets the pending role |
| 4.2 | Click **Open Application**, answer any dropdowns, submit the form | A private reply with a link; a new `ticket-<name>` channel only the applicant, staff and the bot can see; the application embed with Accept / Deny / Close; the intro message; staff are pinged |
| 4.3 | Click **Open Application** again | "You already have an open application: #…" |
| 4.4 | As a non-staff member, click **Accept** in the ticket | "Only staff can do this." (or the server's own text) |
| 4.5 | As staff: **Accept** → pick an extra role → **Confirm accept** | The prompt says "✅ Accepted …". The applicant has the accept role and the extra role, and no pending role. They get the welcome DM. The log channel gets the decision with a transcript file. The ticket channel is deleted after the delay |
| 4.6 | New ticket. **Deny** → type a reason → **Deny, don't kick** | The applicant gets the deny DM with the "kept" ending and keeps the pending role. Log says "Not kicked" |
| 4.7 | New ticket. **Deny** → reason → **Deny and kick** | DM with the "kicked" ending arrives before the kick. The account is removed. Log says "Kicked" |
| 4.8 | New ticket. **Close** → **Yes, close it** | No roles change. Log says "Closed without decision" |
| 4.9 | New ticket. Leave the server with the applicant account | The ticket closes on its own; log says the applicant left |
| 4.10 | New ticket. Delete the ticket channel by hand | Log says the channel was deleted manually |

## 5. Two staff members at once

Needs two staff accounts on two devices.

| # | Do | Expect |
|---|---|---|
| 5.1 | New ticket. Both open the **Accept** prompt, then both press **Confirm accept** as close together as you can | One sees "✅ Accepted …", the other "❌ Someone else already handled this ticket." One welcome DM, one log post |
| 5.2 | New ticket. One prepares **Confirm accept**, the other **Deny and kick**; press together | Exactly one happens. Either the applicant has the role and is still in the server, or they were kicked and have no role. Never both |
| 5.3 | Alliance request. Two reviewers press **Accept** together | One "✅ Accepted …", the other "Already handled by @…". One DM, one log post |

## 6. Alliance flow

| # | Do | Expect |
|---|---|---|
| 6.1 | Pick an alliance from the menu, enter a name and a governor ID with letters in it | Refused: digits only |
| 6.2 | Submit a valid request | A private confirmation; the request appears in that alliance's request channel with Accept / Reject |
| 6.3 | Pick any alliance again | Refused: a request is already waiting |
| 6.4 | A reviewer of **another** alliance clicks **Accept** | "Only **…** leadership can decide this request." |
| 6.5 | The member clicks **Accept** on their own request (give them a reviewer role first) | Refused |
| 6.6 | The right reviewer clicks **Accept** | The member gets that alliance's role and loses any other alliance role. Buttons grey out. DM and log post |
| 6.7 | New request. **Reject** with a reason | No roles change. DM with the reason and when they may ask again. Buttons grey out |
| 6.8 | Ask the same alliance again straight away | Refused until the cooldown ends |
| 6.9 | New request. `/alliances cancel member:` | The request is cancelled; the member is told; they can ask again at once |

## 7. Restart in the middle

| # | Do | Expect |
|---|---|---|
| 7.1 | Open a ticket. Stop the bot. Start it. Click **Accept** in that ticket | The prompt appears and the accept works |
| 7.2 | Post a join request. Stop the bot. Start it. Click **Accept** on it | Works |
| 7.3 | Accept a ticket and stop the bot within the delete delay | After the next start, within 5 minutes, the log is posted once and the channel is deleted |

## 8. Dashboard

| # | Do | Expect |
|---|---|---|
| 8.1 | Open `http://localhost:8000`, log in with Discord | You land on the server list. Only servers you can manage, or hold an access role in, are shown |
| 8.2 | Open the test server: Overview, General, Tickets, Alliances, Audit log, Dashboard access | Each page loads. No error page |
| 8.3 | Change one ticket message, save | "Saved." The change shows in the audit log with your name |
| 8.4 | Open Tickets in two tabs. Save in the first, then save in the second | The second is refused: someone else saved while you were editing |
| 8.5 | Trigger the changed message in Discord without restarting the bot | The new text is used |
| 8.6 | Open `/servers/<an ID of a server you are not in>` | "Not allowed" |
| 8.7 | Log out, then press the browser's Back button and reload | You are sent to the login page |

## 9. Record

Note the date, the commit, which sections you ran, and anything that did not match. A mismatch is a finding for the bug register in AUDIT.md, not something to fix on the spot.
