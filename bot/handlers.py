import asyncio
import html
import json
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
)

from .config import Settings
from .db import Database
from .scheduling import format_interval, parse_interval
from .service import Scheduler

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

QUEUE_PAGE_LIMIT = 40
ALBUM_WAIT_SECONDS = 1.5

HELP = (
    "👋 Я расставляю посты в канале по расписанию.\n\n"
    "<b>Как пользоваться</b>\n"
    "1. Добавьте меня администратором канала с правом публикации сообщений.\n"
    "2. Выберите канал: /channels\n"
    "3. Задайте интервал: /interval 2 (часы; можно 1.5, 90м, 1ч 30м)\n"
    "4. Присылайте мне посты — текст, фото, видео, альбомы, пересланные сообщения. "
    "Каждый новый пост встаёт в очередь на N часов позже последнего.\n\n"
    "<b>Команды</b>\n"
    "/channels — выбрать канал\n"
    "/add — добавить канал вручную\n"
    "/interval — посмотреть или изменить интервал "
    "(очередь пересчитается начиная с первого поста)\n"
    "/queue — очередь публикаций: просмотр, удаление, выравнивание\n"
    "/cancel — отменить текущее действие\n\n"
    "⚠️ Не удаляйте посты из чата со мной до публикации — я копирую их отсюда."
)


class ChannelCb(CallbackData, prefix="ch"):
    chat_id: int


class PostCb(CallbackData, prefix="post"):
    action: str  # view | del | qdel
    post_id: int


class QueueCb(CallbackData, prefix="q"):
    action: str  # refresh | realign
    chat_id: int


class AddChannel(StatesGroup):
    waiting = State()


class SetInterval(StatesGroup):
    waiting = State()


# ---------- helpers ----------


def fmt_ts(ts: int, settings: Settings, with_year: bool = False) -> str:
    pattern = "%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M"
    return datetime.fromtimestamp(ts, settings.timezone).strftime(pattern)


async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def bot_can_post(bot: Bot, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError:
        return False
    if isinstance(member, ChatMemberOwner):
        return True
    return isinstance(member, ChatMemberAdministrator) and bool(member.can_post_messages)


async def user_channels(bot: Bot, db: Database, user_id: int) -> list:
    result = []
    for ch in await db.list_channels():
        if await is_user_admin(bot, ch["chat_id"], user_id):
            result.append(ch)
    return result


async def current_channel(bot: Bot, db: Database, user_id: int):
    """Выбранный пользователем канал, если он всё ещё доступен."""
    chat_id = await db.get_selected_channel(user_id)
    if chat_id is None:
        return None
    channel = await db.get_channel(chat_id)
    if channel is None or not channel["active"]:
        return None
    if not await is_user_admin(bot, chat_id, user_id):
        return None
    return channel


async def require_channel(message: Message, bot: Bot, db: Database):
    channel = await current_channel(bot, db, message.from_user.id)
    if channel is None:
        await message.answer("Сначала выберите канал: /channels")
    return channel


def make_summary(messages: list[Message]) -> str:
    first = messages[0]
    text = next((m.text or m.caption for m in messages if m.text or m.caption), "") or ""
    if len(messages) > 1:
        kind = f"🗂 альбом ({len(messages)})"
    else:
        kind = {
            "text": "📝",
            "photo": "🖼",
            "video": "🎬",
            "animation": "🎞",
            "document": "📎",
            "audio": "🎵",
            "voice": "🎤",
            "video_note": "⏺",
            "sticker": "🏷 стикер",
            "poll": "📊 опрос",
        }.get(first.content_type, f"📦 {first.content_type}")
    snippet = re.sub(r"\s+", " ", text).strip()
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"
    return f"{kind} {snippet}".strip()


def channel_list_kb(channels, selected: int | None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if ch["chat_id"] == selected else "") + ch["title"],
                callback_data=ChannelCb(chat_id=ch["chat_id"]).pack(),
            )
        ]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_view(channel, posts, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    title = html.escape(channel["title"])
    lines = [
        f"📋 Очередь «{title}»",
        f"Интервал: {format_interval(channel['interval_minutes'])}",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if not posts:
        lines.append("Очередь пуста. Пришлите пост — он встанет в очередь.")
    for i, p in enumerate(posts[:QUEUE_PAGE_LIMIT], start=1):
        when = fmt_ts(p["publish_at"], settings)
        lines.append(f"{i}. <b>{when}</b> — {html.escape(p['summary'])}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👁 {i} · {when}",
                    callback_data=PostCb(action="view", post_id=p["id"]).pack(),
                ),
                InlineKeyboardButton(
                    text=f"🗑 {i}", callback_data=PostCb(action="qdel", post_id=p["id"]).pack()
                ),
            ]
        )
    if len(posts) > QUEUE_PAGE_LIMIT:
        lines.append(f"…и ещё {len(posts) - QUEUE_PAGE_LIMIT}")
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Выровнять",
                callback_data=QueueCb(action="realign", chat_id=channel["chat_id"]).pack(),
            ),
            InlineKeyboardButton(
                text="↻ Обновить",
                callback_data=QueueCb(action="refresh", chat_id=channel["chat_id"]).pack(),
            ),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- bot added / removed from channels ----------


