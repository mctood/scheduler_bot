import asyncio
import time
from collections import defaultdict
from collections.abc import Callable

import aiosqlite

from .db import Database
from .scheduling import next_slot, respace


class Scheduler:
    """Операции над очередью канала. Все изменения очереди одного канала
    выполняются под общим замком, чтобы два поста не получили одно время."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self._clock = clock
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def now(self) -> int:
        return int(self._clock())

    async def add_post(
        self,
        channel_id: int,
        user_id: int,
        from_chat_id: int,
        message_ids: list[int],
        summary: str,
    ) -> tuple[int, int]:
        """Поставить пост в конец очереди. Возвращает (id поста, время публикации)."""
        async with self._locks[channel_id]:
            channel = await self.db.get_channel(channel_id)
            if channel is None:
                raise ValueError(f"Unknown channel {channel_id}")
            publish_at = next_slot(
                last_pending=await self.db.last_pending_time(channel_id),
                last_published=channel["last_published_at"],
                interval=channel["interval_minutes"] * 60,
                now=self.now(),
            )
            post_id = await self.db.add_post(
                channel_id, user_id, from_chat_id, message_ids, summary, publish_at
            )
        return post_id, publish_at

    async def set_interval(self, channel_id: int, minutes: int) -> list[aiosqlite.Row]:
        """Сменить интервал и пересчитать очередь, начиная с первого поста."""
        async with self._locks[channel_id]:
            await self.db.set_interval(channel_id, minutes)
            await self._respace(channel_id, minutes * 60)
            return await self.db.pending_posts(channel_id)

    async def realign(self, channel_id: int) -> list[aiosqlite.Row]:
        """Выровнять очередь по текущему интервалу (например, после удаления поста)."""
        async with self._locks[channel_id]:
            channel = await self.db.get_channel(channel_id)
            if channel is None:
                return []
            await self._respace(channel_id, channel["interval_minutes"] * 60)
            return await self.db.pending_posts(channel_id)

    async def _respace(self, channel_id: int, interval: int) -> None:
        posts = await self.db.pending_posts(channel_id)
        new_times = respace([p["publish_at"] for p in posts], interval)
        await self.db.update_post_times([(p["id"], t) for p, t in zip(posts, new_times)])
