import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    timezone: ZoneInfo
    default_interval_minutes: int
    db_path: str
    allowed_users: frozenset[int]


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN (см. .env.example)")

    hours = float(os.getenv("DEFAULT_INTERVAL_HOURS", "1").replace(",", "."))
    allowed = frozenset(
        int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
    )

    return Settings(
        bot_token=token,
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        default_interval_minutes=max(1, round(hours * 60)),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        allowed_users=allowed,
    )