@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot, db: Database, settings: Settings):
    status = event.new_chat_member.status
    chat = event.chat
    if status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await db.upsert_channel(
            chat.id, chat.title or str(chat.id), chat.username, settings.default_interval_minutes
        )
        log.info("Bot is admin in channel %s (%s)", chat.id, chat.title)
        user = event.from_user
        if user and not user.is_bot and await is_user_admin(bot, chat.id, user.id):
            if await db.get_selected_channel(user.id) is None:
                await db.select_channel(user.id, chat.id)
            note = ""
            if not await bot_can_post(bot, chat.id):
                note = "\n⚠️ Но у меня нет права публиковать сообщения — включите его."
            try:
                await bot.send_message(
                    user.id,
                    f"✅ Канал «{html.escape(chat.title or '')}» подключён.{note}\n"
                    "Выбрать канал для постов: /channels",
                )
            except TelegramAPIError:
                pass  # пользователь ещё не запускал бота
    else:
        await db.deactivate_channel(chat.id)
        log.info("Bot removed from channel %s", chat.id)


# ---------- commands ----------


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("channels"))
async def cmd_channels(message: Message, bot: Bot, db: Database, state: FSMContext):
    await state.clear()
    channels = await user_channels(bot, db, message.from_user.id)
    selected = await db.get_selected_channel(message.from_user.id)
    if not channels:
        text = (
            "Пока нет каналов, где мы оба администраторы.\n"
            "Добавьте меня администратором канала (с правом публикации) — "
            "он появится здесь автоматически. Или нажмите «Добавить канал»."
        )
    else:
        text = "Выберите канал, в который будут ставиться посты:"
    await message.answer(text, reply_markup=channel_list_kb(channels, selected))


@router.callback_query(ChannelCb.filter())
async def cb_select_channel(
    query: CallbackQuery, callback_data: ChannelCb, bot: Bot, db: Database, settings: Settings
):
    chat_id = callback_data.chat_id
    channel = await db.get_channel(chat_id)
    if channel is None or not channel["active"]:
        await query.answer("Канал недоступен", show_alert=True)
        return
    if not await is_user_admin(bot, chat_id, query.from_user.id):
        await query.answer("Вы не администратор этого канала", show_alert=True)
        return
    await db.select_channel(query.from_user.id, chat_id)
    warn = "" if await bot_can_post(bot, chat_id) else "\n⚠️ У меня нет права публикации в этом канале!"
    pending = await db.pending_posts(chat_id)
    await query.answer("Канал выбран")
    await safe_edit(
        query.message,
        f"✅ Выбран канал «{html.escape(channel['title'])}»\n"
        f"Интервал: {format_interval(channel['interval_minutes'])}\n"
        f"В очереди: {len(pending)}{warn}\n\n"
        "Присылайте посты — я поставлю их в очередь.",
        None,
    )


