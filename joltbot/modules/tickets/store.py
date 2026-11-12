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
    """Insert an open ticket row before the channel exists.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.
        user_id: Applicant snowflake.
        username: Snapshot of the applicant's name for logs.
        answers: Form answers ``[{"label", "value"}, ...]``.

    Returns:
        New ticket id, or ``None`` if the partial unique index blocked a duplicate.
    """
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


async def set_channel(db: Database, ticket_id: int, channel_id: int) -> bool:
    """Attach the created Discord channel id to an open ticket.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
        channel_id: Discord text channel snowflake.

    Returns:
        True if the ticket was still ``open`` and now has the channel. False if
        it was closed or removed in the meantime; the caller must then delete
        the channel, because no ticket points at it.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(Ticket).where(Ticket.id == ticket_id, Ticket.status == "open").values(channel_id=channel_id)
        )
        return result.rowcount == 1


async def delete_reservation(db: Database, ticket_id: int) -> None:
    """Undo a reservation whose channel could not be created.

    Args:
        db: Shared database.
        ticket_id: Open ticket with no usable channel.
    """
    async with db.session() as session, session.begin():
        await session.execute(delete(Ticket).where(Ticket.id == ticket_id, Ticket.status == "open"))


async def get(db: Database, ticket_id: int) -> Ticket | None:
    """Fetch a ticket by primary key.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.

    Returns:
        ``Ticket`` or ``None``.
    """
    async with db.session() as session:
        return await session.get(Ticket, ticket_id)


async def get_by_channel(db: Database, channel_id: int) -> Ticket | None:
    """Fetch the ticket bound to a Discord channel.

    Args:
        db: Shared database.
        channel_id: Ticket channel snowflake.

    Returns:
        ``Ticket`` or ``None``.
    """
    async with db.session() as session:
        return (await session.execute(select(Ticket).where(Ticket.channel_id == channel_id))).scalar_one_or_none()


async def get_active_by_user(db: Database, guild_id: int, user_id: int) -> Ticket | None:
    """Fetch the member's open or closing ticket in a guild, if any.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.
        user_id: Applicant snowflake.

    Returns:
        Active ``Ticket`` or ``None``.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.guild_id == guild_id, Ticket.user_id == user_id, Ticket.status.in_(ACTIVE))
        )
        return result.scalar_one_or_none()


async def list_active_user_ids(db: Database, guild_id: int) -> set[int]:
    """Members of a guild who have an open or closing ticket right now.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.

    Returns:
        Their Discord user IDs.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Ticket.user_id).where(Ticket.guild_id == guild_id, Ticket.status.in_(ACTIVE))
        )
        return set(result.scalars())


async def list_for_guild(db: Database, guild_id: int, status: str) -> list[Ticket]:
    """List tickets for a guild filtered by status.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.
        status: ``open``, ``closing``, or ``closed``.

    Returns:
        Matching tickets ordered by id.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Ticket).where(Ticket.guild_id == guild_id, Ticket.status == status).order_by(Ticket.id)
        )
        return list(result.scalars())


async def list_closed_needing_delete(db: Database, guild_id: int) -> list[Ticket]:
    """Closed tickets whose channel still exists and should be deleted after delay.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.

    Returns:
        Tickets with ``logged`` True and ``channel_gone`` False.
    """
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


async def list_closed_unlogged(db: Database, guild_id: int) -> list[Ticket]:
    """Closed tickets whose log post failed and whose channel was kept because of it.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.

    Returns:
        Tickets with ``logged`` False and ``channel_gone`` False, ordered by id.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Ticket)
            .where(
                Ticket.guild_id == guild_id,
                Ticket.status == "closed",
                Ticket.logged.is_(False),
                Ticket.channel_gone.is_(False),
                Ticket.channel_id.is_not(None),
            )
            .order_by(Ticket.id)
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
    """Atomically move ``open`` → ``closing`` and record the decision.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
        outcome: Decision string (e.g. ``accepted``, ``denied``, ``closed``).
        handled_by: Staff user id, or None for system outcomes.
        reason: Optional deny/close reason.
        extra_role_id: Optional accept extra role.

    Returns:
        True if this caller won the race; False if already claimed.
    """
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
    """Undo a claim when the action failed, so staff can try again.

    Args:
        db: Shared database.
        ticket_id: Ticket stuck in ``closing`` that should return to ``open``.
    """
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
    """Record that Deny-and-kick succeeded for this ticket.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
    """
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(kicked=True))


async def add_note(db: Database, ticket_id: int, note: str) -> None:
    """Append a line to the ticket's notes (shown on the log embed).

    A note the ticket already has is not added again, so work that is retried
    (see ``TicketManager.recover``) doesn't repeat itself in the log.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
        note: Single note line to append.
    """
    async with db.session() as session, session.begin():
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None and note not in ticket.note_lines:
            ticket.notes = f"{ticket.notes}\n{note}" if ticket.notes else note


async def stamp_closed_at(db: Database, ticket_id: int) -> None:
    """Set ``closed_at`` once if not already stamped (starts delete delay).

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
    """
    async with db.session() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.id == ticket_id, Ticket.closed_at.is_(None)).values(closed_at=utcnow())
        )


async def mark_closed(db: Database, ticket_id: int, *, logged: bool) -> None:
    """Move the ticket to ``closed`` and record whether the log post succeeded.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
        logged: True if transcript/decision was posted to the log channel.
    """
    async with db.session() as session, session.begin():
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None:
            ticket.status = "closed"
            ticket.closed_at = ticket.closed_at or utcnow()
            ticket.logged = logged


async def mark_logged(db: Database, ticket_id: int) -> None:
    """Record that a closed ticket's log post got through on a later try.

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
    """
    async with db.session() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.id == ticket_id, Ticket.status == "closed").values(logged=True)
        )


async def mark_channel_gone(db: Database, ticket_id: int) -> None:
    """Mark that the ticket channel was deleted (or never existed).

    Args:
        db: Shared database.
        ticket_id: Ticket primary key.
    """
    async with db.session() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(channel_gone=True))


def older_than(moment: datetime | None, seconds: float) -> bool:
    """True if ``moment`` is set and older than ``seconds`` ago (UTC).

    Args:
        moment: Timestamp to compare, or None.
        seconds: Age threshold in seconds.

    Returns:
        Whether recovery should treat the ticket as stuck.
    """
    return moment is not None and (utcnow() - moment).total_seconds() > seconds
