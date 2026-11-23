"""Posts log embeds to their channels through one queue per channel.

This is the producer/consumer pattern. Event handlers (the producers) put an
embed on a channel's queue and return at once. One worker task per channel
(the consumer) takes embeds off the queue and sends them in order. So a raid or
a purge can't make hundreds of handlers all call Discord at the same moment,
and a slow or rate-limited channel never holds up the rest of the bot.

Two more things keep a burst cheap:

* Batching: a message can carry up to 10 embeds, so the worker sends whatever
  has piled up as one message. 200 joins become about 20 API calls.
* A bounded queue: past ``QUEUE_MAX`` waiting embeds, new ones are counted and
  dropped instead of growing without limit. The entries themselves are already
  saved in the database by then; only their channel posts are skipped, and the
  channel is told how many.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from .constants import CATEGORY_TITLES, channel_issue_key

if TYPE_CHECKING:
    from ...bot.client import JoltBot

log = logging.getLogger(__name__)

QUEUE_MAX = 300  # embeds waiting for one channel before new ones are dropped
MESSAGE_MAX_EMBEDS = 10  # Discord's limit of embeds on one message
MESSAGE_EMBED_CHARS = 6000  # ...and of all their text together
SEND_PAUSE_SECONDS = 1.0  # between two messages to one channel; lets a burst pile up into bigger batches
IDLE_SECONDS = 60.0  # a worker with nothing to do for this long stops; the next embed starts a new one
# What JoltBot needs in a log channel.
LOG_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "read_message_history")


@dataclass
class _Lane:
    """One channel's queue and the worker that empties it."""

    guild_id: int
    channel_id: int
    queue: asyncio.Queue[tuple[str, discord.Embed]] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAX))
    dropped: int = 0
    task: asyncio.Task[None] | None = None


class Dispatcher:
    """Owns the per-channel queues and workers for one bot process."""

    def __init__(self, bot: JoltBot, *, pause: float = SEND_PAUSE_SECONDS, idle: float = IDLE_SECONDS) -> None:
        """Start with no queues; each is created on first use.

        Args:
            bot: Running bot (for channels and issue reporting).
            pause: Seconds between two messages to one channel.
            idle: Seconds an empty queue's worker waits before stopping.
        """
        self.bot = bot
        self.pause = pause
        self.idle = idle
        self._lanes: dict[int, _Lane] = {}
        # Last known state of each (guild, category) log channel, so the dashboard
        # issue is written when that state changes and not on every post.
        self._channel_ok: dict[tuple[int, str], bool] = {}

    def enqueue(self, guild_id: int, channel_id: int, category: str, embed: discord.Embed) -> None:
        """Queue one embed for a log channel. Never blocks and never raises.

        Args:
            guild_id: Server the channel belongs to.
            channel_id: The log channel.
            category: Log category of the event (for the dashboard issue if posting fails).
            embed: The post.
        """
        lane = self._lanes.get(channel_id)
        if lane is None:
            lane = self._lanes[channel_id] = _Lane(guild_id, channel_id)
        if lane.task is None or lane.task.done():
            lane.task = asyncio.create_task(self._run(lane))
        try:
            lane.queue.put_nowait((category, embed))
        except asyncio.QueueFull:
            lane.dropped += 1

    async def drain(self) -> None:
        """Wait until every queued embed has been sent or given up on. Used by tests."""
        await asyncio.gather(*(lane.queue.join() for lane in self._lanes.values()))

    async def close(self) -> None:
        """Stop every worker. Embeds still waiting are not posted (their entries are saved)."""
        tasks = [lane.task for lane in self._lanes.values() if lane.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._lanes.clear()

    async def _run(self, lane: _Lane) -> None:
        """Worker loop for one channel: take what has piled up, send it as one message, repeat."""
        carry: tuple[str, discord.Embed] | None = None  # taken off the queue but didn't fit the last batch
        try:
            while True:
                if carry is None:
                    try:
                        carry = await asyncio.wait_for(lane.queue.get(), self.idle)
                    except TimeoutError:
                        if lane.queue.empty():
                            return
                        continue
                batch, size = [carry], len(carry[1])
                carry = None
                while len(batch) < MESSAGE_MAX_EMBEDS and not lane.queue.empty():
                    item = lane.queue.get_nowait()
                    if size + len(item[1]) > MESSAGE_EMBED_CHARS:
                        carry = item
                        break
                    batch.append(item)
                    size += len(item[1])
                try:
                    await self._send(lane, batch)
                finally:
                    for _ in batch:
                        lane.queue.task_done()
                await asyncio.sleep(self.pause)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Guild %s: mod log worker for channel %s stopped", lane.guild_id, lane.channel_id)
        finally:
            if carry is not None:
                lane.queue.task_done()

    async def _send(self, lane: _Lane, batch: list[tuple[str, discord.Embed]]) -> None:
        """Send one batch. A channel that is gone or closed to JoltBot becomes a dashboard issue."""
        categories = sorted({category for category, _ in batch})
        guild = self.bot.get_guild(lane.guild_id)
        channel = guild.get_channel(lane.channel_id) if guild is not None else None
        if not isinstance(channel, discord.TextChannel):
            await self._mark(lane.guild_id, categories, ok=False, problem="its channel no longer exists")
            return
        try:
            await channel.send(embeds=[embed for _, embed in batch], allowed_mentions=discord.AllowedMentions.none())
            if lane.dropped:
                skipped, lane.dropped = lane.dropped, 0
                await channel.send(
                    f"⚠️ {skipped} log post{'s' if skipped != 1 else ''} skipped here during a burst of activity. "
                    "The entries themselves are saved in the log history.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            await self._mark(
                lane.guild_id,
                categories,
                ok=False,
                problem=f"I can't post in #{channel.name}. JoltBot needs View Channel, Send Messages, Embed Links "
                "and Read Message History there",
            )
            return
        except discord.HTTPException as e:
            log.warning("Guild %s: couldn't post %d log embed(s): %s", lane.guild_id, len(batch), e)
            return
        await self._mark(lane.guild_id, categories, ok=True)

    async def _mark(self, guild_id: int, categories: list[str], *, ok: bool, problem: str = "") -> None:
        """Report or clear the dashboard issue for each category's log channel, when its state changed."""
        for category in categories:
            key = (guild_id, category)
            if self._channel_ok.get(key) is ok:
                continue
            self._channel_ok[key] = ok
            if ok:
                await self.bot.resolve_issue(guild_id, channel_issue_key(category))
            else:
                await self.bot.report_issue(
                    guild_id,
                    channel_issue_key(category),
                    f"The {CATEGORY_TITLES[category]} log isn't being posted: {problem}. "
                    "Pick another channel or fix the permissions; entries are still saved in the log history.",
                )