@router.callback_query(F.data == "add_channel")
async def cb_add_channel(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await start_add_channel(query.message, state)


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await start_add_channel(message, state)


async def start_add_channel(message: Message, state: FSMContext):
    await state.set_state(AddChannel.waiting)
    await message.answer(
        "Сначала добавьте меня администратором канала с правом публикации сообщений.\n"
        "Затем перешлите сюда любой пост из канала или пришлите его @username / ссылку "
        "t.me/… / числовой id.\n\n/cancel — отмена"
    )


def _channel_ref(message: Message) -> int | str | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id
    if message.forward_from_chat:  # на всякий случай для старых клиентов
        return message.forward_from_chat.id
    text = (message.text or "").strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if m := re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?", text):
        return "@" + m.group(1)
    if re.fullmatch(r"@[A-Za-z0-9_]{4,}", text):
        return text
    return None


@router.message(StateFilter(AddChannel.waiting))
async def on_add_channel(
    message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings
):
    ref = _channel_ref(message)
    if ref is None:
        await message.answer("Не понял, какой это канал. Перешлите пост из канала или /cancel")
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError:
        await message.answer(
            "Не могу найти канал. Убедитесь, что я добавлен в него администратором."
        )
        return
    if chat.type != ChatType.CHANNEL:
        await message.answer("Это не канал.")
        return
    if not await bot_can_post(bot, chat.id):
        await message.answer(
            "Я не администратор этого канала или у меня нет права публикации сообщений."
        )
        return
    if not await is_user_admin(bot, chat.id, message.from_user.id):
        await message.answer("Вы не администратор этого канала.")
        return
    await db.upsert_channel(
        chat.id, chat.title or str(chat.id), chat.username, settings.default_interval_minutes
    )
    await db.select_channel(message.from_user.id, chat.id)
    await state.clear()
    channel = await db.get_channel(chat.id)
    await message.answer(
        f"✅ Канал «{html.escape(channel['title'])}» добавлен и выбран.\n"
        f"Интервал: {format_interval(channel['interval_minutes'])} — изменить: /interval"
    )


@router.message(Command("interval"))
async def cmd_interval(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
    db: Database,
    scheduler: Scheduler,
    settings: Settings,
):
    await state.clear()
    channel = await require_channel(message, bot, db)
    if channel is None:
        return
    if command.args:
        await apply_interval(message, channel, command.args, scheduler, settings)
        return
    await state.set_state(SetInterval.waiting)
    await state.update_data(chat_id=channel["chat_id"])
    await message.answer(
        f"Текущий интервал в «{html.escape(channel['title'])}»: "
        f"<b>{format_interval(channel['interval_minutes'])}</b>\n\n"
        "Пришлите новый интервал в часах (например <code>2</code>, <code>1.5</code>, "
        "<code>90м</code>, <code>1ч 30м</code>).\n"
        "Все посты в очереди будут пересчитаны: первый останется на месте, "
        "остальные — с новым шагом.\n\n/cancel — отмена"
    )


@router.message(StateFilter(SetInterval.waiting), F.text)
async def on_interval_value(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    scheduler: Scheduler,
    settings: Settings,
):
    data = await state.get_data()
    channel = await db.get_channel(data.get("chat_id", 0))
    if channel is None or not await is_user_admin(bot, channel["chat_id"], message.from_user.id):
        await state.clear()
        await message.answer("Канал недоступен. Выберите канал: /channels")
        return
    if await apply_interval(message, channel, message.text, scheduler, settings):
        await state.clear()


async def apply_interval(
    message: Message, channel, raw: str, scheduler: Scheduler, settings: Settings
) -> bool:
    minutes = parse_interval(raw)
    if minutes is None:
        await message.answer(
            "Не понял интервал. Примеры: <code>2</code>, <code>1.5</code>, "
            "<code>90м</code>, <code>1ч 30м</code>"
        )
        return False
    posts = await scheduler.set_interval(channel["chat_id"], minutes)
    channel = await scheduler.db.get_channel(channel["chat_id"])
    text = f"✅ Интервал: <b>{format_interval(minutes)}</b>"
    if posts:
        text += f"\nОчередь пересчитана ({len(posts)} шт.)."
    queue_text, kb = queue_view(channel, posts, settings)
    await message.answer(text + "\n\n" + queue_text, reply_markup=kb)
    return True


@router.message(Command("queue"))
async def cmd_queue(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext):
    await state.clear()
    channel = await require_channel(message, bot, db)
    if channel is None:
        return
    text, kb = queue_view(channel, await db.pending_posts(channel["chat_id"]), settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(QueueCb.filter())
async def cb_queue(
    query: CallbackQuery,
    callback_data: QueueCb,
    bot: Bot,
    db: Database,
    scheduler: Scheduler,
    settings: Settings,
):
    chat_id = callback_data.chat_id
    channel = await db.get_channel(chat_id)
    if channel is None or not await is_user_admin(bot, chat_id, query.from_user.id):
        await query.answer("Нет доступа к каналу", show_alert=True)
        return
    if callback_data.action == "realign":
        posts = await scheduler.realign(chat_id)
        await query.answer("Очередь выровнена")
    else:
        posts = await db.pending_posts(chat_id)
        await query.answer()
    text, kb = queue_view(channel, posts, settings)
    await safe_edit(query.message, text, kb)


@router.callback_query(PostCb.filter())
async def cb_post(
    query: CallbackQuery, callback_data: PostCb, bot: Bot, db: Database, settings: Settings
):
    post = await db.get_post(callback_data.post_id)
    if post is None or post["status"] != "pending":
        await query.answer("Пост уже опубликован или удалён", show_alert=True)
        return
    if not await is_user_admin(bot, post["channel_id"], query.from_user.id):
        await query.answer("Нет доступа к каналу", show_alert=True)
        return

    if callback_data.action == "view":
        await query.answer()
        ids = json.loads(post["message_ids"])
        try:
            if len(ids) == 1:
                await bot.copy_message(query.from_user.id, post["from_chat_id"], ids[0])
            else:
                await bot.copy_messages(query.from_user.id, post["from_chat_id"], ids)
        except TelegramAPIError as e:
            await bot.send_message(
                query.from_user.id, f"Не удалось показать пост: {html.escape(str(e))}"
            )
            return
        await bot.send_message(
            query.from_user.id,
            f"⬆️ Будет опубликован {fmt_ts(post['publish_at'], settings, with_year=True)}",
        )
        return

    await db.delete_pending_post(post["id"])
    await query.answer("Пост удалён из очереди")
    if callback_data.action == "qdel":
        channel = await db.get_channel(post["channel_id"])
        text, kb = queue_view(channel, await db.pending_posts(post["channel_id"]), settings)
        await safe_edit(query.message, text, kb)
    else:
        await safe_edit(query.message, "❌ Пост удалён из очереди.", None)


# ---------- posts ----------

_albums: dict[tuple[int, str], list[Message]] = {}


@router.message(StateFilter(None))
async def on_post(
    message: Message, bot: Bot, db: Database, scheduler: Scheduler, settings: Settings
):
    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. /help")
        return

    if message.media_group_id:
        key = (message.chat.id, message.media_group_id)
        bucket = _albums.setdefault(key, [])
        bucket.append(message)
        if len(bucket) > 1:
            return  # альбом соберёт первый обработчик
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        messages = sorted(_albums.pop(key), key=lambda m: m.message_id)
    else:
        messages = [message]

    channel = await require_channel(messages[0], bot, db)
    if channel is None:
        return

    post_id, publish_at = await scheduler.add_post(
        channel_id=channel["chat_id"],
        user_id=message.from_user.id,
        from_chat_id=message.chat.id,
        message_ids=[m.message_id for m in messages],
        summary=make_summary(messages),
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Отменить", callback_data=PostCb(action="del", post_id=post_id).pack()
                )
            ]
        ]
    )
    await messages[0].reply(
        f"✅ Запланировано в «{html.escape(channel['title'])}» на "
        f"<b>{fmt_ts(publish_at, settings, with_year=True)}</b>",
        reply_markup=kb,
    )
