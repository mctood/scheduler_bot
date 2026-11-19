import asyncio
import json

from bot.db import Database
from bot.publisher import publish_due
from bot.scheduling import format_interval, next_slot, parse_interval, respace
from bot.service import Scheduler

H = 3600
T13 = 1_700_000_000 // H * H  # "13:00" — целый час


def test_next_slot_after_last_pending():
    assert next_slot(T13 + 2 * H, None, H, now=T13) == T13 + 3 * H


def test_next_slot_empty_queue_uses_last_published():
    assert next_slot(None, T13, 2 * H, now=T13 + 600) == T13 + 2 * H


def test_next_slot_empty_queue_old_publication():
    now = T13 + 10 * H + 17
    assert next_slot(None, T13, H, now=now) == T13 + 11 * H + 60  # округление до минуты


def test_next_slot_nothing_at_all():
    assert next_slot(None, None, H, now=T13) == T13 + H


def test_respace_example_from_task():
    # 13:00, 14:00, 15:00 -> интервал 2ч -> 13:00, 15:00, 17:00
    assert respace([T13, T13 + H, T13 + 2 * H], 2 * H) == [T13, T13 + 2 * H, T13 + 4 * H]
    assert respace([], H) == []


def test_parse_interval():
    assert parse_interval("2") == 120
    assert parse_interval("1.5") == 90
    assert parse_interval("1,5") == 90
    assert parse_interval("3ч") == 180
    assert parse_interval("3 часа") == 180
    assert parse_interval("90м") == 90
    assert parse_interval("45 min") == 45
    assert parse_interval("1ч 30м") == 90
    assert parse_interval("0") is None
    assert parse_interval("abc") is None
    assert format_interval(90) == "1 ч 30 мин"
    assert format_interval(120) == "2 ч"
    assert format_interval(15) == "15 мин"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.sent.append((chat_id, [message_id]))

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        self.sent.append((chat_id, list(message_ids)))

    async def send_message(self, *a, **kw):
        pass


def test_queue_flow(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "t.db"))
        await db.connect()
        clock = [T13 - H]
        sched = Scheduler(db, clock=lambda: clock[0])
        ch = -1001
        await db.upsert_channel(ch, "Test", None, 60)

        times = [(await sched.add_post(ch, 1, 1, [i], f"p{i}"))[1] for i in (1, 2, 3)]
        assert times == [T13, T13 + H, T13 + 2 * H]

        # Смена интервала сдвигает очередь начиная с первого
        posts = await sched.set_interval(ch, 120)
        assert [p["publish_at"] for p in posts] == [T13, T13 + 2 * H, T13 + 4 * H]

        # Новый пост — через 2 часа после последнего
        _, t4 = await sched.add_post(ch, 1, 1, [4, 5], "album")
        assert t4 == T13 + 6 * H

        # Удаление + выравнивание
        await db.delete_pending_post(posts[1]["id"])
        posts = await sched.realign(ch)
        assert [p["publish_at"] for p in posts] == [T13, T13 + 2 * H, T13 + 4 * H]

        # Публикация наступивших постов
        bot = FakeBot()
        clock[0] = T13 + 5
        await publish_due(bot, db, sched)
        assert bot.sent == [(ch, [1])]
        assert len(await db.pending_posts(ch)) == 2
        clock[0] = T13 + 4 * H
        await publish_due(bot, db, sched)
        assert bot.sent[-1] == (ch, [4, 5])
        assert json.loads((await db.get_post(4))["message_ids"]) == [4, 5]

        # Очередь пуста -> отсчёт от последней публикации
        _, t5 = await sched.add_post(ch, 1, 1, [9], "x")
        assert t5 == T13 + 6 * H
        await db.close()

    asyncio.run(scenario())
