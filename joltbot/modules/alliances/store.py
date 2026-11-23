"""Alliance request database operations.

Each function opens its own short session/transaction, so concurrent events
never share a connection. All queries go through SQLAlchemy, which sends
values as bound parameters, never as part of the SQL text.

A request leaves 'pending' only through ``claim()`` (Accept) or ``decide()``
(everything else). Both are an atomic ``UPDATE ... WHERE status = 'pending'``,
so if two reviewers click at the same moment, exactly one of them wins.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import Database, rows_changed
from ...core.models import AllianceRequest, PanelMessage
from ...core.types import utcnow
from .constants import ACTIVE_STATUSES, MODULE, STATUS_DECIDING, STATUS_PENDING, STATUS_REJECTED


async def create(
    db: Database,
    *,
    guild_id: int,
    user_id: int,
    username: str,
    alliance_role_id: int,
    alliance_name: str,
    ingame_name: str,
    governor_id: str,
    member_joined_at: datetime | None,
) -> int | None:
    """Insert a pending request, before its message is posted.

    Args:
        db: Shared database.
        guild_id: Discord guild snowflake.
        user_id: The member asking to join.
        username: Snapshot of their name for the record.
        alliance_role_id: The alliance's member role, which identifies the alliance.
        alliance_name: The alliance's name right now.
        ingame_name: What they typed as their in-game name.
        governor_id: What they typed as their governor ID.
        member_joined_at: When they joined the server, if Discord says.

    Returns:
        New request id, or ``None`` if the member already has a pending request
        (the partial unique index refused the row).
    """
    try:
        async with db.session() as session, session.begin():
            request = AllianceRequest(
                guild_id=guild_id,
                user_id=user_id,
                username=username[:100],
                alliance_role_id=alliance_role_id,
                alliance_name=alliance_name[:100],
                ingame_name=ingame_name[:100],
                governor_id=governor_id[:32],
                member_joined_at=member_joined_at,
                status=STATUS_PENDING,
                created_at=utcnow(),
            )
            session.add(request)
            await session.flush()
            return request.id
    except IntegrityError:
        # The one-pending-request-per-member index said no.
        return None


async def set_message(db: Database, request_id: int, channel_id: int, message_id: int) -> bool:
    """Record where a pending request's message (with the buttons) was posted.

    Returns:
        True if the request was still pending. False if it was closed in the
        meantime; the caller should then delete the message.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.id == request_id, AllianceRequest.status == STATUS_PENDING)
            .values(channel_id=channel_id, message_id=message_id)
        )
        return rows_changed(result) == 1


async def delete_unposted(db: Database, request_id: int) -> None:
    """Undo a request whose message could not be posted, so the member can try again."""
    async with db.session() as session, session.begin():
        await session.execute(
            delete(AllianceRequest).where(
                AllianceRequest.id == request_id,
                AllianceRequest.status == STATUS_PENDING,
                AllianceRequest.message_id.is_(None),
            )
        )


async def get(db: Database, request_id: int) -> AllianceRequest | None:
    """Fetch a request by primary key."""
    async with db.session() as session:
        return await session.get(AllianceRequest, request_id)


async def get_by_message(db: Database, message_id: int) -> AllianceRequest | None:
    """Fetch the request whose Accept / Reject buttons are on this message."""
    async with db.session() as session:
        result = await session.execute(select(AllianceRequest).where(AllianceRequest.message_id == message_id))
        return result.scalar_one_or_none()


async def get_active_by_user(db: Database, guild_id: int, user_id: int) -> AllianceRequest | None:
    """Fetch the member's pending (or being-decided) request in a guild, if any."""
    async with db.session() as session:
        result = await session.execute(
            select(AllianceRequest).where(
                AllianceRequest.guild_id == guild_id,
                AllianceRequest.user_id == user_id,
                AllianceRequest.status.in_(ACTIVE_STATUSES),
            )
        )
        return result.scalar_one_or_none()


