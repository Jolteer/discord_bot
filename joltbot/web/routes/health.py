"""GET /health for Docker and hosting platforms: 200 if the dashboard can reach its database.
GET /health/bot for uptime monitors: 200 if the bot is connected to Discord.

Both also answer HEAD with the same status code and no body, because some uptime
monitors (UptimeRobot's free plan, for one) can send nothing else."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from ...core.health import bot_status
from ..security import get_db

router = APIRouter()
log = logging.getLogger(__name__)


def _reply(request: Request, status: str, status_code: int = 200) -> Response:
    """The JSON answer for GET; for HEAD the same status code with an empty body."""
    if request.method == "HEAD":
        return Response(status_code=status_code)
    return JSONResponse({"status": status}, status_code=status_code)


@router.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
async def health(request: Request) -> Response:
    """Public and cheap: no login, no Discord calls, no details about what failed."""
    try:
        async with get_db(request).session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        log.warning("Health check: database unavailable", exc_info=True)
        return _reply(request, "error", 503)
    return _reply(request, "ok")


@router.api_route("/health/bot", methods=["GET", "HEAD"], include_in_schema=False)
async def bot_health(request: Request) -> Response:
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
        return _reply(request, "error", 503)
    if status.online:
        return _reply(request, "ok")
    return _reply(request, "offline", 503)
