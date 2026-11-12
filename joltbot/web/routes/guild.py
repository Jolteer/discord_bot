"""Per-server pages that don't belong to one module: overview, audit log, refresh, issues,
and who may use the dashboard.

Every route here takes ``access: RequireGuild``, which re-checks that the
logged-in user may use this exact server's dashboard before anything else runs.
The dashboard access page takes ``RequireManager`` instead: Manage Server only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select

from ...core.discord_rest import DiscordError, GuildData
from ...core.guild_settings import ModuleState, StaleSettingsError, load_all, save_module
from ...core.health import bot_status
from ...core.issues import list_issues
from ...core.models import AuditLog, GuildIssue
from ...modules.general.settings import MAX_DASHBOARD_ROLES, GeneralSettings
from ...modules.registry import MODULES
from ..rendering import render
from ..security import CsrfProtected, GuildAccess, RequireGuild, RequireManager, get_db, get_discord

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
            # Managers see the "Dashboard access" link; people let in by a role don't.
            "is_manager": access.is_manager,
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


# --------------------------------------------------------------------------- dashboard access


async def _access_guild_data(request: Request, access: GuildAccess) -> GuildData:
    try:
        return await get_discord(request).guild_data(access.guild_id)
    except DiscordError as e:
        raise HTTPException(503, f"Couldn't load this server's roles from Discord. {e}") from None


def _handed_out_roles(states: dict[str, ModuleState[Any]]) -> dict[int, str]:
    """Every role a module makes JoltBot give to members, with where it is set.

    Args:
        states: All of this server's module settings.

    Returns:
        ``{role ID: "Tickets & verification: Role given on accept"}``.
    """
    found: dict[int, str] = {}
    for name, state in states.items():
        assignable = getattr(state.config, "assignable_roles", None)
        if assignable is None:
            continue
        for role in assignable():
            found.setdefault(role.role_id, f"{MODULES[name].title}: {role.description}")
    return found


async def _render_access(
    request: Request,
    access: GuildAccess,
    data: GuildData,
    *,
    selected: list[str],
    version: int,
    errors: list[str],
    saved: bool = False,
    status_code: int = 200,
):
    return await render(
        request,
        "guild/access.html",
        {
            **guild_context(access, "access"),
            "roles": [
                {"id": str(r.id), "name": r.name, "color": f"#{r.color:06x}" if r.color else None, "managed": r.managed}
                for r in data.roles
            ],
            "selected": selected,
            "version": version,
            "errors": errors,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/access")
async def show_access(request: Request, access: RequireManager, saved: bool = False):
    """Show which roles may use this server's dashboard. Managers only.

    Args:
        request: Incoming HTTP request.
        access: A member with Manage Server, Administrator, or the owner.
        saved: Query flag to show the success banner after a redirect.

    Returns:
        HTML for ``guild/access.html``.

    Raises:
        HTTPException: 503 if Discord's role list cannot be loaded.
    """
    data = await _access_guild_data(request, access)
    async with get_db(request).session() as session:
        state = (await load_all(session, access.guild_id))["general"]
    return await _render_access(
        request,
        access,
        data,
        selected=[str(i) for i in state.config.dashboard_role_ids],
        version=state.version,
        errors=[],
        saved=saved,
    )


@router.post("/access", dependencies=[CsrfProtected])
async def save_access(request: Request, access: RequireManager):
    """Save which roles may use this server's dashboard. Managers only.

    The change is written to the audit log by ``save_module``, in the same
    transaction, with who made it and the old and new lists.

    Args:
        request: Incoming HTTP request (form body + CSRF).
        access: A member with Manage Server, Administrator, or the owner.

    Returns:
        Redirect to GET with ``?saved=1``, or the form with errors (400).

    Raises:
        HTTPException: 503 if Discord's role list cannot be loaded.
    """
    form = await request.form()
    picked = list(dict.fromkeys(str(v).strip() for v in form.getlist("dashboard_role_ids")))
    try:
        version = int(form.get("version", "-1"))
    except ValueError:
        version = -1
    data = await _access_guild_data(request, access)  # before the transaction: never hold one across a Discord call

    async def fail(errors: list[str]):
        return await _render_access(
            request, access, data, selected=picked, version=version, errors=errors, status_code=400
        )

    try:
        async with get_db(request).session() as session, session.begin():
            states = await load_all(session, access.guild_id)
            current: GeneralSettings = states["general"].config
            try:
                new = GeneralSettings.model_validate({**current.model_dump(mode="json"), "dashboard_role_ids": picked})
            except ValidationError:
                return await fail([f"Pick roles from the list, at most {MAX_DASHBOARD_ROLES}."])
            # Role IDs must belong to THIS server, or a pasted ID could name a role elsewhere.
            if any(role_id not in data.role_ids for role_id in new.dashboard_role_ids):
                return await fail(["A selected role isn't in this server. Refresh the page and pick again."])

            names = {r.id: r.name for r in data.roles}
            handed_out = _handed_out_roles(states)
            errors = [
                f"JoltBot gives out {names[role_id]} ({handed_out[role_id]}), so everyone who gets it that way "
                "would be able to use this dashboard. Pick a role that only the people you trust have."
                for role_id in new.dashboard_role_ids
                if role_id in handed_out and role_id not in current.dashboard_role_ids
            ]
            if errors:
                return await fail(errors)

            await save_module(
                session,
                access.guild_id,  # from the checked URL, never from the form
                "general",
                enabled=True,
                config=new,
                expected_version=version,
                actor=access.actor,
            )
    except StaleSettingsError:
        return await fail(["Someone else saved settings while you were editing. Reload the page and try again."])
    return RedirectResponse(f"/servers/{access.guild_id}/access?saved=1", status_code=303)
