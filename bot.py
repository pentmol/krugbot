import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db import DB
from dotenv import load_dotenv


# force=True so logs show even if something configured logging earlier
logging.basicConfig(level=logging.INFO, force=True)
log = logging.getLogger("krugbot")

router = Router()

ADMIN_CHAT_ID = 1962773771


class RewriteStates(StatesGroup):
    waiting_new_video = State()


class ProfileStates(StatesGroup):
    waiting_age = State()
    waiting_gender = State()
    waiting_looking_for = State()
    waiting_about = State()


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Искать")],
            [KeyboardButton(text="⭕️ Мой кружок")],
            [KeyboardButton(text="🚫 Завершить чат")],
        ],
        resize_keyboard=True,
    )

def kb_gender_inline(kind: str) -> InlineKeyboardMarkup:
    # kind: "gender" or "looking"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="М", callback_data=f"{kind}:M"),
                InlineKeyboardButton(text="Ж", callback_data=f"{kind}:F"),
            ]
        ]
    )


def kb_profile_edit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить", callback_data="edit_profile")],
        ]
    )


def kb_watch() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Смотреть", callback_data="watch")],
        ]
    )


def kb_ready(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Смотреть", callback_data="watch")],
            [InlineKeyboardButton(text="Реферальная система", callback_data=f"referral:{user_id}")],
        ]
    )


def kb_video(video_id: int, owner_user_id: int, viewer_user_id: int, video_message_id: int | None = None) -> InlineKeyboardMarkup:
    # Оставляем только одну кнопку жалобы.
    # Лайков/дизлайков в карточке профиля нет.
    complaint_or_block = (
        InlineKeyboardButton(text="Заблокировать", callback_data=f"block:{owner_user_id}")
        if viewer_user_id == ADMIN_CHAT_ID
        else InlineKeyboardButton(text="Жалоба", callback_data=f"complaint:{video_id}:{video_message_id or 0}")
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать чат", callback_data=f"chat_start:{owner_user_id}")],
            [
                InlineKeyboardButton(text="Следующее", callback_data="next"),
                complaint_or_block,
            ],
        ]
    )


def format_profile_card(profile: dict | None) -> str:
    if not profile:
        return "Информация о пользователе недоступна."
    return (
        f"Возраст: {profile.get('age')}\n"
        f"Пол: {profile.get('gender')}\n"
        f"Ищет: {profile.get('looking_for')}\n"
        f"О себе: {profile.get('about')}"
    )


def kb_my_video(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перезаписать", callback_data="rewrite")],
            [
                InlineKeyboardButton(text="Удалить кружок", callback_data="delete_video"),
                InlineKeyboardButton(text="Реферальная система", callback_data=f"referral:{user_id}"),
            ],
        ]
    )

def kb_admin_ban(owner_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Забанить пользователя", callback_data=f"admin_ban:{owner_user_id}")],
        ]
    )

def format_user_ref(user_id: int, username: str | None) -> str:
    if username:
        return f"{user_id} (@{username})"
    return str(user_id)


_touched_users: dict[int, str | None] = {}

async def touch_user(db: DB, tg_user) -> None:
    # Avoid a Supabase write on every Telegram update.
    # Refresh the DB only on first contact or when the username changes.
    current_username = tg_user.username
    if _touched_users.get(tg_user.id, object()) != current_username:
        await db.ensure_user(tg_user.id, current_username)
        _touched_users[tg_user.id] = current_username


async def guard_banned_message(message: Message, db: DB) -> bool:
    user_id = message.from_user.id
    if not await db.is_banned(user_id):
        return False
    if not await db.banned_notified(user_id):
        await message.answer("Ты забанен(а) и не можешь пользоваться ботом.")
        await db.set_banned_notified(user_id)
    return True


async def guard_banned_callback(cb: CallbackQuery, db: DB) -> bool:
    if not await db.is_banned(cb.from_user.id):
        return False
    await cb.answer()
    return True


