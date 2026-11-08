"""GET /health for Docker and hosting platforms: 200 if the dashboard can reach its database."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

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
