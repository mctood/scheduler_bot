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


# ---------- ночное время и первый пост ----------

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from bot.scheduling import Night, parse_datetime, parse_night, skip_night  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
NIGHT = Night(20 * 60, 8 * 60)


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=MSK).timestamp())


def test_parse_night():
    assert parse_night("20:00-08:00") == NIGHT
    assert parse_night("20-8") == NIGHT
    assert parse_night("22:30 – 7") == Night(22 * 60 + 30, 7 * 60)
    assert parse_night("1-6") == Night(60, 360)
    assert parse_night("8-8") is None
    assert parse_night("abc") is None
    assert str(NIGHT) == "20:00–08:00"


def test_skip_night_example_from_task():
    # Последний пост 19:00, ночь 20–08, интервал 4ч -> 08:00 следующего дня
    assert next_slot(at(19, 19), None, 4 * H, now=at(19, 12), night=NIGHT, tz=MSK) == at(20, 8)
    # Интервал 14ч -> 09:00 (позже конца ночи)
    assert next_slot(at(19, 19), None, 14 * H, now=at(19, 12), night=NIGHT, tz=MSK) == at(20, 9)


def test_skip_night_edges():
    assert skip_night(at(19, 19, 59), NIGHT, MSK) == at(19, 19, 59)
    assert skip_night(at(19, 20), NIGHT, MSK) == at(20, 8)
    assert skip_night(at(20, 3), NIGHT, MSK) == at(20, 8)  # после полуночи — в тот же день
    assert skip_night(at(20, 8), NIGHT, MSK) == at(20, 8)
    day_night = Night(13 * 60, 14 * 60)  # окно без перехода через полночь
    assert skip_night(at(19, 13, 30), day_night, MSK) == at(19, 14)
    assert skip_night(at(19, 12), None, MSK) == at(19, 12)


def test_respace_with_night_and_first():
    times = [at(19, 16), at(19, 18), at(19, 20), at(20, 8)]
    assert respace(times, 2 * H, night=NIGHT, tz=MSK) == [
        at(19, 16), at(19, 18), at(20, 8), at(20, 10)
    ]
    assert respace(times, 3 * H, night=NIGHT, tz=MSK, first=at(19, 10)) == [
        at(19, 10), at(19, 13), at(19, 16), at(19, 19)
    ]


def test_parse_datetime():
    now = at(19, 12)
    assert parse_datetime("14:30", now, MSK) == at(19, 14, 30)
    assert parse_datetime("09:00", now, MSK) == at(20, 9)  # уже прошло -> завтра
    assert parse_datetime("21.11 14:30", now, MSK) == at(21, 14, 30)
    assert parse_datetime("21.11.2026 14:30", now, MSK) == at(21, 14, 30)
    assert parse_datetime("25:00", now, MSK) is None
    assert parse_datetime("31.02 10:00", now, MSK) is None
    assert parse_datetime("завтра", now, MSK) is None


def test_service_first_and_night(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "n.db"))
        await db.connect()
        clock = [at(19, 9)]
        sched = Scheduler(db, tz=MSK, clock=lambda: clock[0])
        ch = -1002
        await db.upsert_channel(ch, "Test", None, 120)
        for i in range(4):
            await sched.add_post(ch, 1, 1, [i], "p")
        # 11, 13, 15, 17
        posts = await sched.set_first_time(ch, at(19, 15))
        assert [p["publish_at"] for p in posts] == [at(19, 15), at(19, 17), at(19, 19), at(19, 21)]
        posts = await sched.set_night(ch, NIGHT)
        assert [p["publish_at"] for p in posts] == [at(19, 15), at(19, 17), at(19, 19), at(20, 8)]
        _, t = await sched.add_post(ch, 1, 1, [9], "p")
        assert t == at(20, 10)
        posts = await sched.set_night(ch, None)
        assert posts[-2]["publish_at"] == at(19, 21)
        await db.close()

    asyncio.run(scenario())