async def continue_after_start_gate(message: Message, db: DB, state: FSMContext) -> None:
    user_id = message.from_user.id
    if not await db.profile_complete(user_id):
        await state.set_state(ProfileStates.waiting_age)
        await message.answer(
            "Привет! Давай заполним профиль.\nСколько тебе лет? (числом) (18-100)",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start")]],
                resize_keyboard=True,
            ),
        )
        return

    if await db.user_has_video(user_id):
        await message.answer(
            "Ты уже отправлял(а) кружок. Теперь можешь смотреть чужие.",
            reply_markup=main_kb(),
        )
        await message.answer("Нажми «Искать» или кнопку «Смотреть».", reply_markup=kb_watch())
        return

    await message.answer(
        "Профиль готов. Для начала поиска отправь свой кружок (video note).",
        reply_markup=main_kb(),
    )


@router.message(CommandStart())
async def start(message: Message, db: DB, state: FSMContext) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return

    await continue_after_start_gate(message, db, state)


@router.message(ProfileStates.waiting_age)
async def prof_age(message: Message, db: DB, state: FSMContext) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if not message.text:
        await message.answer("Напиши возраст числом.")
        return
    try:
        age = int(message.text.strip())
    except Exception:
        await message.answer("Напиши возраст числом.")
        return
    if age < 18 or age > 100:
        await message.answer("Укажи возраст в диапазоне 18-100.")
        return

    await state.update_data(age=age)
    await state.set_state(ProfileStates.waiting_gender)
    await message.answer("Укажи свой пол:", reply_markup=kb_gender_inline("gender"))


