"""GET /health for Docker and hosting platforms: 200 if the dashboard can reach its database.
GET /health/bot for uptime monitors: 200 if the bot is connected to Discord."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...core.health import bot_status
from ..security import get_db

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    """Public and cheap: no login, no Discord calls, no details about what failed."""
    try:
        async with get_db(request).session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        log.warning("Health check: database unavailable", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.get("/health/bot", include_in_schema=False)
async def bot_health(request: Request) -> JSONResponse:
    """For uptime monitors: 200 while the bot's heartbeat is fresh, 503 otherwise.

    Separate from ``/health`` on purpose. That one tells Docker and Caddy whether
    the dashboard works, and must not fail just because the bot is down.
    Public like ``/health``, and just as tight-lipped.
    """
    try:
        async with get_db(request).session() as session:
            status = await bot_status(session)
    except Exception:
        log.warning("Bot health check: database unavailable", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=503)
    if status.online:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "offline"}, status_code=503)
