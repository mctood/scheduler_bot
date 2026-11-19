"""Чистая логика расчёта времени публикаций (без Telegram и БД)."""

import re

MINUTE = 60
MAX_INTERVAL_MINUTES = 30 * 24 * 60  # 30 дней


def _ceil_minute(ts: int) -> int:
    return -(-ts // MINUTE) * MINUTE


def next_slot(
    last_pending: int | None,
    last_published: int | None,
    interval: int,
    now: int,
) -> int:
    """Время для нового поста.

    Новый пост встаёт на ``interval`` секунд позже самого последнего поста в очереди.
    Если очередь пуста — отсчёт идёт от последней публикации. Если и так получается
    время в прошлом (или публикаций ещё не было) — пост ставится на ``now + interval``.
    """
    base = last_pending if last_pending is not None else last_published
    if base is not None:
        candidate = base + interval
        if candidate > now:
            return candidate
    return _ceil_minute(now + interval)


def respace(times: list[int], interval: int) -> list[int]:
    """Пересчитать очередь: первый пост остаётся на месте, остальные идут
    с шагом ``interval`` после него. ``times`` должны быть отсортированы."""
    if not times:
        return []
    first = times[0]
    return [first + i * interval for i in range(len(times))]


_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ч|час|часа|часов|h|hr|hours?)?")
_MINUTES_RE = re.compile(r"(\d+)\s*(?:м|мин|минут|минуты|m|min|minutes?)")
_HM_RE = re.compile(
    r"(\d+)\s*(?:ч|час|часа|часов|h)\s*(\d+)\s*(?:м|мин|минут|минуты|m|min)?"
)


def parse_interval(text: str) -> int | None:
    """Разобрать интервал из текста пользователя. Возвращает минуты или None.

    Примеры: ``2``, ``1.5``, ``1,5``, ``3ч``, ``90м``, ``1ч 30м``.
    """
    t = text.strip().lower().replace(",", ".")
    minutes: float | None = None
    if m := _HM_RE.fullmatch(t):
        minutes = int(m.group(1)) * 60 + int(m.group(2))
    elif m := _MINUTES_RE.fullmatch(t):
        minutes = int(m.group(1))
    elif m := _HOURS_RE.fullmatch(t):
        minutes = float(m.group(1)) * 60
    if minutes is None:
        return None
    minutes = round(minutes)
    if minutes < 1 or minutes > MAX_INTERVAL_MINUTES:
        return None
    return minutes


def format_interval(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"