async def list_active(db: Database, guild_id: int) -> list[AllianceRequest]:
    """Every request of a guild still waiting for a decision, oldest first."""
    async with db.session() as session:
        result = await session.execute(
            select(AllianceRequest)
            .where(AllianceRequest.guild_id == guild_id, AllianceRequest.status.in_(ACTIVE_STATUSES))
            .order_by(AllianceRequest.id)
        )
        return list(result.scalars())


async def last_rejection(db: Database, guild_id: int, user_id: int, alliance_role_id: int) -> AllianceRequest | None:
    """The member's most recent rejected request to this alliance, for the cooldown."""
    async with db.session() as session:
        result = await session.execute(
            select(AllianceRequest)
            .where(
                AllianceRequest.guild_id == guild_id,
                AllianceRequest.user_id == user_id,
                AllianceRequest.alliance_role_id == alliance_role_id,
                AllianceRequest.status == STATUS_REJECTED,
            )
            .order_by(AllianceRequest.decided_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def claim(db: Database, request_id: int, *, decided_by: int) -> bool:
    """Atomically move ``pending`` → ``deciding`` for an Accept.

    Returns:
        True if this caller won the race; False if someone else got there first.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.id == request_id, AllianceRequest.status == STATUS_PENDING)
            .values(status=STATUS_DECIDING, decided_by=decided_by, claimed_at=utcnow())
        )
        return rows_changed(result) == 1


async def finish(db: Database, request_id: int, *, status: str) -> None:
    """Move a claimed request (``deciding``) to its final status, once the role change worked."""
    async with db.session() as session, session.begin():
        await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.id == request_id, AllianceRequest.status == STATUS_DECIDING)
            .values(status=status, decided_at=utcnow())
        )


async def release(db: Database, request_id: int) -> None:
    """Undo a claim when the role change failed, so leadership can try again."""
    async with db.session() as session, session.begin():
        await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.id == request_id, AllianceRequest.status == STATUS_DECIDING)
            .values(status=STATUS_PENDING, decided_by=None, claimed_at=None)
        )


async def release_stuck(db: Database) -> int:
    """Reopen every request left in ``deciding``. Called once when the bot starts.

    Nothing is being decided at that moment, so such a request was cut short by
    a restart. Back in ``pending``, its buttons work again; accepting twice is
    harmless, because the role change sets the member's roles to the same result.

    Returns:
        How many requests were reopened.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.status == STATUS_DECIDING)
            .values(status=STATUS_PENDING, decided_by=None, claimed_at=None)
        )
        return rows_changed(result)


async def decide(
    db: Database, request_id: int, *, status: str, decided_by: int | None, reason: str | None = None
) -> bool:
    """Atomically move ``pending`` → a final status that needs no role change.

    Args:
        db: Shared database.
        request_id: Request primary key.
        status: ``rejected``, ``closed`` or ``cancelled``.
        decided_by: Who did it, or ``None`` when the member simply left.
        reason: Optional reject reason.

    Returns:
        True if this caller won the race; False if the request was already handled.
    """
    async with db.session() as session, session.begin():
        result = await session.execute(
            update(AllianceRequest)
            .where(AllianceRequest.id == request_id, AllianceRequest.status == STATUS_PENDING)
            .values(status=status, decided_by=decided_by, reason=reason, decided_at=utcnow())
        )
        return rows_changed(result) == 1


async def get_panel(db: Database, guild_id: int) -> PanelMessage | None:
    """Where this guild's alliance panel was last posted, if it was."""
    async with db.session() as session:
        return await session.get(PanelMessage, (guild_id, MODULE))


async def set_panel(db: Database, guild_id: int, channel_id: int, message_id: int) -> None:
    """Save where the alliance panel was posted, replacing the previous location."""
    async with db.session() as session, session.begin():
        row = await session.get(PanelMessage, (guild_id, MODULE))
        if row is None:
            session.add(PanelMessage(guild_id=guild_id, module=MODULE, channel_id=channel_id, message_id=message_id))
        else:
            row.channel_id, row.message_id = channel_id, message_id