@router.callback_query(F.data.in_(["gender:M", "gender:F"]))
async def cb_prof_gender(cb: CallbackQuery, state: FSMContext, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await state.get_state() != ProfileStates.waiting_gender.state:
        await cb.answer()
        return

    gender = "М" if cb.data.endswith(":M") else "Ж"
    await state.update_data(gender=gender)
    await state.set_state(ProfileStates.waiting_looking_for)
    await cb.answer()
    await cb.message.answer("Какой пол ты ищешь?", reply_markup=kb_gender_inline("looking"))


@router.callback_query(F.data.in_(["looking:M", "looking:F"]))
async def cb_prof_looking_for(cb: CallbackQuery, state: FSMContext, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await state.get_state() != ProfileStates.waiting_looking_for.state:
        await cb.answer()
        return

    looking_for = "М" if cb.data.endswith(":M") else "Ж"
    await state.update_data(looking_for=looking_for)
    await state.set_state(ProfileStates.waiting_about)
    await cb.answer()
    await cb.message.answer(
        "Напиши о себе (например: кого ты ищешь или чем увлекаешься).",
        reply_markup=main_kb(),
    )


@router.message(ProfileStates.waiting_about)
async def prof_about(message: Message, db: DB, state: FSMContext) -> None:
    user_id = message.from_user.id
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    about = (message.text or "").strip()
    if not about:
        await message.answer("Напиши пару слов о себе текстом.")
        return
    if len(about) > 800:
        await message.answer("Слишком длинно. Напиши покороче (до 800 символов).")
        return

    data = await state.get_data()
    await db.set_profile(
        user_id,
        age=int(data["age"]),
        gender=str(data["gender"]),
        looking_for=str(data["looking_for"]),
        about=about,
    )
    await state.clear()
    if await db.user_has_video(user_id):
        await message.answer(
            "Профиль создан.\nУ тебя уже есть кружок — теперь можешь искать.",
            reply_markup=main_kb(),
        )
        await message.answer("Нажми «🔍 Искать» или кнопку «Смотреть».", reply_markup=kb_watch())
        return

    await message.answer(
        "Профиль создан.\nДля начала поиска отправь свой кружок (video note).",
        reply_markup=main_kb(),
    )


@router.message(F.video_note)
async def got_video_note(message: Message, db: DB, state: FSMContext) -> None:
    user_id = message.from_user.id
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    active_chat_user_id = await db.get_active_chat_user(user_id)
    if active_chat_user_id is not None:
        try:
            await message.bot.copy_message(
                active_chat_user_id,
                message.chat.id,
                message.message_id,
            )
        except TelegramBadRequest:
            await message.answer("Не удалось доставить кружок собеседнику.")
        return
    if not await db.profile_complete(user_id):
        await message.answer("Сначала заполни профиль через /start.")
        return
    # Don't allow changing video while user is in profile onboarding/edit flow.
    if (await state.get_state()) in (
        ProfileStates.waiting_age.state,
        ProfileStates.waiting_gender.state,
        ProfileStates.waiting_looking_for.state,
        ProfileStates.waiting_about.state,
    ):
        await message.answer("Сначала закончи заполнение профиля.")
        return
    current_state = await state.get_state()
    if current_state == RewriteStates.waiting_new_video.state:
        await db.set_user_video(user_id, message.video_note.file_id)
        await state.clear()
        await message.answer(
            "Готово, твой кружок обновлен. Теперь другие пользователи видят твой кружок.\n"
            "Нажми «Искать» или кнопку «Смотреть».\n"
            "Так же ты можешь повысить просмотры своей анкеты, подробнее по кнопке: Реферальная система.",
            reply_markup=kb_ready(user_id),
        )
        return

    if await db.user_has_video(user_id):
        await message.answer(
            "У тебя уже установлен кружок. Если хочешь установить новый — используй кнопку «Мой кружок».",
            reply_markup=main_kb(),
        )
        return

    await db.set_user_video(user_id, message.video_note.file_id)
    await message.answer(
        "Готово, твой кружок установлен. Теперь другие пользователи видят твой кружок.\n"
        "Нажми «Искать» или кнопку «Смотреть».\n"
        "Так же ты можешь повысить просмотры своей анкеты, подробнее по кнопке: Реферальная система.",
        reply_markup=kb_ready(user_id),
    )


async def send_next_video(bot: Bot, chat_id: int, viewer_user_id: int, db: DB) -> None:
    # All prerequisites come from one Supabase query instead of four.
    state = await db.get_search_state(viewer_user_id)
    if state["banned"]:
        return
    if state["active_chat_user_id"] is not None:
        await bot.send_message(
            chat_id,
            "Сейчас ты находишься в чате. Заверши его кнопкой «🚫 Завершить чат», чтобы снова искать кружки.",
        )
        return
    if not state["profile_complete"]:
        await bot.send_message(chat_id, "Сначала заполни профиль через /start.")
        return
    if not state["has_video"]:
        await bot.send_message(
            chat_id,
            "Чтобы смотреть чужие кружки, сначала отправь свой кружок.",
        )
        return

    # File IDs are bot-specific; if DB has stale IDs (e.g. token changed),
    # Telegram returns "wrong file identifier". In that case we drop the record
    # and try another one.
    for _ in range(10):
        video = await db.pick_next_video(viewer_user_id)
        if not video:
            await bot.send_message(
                chat_id,
                "Пока нет новых кружков. Попробуй позже.",
                reply_markup=kb_watch(),
            )
            return

        await db.mark_viewed(viewer_user_id, video.id)
        try:
            video_message = await bot.send_video_note(
                chat_id,
                video.file_id,
            )
            profile = {
                "age": video.age,
                "gender": video.gender,
                "looking_for": video.looking_for,
                "about": video.about,
                "profile_complete": True,
            }
            await bot.send_message(
                chat_id,
                format_profile_card(profile),
                reply_markup=kb_video(video.id, video.owner_user_id, viewer_user_id, video_message_id=video_message.message_id),
            )
            return
        except TelegramBadRequest as e:
            msg = str(e)
            if "wrong file identifier" in msg or "wrong file identifier/HTTP URL specified" in msg:
                await db.delete_video_by_id(video.id)
                continue
            raise


@router.callback_query(F.data == "watch")
async def cb_watch(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    await cb.answer()
    await send_next_video(bot, cb.message.chat.id, cb.from_user.id, db)


@router.callback_query(F.data.startswith("referral:"))
async def cb_referral(cb: CallbackQuery, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    await cb.answer()
    user_id = cb.from_user.id
    await cb.message.answer(
        "Делись этой ссылкой и получи +20 показов твоего кружка за каждый кружок твоих друзей.\n"
        f"Твоя ссылка: https://t.me/Anonymcircle_bot?start=ref_{user_id}"
    )


@router.callback_query(F.data == "next")
async def cb_next(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    await cb.answer()
    await send_next_video(bot, cb.message.chat.id, cb.from_user.id, db)


@router.callback_query(F.data.startswith("complaint:"))
async def cb_complaint(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return

    try:
        parts = cb.data.split(":")
        video_id = int(parts[1])
        video_message_id = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        await cb.answer("Не удалось обработать жалобу.", show_alert=True)
        return

    await db.add_complaint(cb.from_user.id, video_id)

    # Убираем пожаловавшийся кружок и карточку из чата пользователя.
    # Следующая анкета специально НЕ отправляется.
    if video_message_id:
        try:
            await bot.delete_message(cb.message.chat.id, video_message_id)
        except TelegramBadRequest:
            pass

    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass

    await cb.answer()
    await bot.send_message(cb.message.chat.id, "Спасибо за жалобу!")

    # Уведомляем администратора с кружком, на который пожаловались.
    try:
        video_info = await db.get_video_info(video_id)
        if video_info:
            owner_user_id = video_info["owner_user_id"]
            file_id = video_info["file_id"]
            owner_username = await db.get_username(owner_user_id)
            reporter_username = await db.get_username(cb.from_user.id)
            try:
                await bot.send_video_note(ADMIN_CHAT_ID, file_id, reply_markup=kb_admin_ban(owner_user_id))
            except TelegramBadRequest:
                await bot.send_message(ADMIN_CHAT_ID, "(не удалось отправить кружок: неверный file_id)")
            await bot.send_message(
                ADMIN_CHAT_ID,
                "Жалоба.\n"
                f"Кружок ID: {video_id}\n"
                f"Автор: {format_user_ref(owner_user_id, owner_username)}\n"
                f"Жалоба от: {format_user_ref(cb.from_user.id, reporter_username)}",
            )
    except Exception:
        log.exception("Failed to notify admin about complaint")

@router.callback_query(F.data.startswith("block:"))
async def cb_block(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    if await guard_banned_callback(cb, db):
        return
    try:
        _, raw_owner_id = cb.data.split(":", 1)
        owner_user_id = int(raw_owner_id)
    except Exception:
        await cb.answer("Не удалось заблокировать пользователя", show_alert=True)
        return

    await db.hide_user_videos(owner_user_id)
    await cb.answer("Пользователь заблокирован", show_alert=False)
    await cb.message.answer(
        "Кружок заблокирован и больше не будет показываться другим пользователям."
    )
    await send_next_video(bot, cb.message.chat.id, cb.from_user.id, db)


@router.callback_query(F.data.startswith("chat_start:"))
async def cb_chat_start(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return

    try:
        _, raw_owner_id = cb.data.split(":", 1)
        owner_user_id = int(raw_owner_id)
    except Exception:
        await cb.answer("Не удалось начать чат", show_alert=True)
        return

    current_partner = await db.get_active_chat_user(cb.from_user.id)
    if current_partner is not None and current_partner != owner_user_id:
        await cb.answer()
        await cb.message.answer(
            "Сейчас ты уже находишься в чате. Сначала заверши его кнопкой «🚫 Завершить чат», чтобы начать новый."
        )
        return

    if current_partner == owner_user_id:
        await cb.answer()
        await cb.message.answer("Ты уже в чате с этим пользователем. Для завершения используй кнопку «🚫 Завершить чат».")
        return

    owner_partner = await db.get_active_chat_user(owner_user_id)
    if owner_partner is not None and owner_partner != cb.from_user.id:
        await cb.answer()
        await cb.message.answer("Этот пользователь сейчас уже находится в другом чате.")
        return

    # Пытаемся создать отдельную тему в админской форум-группе.
    # Если форум не настроен или Telegram не позволяет создать тему,
    # это не должно мешать самому чату между пользователями.
    forum_chat_id = await db.get_forum_chat_id()
    topic_id = None

    if forum_chat_id is not None:
        owner_username = await db.get_username(owner_user_id)
        viewer_name = cb.from_user.username or str(cb.from_user.id)
        owner_name = owner_username or str(owner_user_id)

        try:
            topic = await bot.create_forum_topic(
                chat_id=forum_chat_id,
                name=f"💬 {viewer_name} ↔ {owner_name}",
            )
            topic_id = topic.message_thread_id

            try:
                await bot.send_message(
                    chat_id=forum_chat_id,
                    message_thread_id=topic_id,
                    text=(
                        "🔵 Новый чат\n\n"
                        f"Пользователь 1: {cb.from_user.id}"
                        f"{f' (@{cb.from_user.username})' if cb.from_user.username else ''}\n"
                        f"Пользователь 2: {owner_user_id}"
                        f"{f' (@{owner_username})' if owner_username else ''}"
                    ),
                )
            except (TelegramBadRequest, TelegramNetworkError, asyncio.TimeoutError):
                log.exception(
                    "Тема создана, но не удалось отправить стартовое сообщение: topic_id=%s",
                    topic_id,
                )
        except (TelegramBadRequest, TelegramNetworkError, asyncio.TimeoutError):
            log.exception("Не удалось создать тему для чата. Чат всё равно будет запущен.")

    # Сам чат запускается независимо от результата создания форум-темы.
    await db.start_chat(cb.from_user.id, owner_user_id)

    # Сохраняем связь с темой только если тема действительно была создана.
    if forum_chat_id is not None and topic_id is not None:
        await db.create_chat_topic(
            cb.from_user.id,
            owner_user_id,
            forum_chat_id,
            topic_id,
        )
    await cb.answer()

    # После начала чата убираем кнопки с карточки пользователя,
    # чтобы нельзя было повторно нажимать «Начать чат».
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await cb.message.answer("Собеседник подключён.", reply_markup=main_kb())
    try:
        await bot.send_message(
            owner_user_id,
            "С тобой начали чат. Теперь вы в чате. Чтобы закончить чат, используй /stopchat.",
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "rewrite")
async def cb_rewrite(cb: CallbackQuery, state: FSMContext, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    await cb.answer()
    await state.set_state(RewriteStates.waiting_new_video)
    await cb.message.answer("Отправь новый кружок (video note), чтобы заменить текущий.")


@router.callback_query(F.data == "delete_video")
async def cb_delete_video(cb: CallbackQuery, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    user_id = cb.from_user.id
    if not await db.get_user_video(user_id):
        await cb.answer("У тебя нет кружка.", show_alert=True)
        return

    await db.clear_user_video(user_id)
    await cb.answer("Кружок удалён", show_alert=False)
    await cb.message.answer(
        "Твой кружок удалён и больше не будет показываться другим пользователям.",
        reply_markup=main_kb(),
    )


@router.callback_query(F.data.startswith("admin_ban:"))
async def cb_admin_ban(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await touch_user(db, cb.from_user)
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    try:
        _, raw_user_id = cb.data.split(":", 1)
        target_user_id = int(raw_user_id)
    except Exception:
        await cb.answer("Ошибка", show_alert=True)
        return

    await db.ban_user(target_user_id)
    await db.clear_user_video(target_user_id)
    await cb.answer("Пользователь забанен", show_alert=False)
    await bot.send_message(ADMIN_CHAT_ID, f"Забанен пользователь: {target_user_id}")


def build_dp(db: DB) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp["db"] = db
    return dp


async def set_commands(bot: Bot) -> None:
    # Telegram commands must be latin; descriptions can be Russian.
    commands = [
        BotCommand(command="search", description="Искать (смотреть кружки)"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="my_video", description="Мой кружок"),
        BotCommand(command="start", description="Старт"),
    ]

    # Network hiccups shouldn't prevent the bot from starting.
    log.info("Setting bot commands…")
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                bot.set_my_commands(
                    commands,
                    scope=BotCommandScopeDefault(),
                    request_timeout=10,
                ),
                timeout=12,
            )
            log.info("Bot commands set.")
            return
        except TelegramNetworkError as e:
            if attempt == 2:
                log.warning("Failed to set commands (network). Continuing. %s", e)
                return
            await asyncio.sleep(1.5 * (attempt + 1))
        except asyncio.TimeoutError:
            if attempt == 2:
                log.warning("Failed to set commands (timeout). Continuing.")
                return
            await asyncio.sleep(1.5 * (attempt + 1))


@router.message(F.text == "/search")
async def cmd_search(message: Message, bot: Bot, db: DB) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    await send_next_video(bot, message.chat.id, message.from_user.id, db)


@router.message(F.text == "/setforum")
async def cmd_setforum(message: Message, db: DB) -> None:
    await touch_user(db, message.from_user)
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    if message.chat.type != "supergroup" or not getattr(message.chat, "is_forum", False):
        await message.answer("Эту команду нужно отправить в супергруппе с включёнными темами.")
        return

    await db.set_forum_chat_id(message.chat.id)
    await message.answer(
        "✅ Группа для чатов настроена. Теперь каждый новый чат будет создаваться "
        "в отдельной теме, а после /stopchat тема будет закрываться."
    )


@router.message(F.text == "/stopchat")
async def cmd_stopchat(message: Message, bot: Bot, db: DB) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return

    partner_user_id = await db.end_chat(message.from_user.id)
    if partner_user_id is None:
        await message.answer("Сейчас у тебя нет активного чата.")
        return

    topic = await db.close_chat_topic(message.from_user.id)
    if topic:
        try:
            await bot.close_forum_topic(
                chat_id=topic["group_chat_id"],
                message_thread_id=topic["topic_id"],
            )
        except TelegramBadRequest:
            log.exception("Не удалось закрыть тему чата: topic_id=%s", topic["topic_id"])

    await message.answer("Чат завершён.", reply_markup=ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Искать")],
            [KeyboardButton(text="⭕️ Мой кружок")],
        ],
        resize_keyboard=True,
    ))
    try:
        await bot.send_message(partner_user_id, "Собеседник завершил чат.")
    except TelegramBadRequest:
        pass


@router.message(F.text == "/my_video")
async def cmd_my_video(message: Message, bot: Bot, db: DB) -> None:
    user_id = message.from_user.id
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    video = await db.get_user_video(user_id)
    if not video:
        await message.answer("Сначала отправь свой кружок (video note).", reply_markup=main_kb())
        return

    likes = await db.get_video_likes(video.id)
    dislikes = await db.get_video_dislikes(video.id)
    try:
        await bot.send_video_note(message.chat.id, video.file_id)
    except TelegramBadRequest as e:
        msg = str(e)
        if "wrong file identifier" in msg or "wrong file identifier/HTTP URL specified" in msg:
            await db.clear_user_video(user_id)
            await message.answer(
                "Твой старый кружок больше недоступен (скорее всего менялся токен бота). Отправь кружок заново.",
                reply_markup=main_kb(),
            )
            return
        raise
    await message.answer(
        f"Твой кружок.\nЛайки: {likes}\nДизлайки: {dislikes}",
        reply_markup=kb_my_video(user_id),
    )


@router.message(F.text == "/profile")
async def cmd_profile(message: Message, db: DB) -> None:
    user_id = message.from_user.id
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    profile = await db.get_profile(user_id)
    if not profile or not profile.get("profile_complete"):
        await message.answer("Профиль не заполнен. Напиши /start.", reply_markup=main_kb())
        return

    await message.answer(
        "Профиль\n"
        f"Возраст: {profile.get('age')}\n"
        f"Пол: {profile.get('gender')}\n"
        f"Ищу: {profile.get('looking_for')}\n"
        f"О себе: {profile.get('about')}",
        reply_markup=main_kb(),
    )
    await message.answer("Хочешь изменить профиль?", reply_markup=kb_profile_edit())


@router.callback_query(F.data == "edit_profile")
async def cb_edit_profile(cb: CallbackQuery, db: DB, state: FSMContext) -> None:
    await touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    await cb.answer()
    await state.set_state(ProfileStates.waiting_age)
    await cb.message.answer(
        "Ок, давай обновим профиль.\nСколько тебе лет? (числом) (18-100)",
        reply_markup=main_kb(),
    )


@router.message(F.text == "🚫 Завершить чат")
async def btn_stopchat(message: Message, bot: Bot, db: DB) -> None:
    await cmd_stopchat(message, bot, db)


@router.message(F.text == "🔍 Искать")
async def btn_search(message: Message, bot: Bot, db: DB) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    await send_next_video(bot, message.chat.id, message.from_user.id, db)


@router.message(F.text == "⭕️ Мой кружок")
async def btn_my_video(message: Message, bot: Bot, db: DB) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    await cmd_my_video(message, bot, db)


@router.message(F.text == "👤 Мой профиль")
async def btn_profile(message: Message, db: DB) -> None:
    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    await cmd_profile(message, db)


@router.message()
async def relay_chat_messages(message: Message, bot: Bot, db: DB) -> None:
    if not message.from_user:
        return

    await touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return

    if message.text and message.text.startswith("/"):
        return

    partner_user_id = await db.get_active_chat_user(message.from_user.id)
    if partner_user_id is None:
        return

    # Отправляем сообщение собеседнику и администратору независимо друг от друга.
    # Раньше оба действия были в одном try, поэтому ошибка при отправке одному
    # получателю могла помешать отправке второму.
    try:
        await bot.copy_message(
            chat_id=partner_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramBadRequest:
        await message.answer("Не удалось доставить сообщение собеседнику.")

    # Копируем каждое сообщение в отдельную тему админской форум-группы:
    # текст, фото, видео, кружки, документы, стикеры и другие поддерживаемые
    # Telegram типы сообщений.
    topic = await db.get_active_chat_topic(message.from_user.id)
    if topic:
        try:
            await bot.copy_message(
                chat_id=topic["group_chat_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=topic["topic_id"],
            )
        except TelegramBadRequest:
            log.exception(
                "Не удалось скопировать сообщение в тему: "
                "user_id=%s, partner_id=%s, message_id=%s, topic_id=%s",
                message.from_user.id,
                partner_user_id,
                message.message_id,
                topic["topic_id"],
            )

async def main() -> None:
    # Load .env if present (local dev convenience)
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set DATABASE_URL environment variable")
    db = await DB.create(database_url)
    
    try:
        log.info("Bot starting…")
        async with Bot(token=token) as bot:
            # If a webhook was previously set (e.g. hosting), long polling won't receive updates.
            try:
                await bot.delete_webhook(drop_pending_updates=False, request_timeout=10)
            except (TelegramNetworkError, asyncio.TimeoutError):
                log.warning("Failed to delete webhook (network/timeout). Continuing.")
            # Commands menu setup is optional and can hang on some servers due to network issues.
            # The bot uses reply/inline keyboards, so skipping setMyCommands is OK.
            dp = build_dp(db)
            log.info("Polling started. Press Ctrl+C to stop.")
            await dp.start_polling(bot, db=db)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

