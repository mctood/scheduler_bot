import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject

from .config import Settings, load_settings
from .db import Database
from .handlers import router
from .publisher import publisher_loop
from .service import Scheduler


class AccessMiddleware(BaseMiddleware):
    """Если задан ALLOWED_USERS — пускаем только этих пользователей."""

    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id in self.allowed:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type == "private":
            await event.answer("⛔ Нет доступа к этому боту.")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        return None


async def run(settings: Settings) -> None:
    db = Database(settings.db_path)
    await db.connect()
    scheduler = Scheduler(db, tz=settings.timezone)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    if settings.allowed_users:
        access = AccessMiddleware(settings.allowed_users)
        dp.message.outer_middleware(access)
        dp.callback_query.outer_middleware(access)
    dp.include_router(router)
    dp["db"] = db
    dp["scheduler"] = scheduler
    dp["settings"] = settings

    await bot.set_my_commands(
        [
            BotCommand(command="channels", description="Выбрать канал"),
            BotCommand(command="interval", description="Интервал между постами"),
            BotCommand(command="queue", description="Очередь публикаций"),
            BotCommand(command="first", description="Время первого поста"),
            BotCommand(command="night", description="Ночное время без публикаций"),
            BotCommand(command="add", description="Добавить канал вручную"),
            BotCommand(command="help", description="Помощь"),
        ]
    )

    publisher = asyncio.create_task(publisher_loop(bot, db, scheduler))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await bot.session.close()
        await db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run(load_settings()))


if __name__ == "__main__":
    main()
