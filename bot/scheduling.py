"""Чистая логика расчёта времени публикаций (без Telegram и БД)."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

MINUTE = 60
MAX_INTERVAL_MINUTES = 30 * 24 * 60  # 30 дней


@dataclass(frozen=True)
class Night:
    """Ночное окно, когда публиковать нельзя. Минуты от начала суток, местное время.
    ``start > end`` — окно через полночь (например 20:00–08:00)."""

    start: int
    end: int

    def contains(self, minute_of_day: int) -> bool:
        if self.start < self.end:
            return self.start <= minute_of_day < self.end
        return minute_of_day >= self.start or minute_of_day < self.end

    def __str__(self) -> str:
        return f"{format_hm(self.start)}–{format_hm(self.end)}"


def format_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _ceil_minute(ts: int) -> int:
    return -(-ts // MINUTE) * MINUTE


def skip_night(ts: int, night: Night | None, tz: tzinfo = timezone.utc) -> int:
    """Если ``ts`` попадает в ночное окно — перенести на конец этого окна."""
    if night is None:
        return ts
    local = datetime.fromtimestamp(ts, tz)
    if not night.contains(local.hour * 60 + local.minute):
        return ts
    end_time = time(night.end // 60, night.end % 60)
    day: date = local.date()
    target = datetime.combine(day, end_time, tzinfo=tz)
    if target <= local:
        target = datetime.combine(day + timedelta(days=1), end_time, tzinfo=tz)
    return int(target.timestamp())


def next_slot(
    last_pending: int | None,
    last_published: int | None,
    interval: int,
    now: int,
    night: Night | None = None,
    tz: tzinfo = timezone.utc,
) -> int:
    """Время для нового поста.

    Новый пост встаёт на ``interval`` секунд позже самого последнего поста в очереди.
    Если очередь пуста — отсчёт идёт от последней публикации. Если и так получается
    время в прошлом (или публикаций ещё не было) — пост ставится на ``now + interval``.
    Если время попадает в ночное окно — пост переносится на конец ночи.
    """
    base = last_pending if last_pending is not None else last_published
    candidate = None
    if base is not None and base + interval > now:
        candidate = base + interval
    if candidate is None:
        candidate = _ceil_minute(now + interval)
    return skip_night(candidate, night, tz)


def respace(
    times: list[int],
    interval: int,
    night: Night | None = None,
    tz: tzinfo = timezone.utc,
    first: int | None = None,
) -> list[int]:
    """Пересчитать очередь: первый пост остаётся на месте (или ставится на ``first``),
    каждый следующий — через ``interval`` после предыдущего с учётом ночного окна.
    ``times`` должны быть отсортированы."""
    if not times:
        return []
    result = [times[0] if first is None else first]
    for _ in times[1:]:
        result.append(skip_night(result[-1] + interval, night, tz))
    return result


_NIGHT_RE = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?")


def parse_night(text: str) -> Night | None:
    """``20-8``, ``20:00-08:00``, ``22:30 – 7``. Возвращает None, если не разобрать."""
    m = _NIGHT_RE.fullmatch(text.strip())
    if not m:
        return None
    sh, sm, eh, em = (int(x) if x else 0 for x in m.groups())
    if sh > 23 or eh > 24 or sm > 59 or em > 59:
        return None
    start, end = sh * 60 + sm, (eh * 60 + em) % (24 * 60)
    if start == end:
        return None
    return Night(start, end)


_DT_RE = re.compile(
    r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+)?(\d{1,2})[:.](\d{2})"
)


def parse_datetime(text: str, now: int, tz: tzinfo) -> int | None:
    """``14:30`` — ближайшие 14:30 в будущем; ``21.11 14:30`` или ``21.11.2026 14:30``.
    Возвращает unix-время или None."""
    m = _DT_RE.fullmatch(text.strip())
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    hour, minute = int(hour), int(minute)
    if hour > 23 or minute > 59:
        return None
    now_local = datetime.fromtimestamp(now, tz)
    try:
        if day is None:
            dt = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt.timestamp() <= now:
                dt = datetime.combine(
                    dt.date() + timedelta(days=1), time(hour, minute), tzinfo=tz
                )
        else:
            y = int(year) if year else now_local.year
            if y < 100:
                y += 2000
            dt = datetime(y, int(month), int(day), hour, minute, tzinfo=tz)
            if not year and dt.timestamp() <= now:
                dt = dt.replace(year=y + 1)
    except ValueError:
        return None
    return int(dt.timestamp())


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
