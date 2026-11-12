"""Per-server pages that don't belong to one module: overview, audit log, refresh, issues.

Every route here takes ``access: RequireGuild``, which re-checks that the
logged-in user may manage this exact server before anything else runs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from ...core.discord_rest import DiscordError
from ...core.guild_settings import load_all
from ...core.health import bot_status
from ...core.issues import list_issues
from ...core.models import AuditLog, GuildIssue
from ...modules.registry import MODULES
from ..rendering import render
from ..security import CsrfProtected, GuildAccess, RequireGuild, get_db, get_discord

router = APIRouter(prefix="/servers/{guild_id}")
AUDIT_PAGE_SIZE = 50


def guild_context(access: GuildAccess, active: str) -> dict[str, Any]:
    """Build template context shared by every per-server page.

    Args:
        access: Proven guild manager access.
        active: Sidebar highlight key (e.g. ``"overview"``, ``"tickets"``).

    Returns:
        Dict with ``guild`` display fields and ``active``.
    """
    return {
        "guild": {
            "id": str(access.guild_id),
            "name": access.guild.name or access.user_guild.name,
            "icon_url": access.user_guild.icon_url,
            "initial": (access.guild.name or "?")[:1].upper(),
        },
        "active": active,
    }


@router.get("")
async def overview(request: Request, access: RequireGuild):
    """Guild overview: module status, setup problems, and open issues.

    Args:
        request: Incoming HTTP request.
        access: Authenticated guild manager.

    Returns:
        HTML for ``guild/overview.html``.
    """
    async with get_db(request).session() as session:
        states = await load_all(session, access.guild_id)
        issues = await list_issues(session, access.guild_id)
        bot = await bot_status(session)
    modules = [
        {
            "name": m.name,
            "title": m.title,
            "description": m.description,
            "toggleable": m.toggleable,
            "has_page": m.web is not None,  # bot-only modules have no settings page to link to
            "enabled": states[m.name].enabled,
            "problems": states[m.name].config.setup_problems(),
        }
        for m in MODULES.values()
    ]
    return await render(
        request,
        "guild/overview.html",
        {
            **guild_context(access, "overview"),
            "modules": modules,
            "issues": [{"id": i.id, "message": i.message, "count": i.count, "last_seen": i.last_seen} for i in issues],
            "bot_offline": not bot.online,
            "bot_last_seen": bot.last_seen,
        },
    )


def _short(value: Any, names: dict[str, str]) -> str:
    """Readable version of an audit value; Discord IDs become role/channel names when known."""
    if value is None or value == [] or value == "":
        return "(none)"
    if isinstance(value, list):
        return (
            ", ".join(_short(v, names) for v in value)
            if all(not isinstance(v, dict) for v in value)
            else f"{len(value)} item(s)"
        )
    text = str(value)
    if text in names:
        return names[text]
    return text if len(text) <= 120 else text[:117] + "…"


@router.get("/audit")
async def audit(request: Request, access: RequireGuild, before: int | None = None):
    query = select(AuditLog).where(AuditLog.guild_id == access.guild_id)  # this server only
    if before is not None:
        query = query.where(AuditLog.id < before)
    query = query.order_by(AuditLog.id.desc()).limit(AUDIT_PAGE_SIZE + 1)
    async with get_db(request).session() as session:
        rows = list((await session.execute(query)).scalars())
    has_more = len(rows) > AUDIT_PAGE_SIZE
    rows = rows[:AUDIT_PAGE_SIZE]

    names: dict[str, str] = {}
    try:
        data = await get_discord(request).guild_data(access.guild_id)
        names = {str(r.id): f"@{r.name}" for r in data.roles} | {str(c.id): f"#{c.name}" for c in data.channels}
    except DiscordError:
        pass  # show raw IDs instead

    entries = [
        {
            "id": r.id,
            "when": r.created_at,
            "who": r.actor_name,
            "source": r.source,
            "action": r.action,
            "module": MODULES[r.module].title if r.module in MODULES else (r.module or ""),
            "changes": [
                {"field": k, "old": _short(v[0], names), "new": _short(v[1], names)}
                for k, v in (r.changes or {}).items()
                if isinstance(v, list) and len(v) == 2
            ],
        }
        for r in rows
    ]
    context = {
        **guild_context(access, "audit"),
        "entries": entries,
        "has_more": has_more,
        "next_before": rows[-1].id if rows else None,
    }
    # htmx "Load more" asks for just the extra rows.
    if request.headers.get("hx-request") and before is not None:
        return await render(request, "guild/_audit_rows.html", context)
    return await render(request, "guild/audit.html", context)


@router.post("/refresh", dependencies=[CsrfProtected])
async def refresh(request: Request, access: RequireGuild, next: str = Form("")):
    """Forget the cached roles/channels so newly created ones show up."""
    get_discord(request).forget_guild(access.guild_id)
    prefix = f"/servers/{access.guild_id}"
    # Only redirect within this server's pages (never to an outside URL).
    target = next if next.startswith(prefix) and "//" not in next else prefix
    return RedirectResponse(target, status_code=303)


@router.post("/issues/{issue_id}/dismiss", dependencies=[CsrfProtected])
async def dismiss_issue(request: Request, access: RequireGuild, issue_id: int):
    async with get_db(request).session() as session, session.begin():
        # The guild_id condition stops someone dismissing another server's issue by changing issue_id.
        result = await session.execute(
            delete(GuildIssue).where(GuildIssue.id == issue_id, GuildIssue.guild_id == access.guild_id)
        )
    if result.rowcount != 1:
        raise HTTPException(404, "That warning doesn't exist (maybe it was already cleared).")
    return RedirectResponse(f"/servers/{access.guild_id}", status_code=303)
