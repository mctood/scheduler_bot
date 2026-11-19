import asyncio
import html
import json
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from .db import Database
from .service import Scheduler

log = logging.getLogger(__name__)

POLL_SECONDS = 10


async def publish_due(bot: Bot, db: Database, scheduler: Scheduler) -> None:
    for post in await db.due_posts(scheduler.now()):
        ids: list[int] = json.loads(post["message_ids"])
        try:
            if len(ids) == 1:
                await bot.copy_message(post["channel_id"], post["from_chat_id"], ids[0])
            else:
                await bot.copy_messages(post["channel_id"], post["from_chat_id"], ids)
        except TelegramRetryAfter as e:
            log.warning("Flood control, waiting %s s", e.retry_after)
            await asyncio.sleep(e.retry_after)
            return  # пост остаётся в очереди, попробуем на следующем круге
        except (TelegramNetworkError, TelegramServerError) as e:
            log.warning("Temporary error while publishing post %s: %s", post["id"], e)
            return
        except TelegramAPIError as e:
            log.error("Failed to publish post %s: %s", post["id"], e)
            await db.mark_post(post["id"], "failed", str(e))
            await _notify(
                bot,
                post["user_id"],
                f"⚠️ Не удалось опубликовать пост #{post['id']} "
                f"({html.escape(post['summary'])}):\n<code>{html.escape(str(e))}</code>",
            )
            continue

        await db.mark_post(post["id"], "published")
        await db.set_last_published(post["channel_id"], scheduler.now())
        log.info("Published post %s to %s", post["id"], post["channel_id"])


async def _notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        pass


async def publisher_loop(bot: Bot, db: Database, scheduler: Scheduler) -> None:
    while True:
        try:
            await publish_due(bot, db, scheduler)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Publisher iteration failed")
        await asyncio.sleep(POLL_SECONDS)
