"""Public landing page and the server picker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from ...core.models import Guild
from ...core.permissions import bot_invite_url
from ..rendering import render
from ..security import RequireSession, UserGuild, current_user_guilds, get_config, get_db

router = APIRouter()


@router.get("/")
async def landing(request: Request):
    return await render(request, "landing.html")


@router.get("/servers")
async def servers(request: Request, ws: RequireSession):
    config = get_config(request)
    manageable = [g for g in await current_user_guilds(request, ws) if g.can_manage]
    async with get_db(request).session() as session:
        ids = [g.id for g in manageable]
        active = (
            set((await session.execute(select(Guild.id).where(Guild.id.in_(ids), Guild.active.is_(True)))).scalars())
            if ids
            else set()
        )

    def card(g: UserGuild) -> dict[str, Any]:
        return {
            "id": str(g.id),
            "name": g.name,
            "icon_url": g.icon_url,
            "initial": g.name[:1].upper(),
            "invite_url": bot_invite_url(config.discord_client_id, g.id),
        }

    return await render(
        request,
        "servers.html",
        {
            "joined": [card(g) for g in sorted(manageable, key=lambda g: g.name.lower()) if g.id in active],
            "not_joined": [card(g) for g in sorted(manageable, key=lambda g: g.name.lower()) if g.id not in active],
        },
    )
