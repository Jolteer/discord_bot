"""Ticket database operations (ported from the old bot/database.py).

Each function opens its own short session/transaction, so concurrent events
never share a connection. (The old bot shared one connection, so a rollback in
one event could undo another event's write.)

``claim()`` is the only way out of 'open'. It's an atomic
``UPDATE ... WHERE status = 'open'``, so if two staff members click Accept and
Deny at the same moment, exactly one of them wins.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import Database
from ...core.models import Ticket
from ...core.types import utcnow

ACTIVE = ("open", "closing")


async def reserve(
    db: Database, *, guild_id: int, user_id: int, username: str, answers: list[dict[str, str]]
) -> int | None:
    """Insert an open ticket (no channel yet). None if the member already has one."""
    try:
        async with db.session() as session, session.begin():
            ticket = Ticket(
                guild_id=guild_id,
                user_id=user_id,
                username=username[:100],
                answers=answers,
                status="open",
                created_at=utcnow(),
            )
            session.add(ticket)
            await session.flush()
            return ticket.id
    except IntegrityError:
        # The one-active-ticket-per-member index said no.
        return None


async def set_channel(db: Database, ticket_id: int, channel_id: int) -> None:
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(channel_id=channel_id))


async def delete_reservation(db: Database, ticket_id: int) -> None:
    """Only used to undo a reservation whose channel couldn't be created."""
    async with db.session() as session, session.begin():
        await session.execute(delete(Ticket).where(Ticket.id == ticket_id, Ticket.status == "open"))


async def get(db: Database, ticket_id: int) -> Ticket | None:
    async with db.session() as session:
        return await session.get(Ticket, ticket_id)


async def get_by_channel(db: Database, channel_id: int) -> Ticket | None:
    async with db.session() as session:
        return (await session.execute(select(Ticket).where(Ticket.channel_id == channel_id))).scalar_one_or_none()


async def get_active_by_user(db: Database, guild_id: int, user_id: int) -> Ticket | None:
    async with db.session() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.guild_id == guild_id, Ticket.user_id == user_id, Ticket.status.in_(ACTIVE))
        )
        return result.scalar_one_or_none()


async def list_for_guild(db: Database, guild_id: int, status: str) -> list[Ticket]:
    async with db.session() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.guild_id == guild_id, Ticket.status == status).order_by(Ticket.id)
        )
        return list(result.scalars())


async def list_closed_needing_delete(db: Database, guild_id: int) -> list[Ticket]:
    async with db.session() as session:
        result = await session.execute(
            select(Ticket).where(
                Ticket.guild_id == guild_id,
                Ticket.status == "closed",
                Ticket.logged.is_(True),
                Ticket.channel_gone.is_(False),
                Ticket.channel_id.is_not(None),
            )
        )
        return list(result.scalars())


async def claim(
    db: Database,
    ticket_id: int,
    *,
    outcome: str,
    handled_by: int | None,
    reason: str | None = None,
    extra_role_id: int | None = None,
) -> bool:
    """Atomically move open -> closing and record the decision. False if someone beat us to it."""
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "open")
            .values(
                status="closing",
                outcome=outcome,
                handled_by=handled_by,
                reason=reason,
                extra_role_id=extra_role_id,
                kicked=False,
                claimed_at=utcnow(),
            )
        )
        return result.rowcount == 1


async def release(db: Database, ticket_id: int) -> None:
    """Undo a claim when the action failed, so staff can try again."""
    async with db.session() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "closing")
            .values(
                status="open",
                outcome=None,
                handled_by=None,
                reason=None,
                extra_role_id=None,
                kicked=False,
                claimed_at=None,
            )
        )


async def set_kicked(db: Database, ticket_id: int) -> None:
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(kicked=True))


async def add_note(db: Database, ticket_id: int, note: str) -> None:
    async with db.session() as session, session.begin():
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None:
            ticket.notes = f"{ticket.notes}\n{note}" if ticket.notes else note


async def stamp_closed_at(db: Database, ticket_id: int) -> None:
    async with db.session() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.id == ticket_id, Ticket.closed_at.is_(None)).values(closed_at=utcnow())
        )


async def mark_closed(db: Database, ticket_id: int, *, logged: bool) -> None:
    async with db.session() as session, session.begin():
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None:
            ticket.status = "closed"
            ticket.closed_at = ticket.closed_at or utcnow()
            ticket.logged = logged


async def mark_channel_gone(db: Database, ticket_id: int) -> None:
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(channel_gone=True))


def older_than(moment: datetime | None, seconds: float) -> bool:
    return moment is not None and (utcnow() - moment).total_seconds() > seconds
