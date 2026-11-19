import json
import os
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    chat_id           INTEGER PRIMARY KEY,
    title             TEXT    NOT NULL,
    username          TEXT,
    interval_minutes  INTEGER NOT NULL,
    last_published_at INTEGER,
    active            INTEGER NOT NULL DEFAULT 1,
    night_start       INTEGER,
    night_end         INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    selected_channel INTEGER
);

CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    from_chat_id INTEGER NOT NULL,
    message_ids  TEXT    NOT NULL,
    summary      TEXT    NOT NULL DEFAULT '',
    publish_at   INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_status_time ON posts (status, publish_at);
CREATE INDEX IF NOT EXISTS idx_posts_channel ON posts (channel_id, status, publish_at);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        columns = {r["name"] for r in await self._fetchall("PRAGMA table_info(channels)")}
        for name in ("night_start", "night_end"):
            if name not in columns:
                await self.conn.execute(f"ALTER TABLE channels ADD COLUMN {name} INTEGER")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _write(self, sql: str, params: tuple = ()) -> int | None:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    # ---------- channels ----------

    async def upsert_channel(
        self, chat_id: int, title: str, username: str | None, default_interval: int
    ) -> None:
        await self._write(
            """
            INSERT INTO channels (chat_id, title, username, interval_minutes, active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title, username = excluded.username, active = 1
            """,
            (chat_id, title, username, default_interval),
        )

    async def deactivate_channel(self, chat_id: int) -> None:
        await self._write("UPDATE channels SET active = 0 WHERE chat_id = ?", (chat_id,))

    async def get_channel(self, chat_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM channels WHERE chat_id = ?", (chat_id,))

    async def list_channels(self) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM channels WHERE active = 1 ORDER BY title COLLATE NOCASE"
        )

    async def set_interval(self, chat_id: int, minutes: int) -> None:
        await self._write(
            "UPDATE channels SET interval_minutes = ? WHERE chat_id = ?", (minutes, chat_id)
        )

    async def set_night(self, chat_id: int, start: int | None, end: int | None) -> None:
        await self._write(
            "UPDATE channels SET night_start = ?, night_end = ? WHERE chat_id = ?",
            (start, end, chat_id),
        )

    async def set_last_published(self, chat_id: int, ts: int) -> None:
        await self._write(
            "UPDATE channels SET last_published_at = ? WHERE chat_id = ?", (ts, chat_id)
        )

    # ---------- users ----------

    async def get_selected_channel(self, user_id: int) -> int | None:
        row = await self._fetchone(
            "SELECT selected_channel FROM users WHERE user_id = ?", (user_id,)
        )
        return row["selected_channel"] if row else None

    async def select_channel(self, user_id: int, chat_id: int) -> None:
        await self._write(
            """
            INSERT INTO users (user_id, selected_channel) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET selected_channel = excluded.selected_channel
            """,
            (user_id, chat_id),
        )

    # ---------- posts ----------

    async def add_post(
        self,
        channel_id: int,
        user_id: int,
        from_chat_id: int,
        message_ids: list[int],
        summary: str,
        publish_at: int,
    ) -> int:
        post_id = await self._write(
            """
            INSERT INTO posts
                (channel_id, user_id, from_chat_id, message_ids, summary, publish_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                user_id,
                from_chat_id,
                json.dumps(message_ids),
                summary,
                publish_at,
                int(time.time()),
            ),
        )
        assert post_id is not None
        return post_id

    async def get_post(self, post_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))

    async def last_pending_time(self, channel_id: int) -> int | None:
        row = await self._fetchone(
            "SELECT MAX(publish_at) AS t FROM posts WHERE channel_id = ? AND status = 'pending'",
            (channel_id,),
        )
        return row["t"] if row else None

    async def pending_posts(self, channel_id: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT * FROM posts WHERE channel_id = ? AND status = 'pending'
            ORDER BY publish_at, id
            """,
            (channel_id,),
        )

    async def update_post_times(self, items: list[tuple[int, int]]) -> None:
        """items: [(post_id, publish_at), ...]"""
        await self.conn.executemany(
            "UPDATE posts SET publish_at = ? WHERE id = ? AND status = 'pending'",
            [(ts, pid) for pid, ts in items],
        )
        await self.conn.commit()

    async def due_posts(self, now: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT p.* FROM posts p JOIN channels c ON c.chat_id = p.channel_id
            WHERE p.status = 'pending' AND p.publish_at <= ? AND c.active = 1
            ORDER BY p.publish_at, p.id
            """,
            (now,),
        )

    async def mark_post(self, post_id: int, status: str, error: str | None = None) -> None:
        await self._write(
            "UPDATE posts SET status = ?, error = ? WHERE id = ?", (status, error, post_id)
        )

    async def delete_pending_post(self, post_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM posts WHERE id = ? AND status = 'pending'", (post_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0
