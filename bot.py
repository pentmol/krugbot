import asyncio
import logging
import os
import re

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
PARTNER_GROUP_ID = -1003988999463
PARTNER_BOT_URL = "https://t.me/anonymchat_rubot"
PARTNER_USAGE_PATTERN = re.compile(r"^\s*(\d{5,20})\s+использует\s+бота\.?\s*$", re.IGNORECASE)


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
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="⭕️ Мой кружок")],
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


def kb_access_gate() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться", url=PARTNER_BOT_URL)],
            [InlineKeyboardButton(text="Проверить", callback_data="check_partner_access")],
        ]
    )


def kb_ready(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Смотреть", callback_data="watch")],
            [InlineKeyboardButton(text="Реферальная система", callback_data=f"referral:{user_id}")],
        ]
    )


def kb_video(video_id: int, owner_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Начать чат", callback_data=f"chat_start:{owner_user_id}"),
            ],
            [
                InlineKeyboardButton(text="👍", callback_data=f"rate:{video_id}:1"),
                InlineKeyboardButton(text="👎", callback_data=f"rate:{video_id}:-1"),
            ],
            [
                InlineKeyboardButton(text="Следующее", callback_data="next"),
                InlineKeyboardButton(text="Жалоба", callback_data=f"complaint:{video_id}"),
            ]
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


def kb_my_video() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перезаписать", callback_data="rewrite")],
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


def touch_user(db: DB, tg_user) -> None:
    db.ensure_user(tg_user.id, tg_user.username)


async def send_access_gate_message(message: Message) -> None:
    await message.answer(
        "Перед использованием бота подпишитесь на наших спонсоров.",
        reply_markup=kb_access_gate(),
    )


async def guard_partner_access_message(message: Message, db: DB) -> bool:
    if message.chat.type != "private":
        return False
    if db.is_partner_verified(message.from_user.id):
        return False
    await send_access_gate_message(message)
    return True


async def guard_partner_access_callback(cb: CallbackQuery, db: DB) -> bool:
    if cb.message.chat.type != "private":
        return False
    if db.is_partner_verified(cb.from_user.id):
        return False
    await cb.answer("Сначала пройди проверку", show_alert=False)
    await cb.message.answer(
        "Перед использованием бота подпишитесь на наших спонсоров.",
        reply_markup=kb_access_gate(),
    )
    return True


async def guard_banned_message(message: Message, db: DB) -> bool:
    user_id = message.from_user.id
    if not db.is_banned(user_id):
        return False
    if not db.banned_notified(user_id):
        await message.answer("Ты забанен(а) и не можешь пользоваться ботом.")
        db.set_banned_notified(user_id)
    return True


async def guard_banned_callback(cb: CallbackQuery, db: DB) -> bool:
    if not db.is_banned(cb.from_user.id):
        return False
    # Must answer callback to stop the loading spinner, but don't spam messages.
    await cb.answer()
    return True


@router.message(CommandStart())
async def start(message: Message, db: DB, state: FSMContext) -> None:
    user_id = message.from_user.id
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return

    if not db.profile_complete(user_id):
        await state.set_state(ProfileStates.waiting_age)
        await message.answer(
            "Привет! Давай заполним профиль.\nСколько тебе лет? (числом) (18-100)",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start")]],
                resize_keyboard=True,
            ),
        )
        return

    if db.user_has_video(user_id):
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


@router.message(ProfileStates.waiting_age)
async def prof_age(message: Message, db: DB, state: FSMContext) -> None:
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
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
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
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
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
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
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    about = (message.text or "").strip()
    if not about:
        await message.answer("Напиши пару слов о себе текстом.")
        return
    if len(about) > 800:
        await message.answer("Слишком длинно. Напиши покороче (до 800 символов).")
        return

    data = await state.get_data()
    db.set_profile(
        user_id,
        age=int(data["age"]),
        gender=str(data["gender"]),
        looking_for=str(data["looking_for"]),
        about=about,
    )
    await state.clear()
    if db.user_has_video(user_id):
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
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    active_chat_user_id = db.get_active_chat_user(user_id)
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
    if not db.profile_complete(user_id):
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
        db.set_user_video(user_id, message.video_note.file_id)
        await state.clear()
        await message.answer(
            "Готово, твой кружок обновлен. Теперь другие пользователи видят твой кружок.\n"
            "Нажми «Искать» или кнопку «Смотреть».\n"
            "Так же ты можешь повысить просмотры своей анкеты, подробнее по кнопке: Реферальная система.",
            reply_markup=kb_ready(user_id),
        )
        return

    if db.user_has_video(user_id):
        await message.answer(
            "У тебя уже установлен кружок. Если хочешь установить новый — используй кнопку «Мой кружок».",
            reply_markup=main_kb(),
        )
        return

    db.set_user_video(user_id, message.video_note.file_id)
    await message.answer(
        "Готово, твой кружок установлен. Теперь другие пользователи видят твой кружок.\n"
        "Нажми «Искать» или кнопку «Смотреть».\n"
        "Так же ты можешь повысить просмотры своей анкеты, подробнее по кнопке: Реферальная система.",
        reply_markup=kb_ready(user_id),
    )


async def send_next_video(bot: Bot, chat_id: int, viewer_user_id: int, db: DB) -> None:
    if db.is_banned(viewer_user_id):
        # Don't spam banned users; they got a one-time notice on message handlers.
        return
    if db.get_active_chat_user(viewer_user_id) is not None:
        await bot.send_message(
            chat_id,
            "Сейчас ты находишься в чате. Заверши его командой /stopchat, чтобы снова искать кружки.",
        )
        return
    if not db.profile_complete(viewer_user_id):
        await bot.send_message(chat_id, "Сначала заполни профиль через /start.")
        return
    if not db.user_has_video(viewer_user_id):
        await bot.send_message(
            chat_id,
            "Чтобы смотреть чужие кружки, сначала отправь свой кружок.",
        )
        return

    # File IDs are bot-specific; if DB has stale IDs (e.g. token changed),
    # Telegram returns "wrong file identifier". In that case we drop the record
    # and try another one.
    for _ in range(10):
        video = db.pick_next_video(viewer_user_id)
        if not video:
            await bot.send_message(
                chat_id,
                "Пока нет новых кружков. Попробуй позже.",
                reply_markup=kb_watch(),
            )
            return

        db.mark_viewed(viewer_user_id, video.id)
        try:
            await bot.send_video_note(
                chat_id,
                video.file_id,
            )
            profile = db.get_profile(video.owner_user_id)
            await bot.send_message(
                chat_id,
                format_profile_card(profile),
                reply_markup=kb_video(video.id, video.owner_user_id),
            )
            return
        except TelegramBadRequest as e:
            msg = str(e)
            if "wrong file identifier" in msg or "wrong file identifier/HTTP URL specified" in msg:
                db.delete_video_by_id(video.id)
                continue
            raise


@router.callback_query(F.data == "watch")
async def cb_watch(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer()
    await send_next_video(bot, cb.message.chat.id, cb.from_user.id, db)


@router.callback_query(F.data == "check_partner_access")
async def cb_check_partner_access(cb: CallbackQuery, db: DB, state: FSMContext) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if db.is_partner_verified(cb.from_user.id):
        await cb.answer("Проверка пройдена")
        if not db.profile_complete(cb.from_user.id):
            await state.set_state(ProfileStates.waiting_age)
            await cb.message.answer(
                "Проверка пройдена. Давай заполним профиль.\nСколько тебе лет? (числом) (18-100)",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="/start")]],
                    resize_keyboard=True,
                ),
            )
            return
        await cb.message.answer("Проверка пройдена, доступ открыт.", reply_markup=main_kb())
        return
    await cb.answer("ID пока не найден. Сначала используй @anonymchat_rubot", show_alert=True)


@router.callback_query(F.data.startswith("referral:"))
async def cb_referral(cb: CallbackQuery, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer()
    user_id = cb.from_user.id
    await cb.message.answer(
        "Делись этой ссылкой и получи +20 показов твоего кружка за каждый кружок твоих друзей.\n"
        f"Твоя ссылка: https://t.me/Anonymcircle_bot?start=ref_{user_id}"
    )


@router.callback_query(F.data == "next")
async def cb_next(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer()
    await send_next_video(bot, cb.message.chat.id, cb.from_user.id, db)


@router.callback_query(F.data.startswith("complaint:"))
async def cb_complaint(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer("Спасибо за жалобу!", show_alert=False)
    try:
        _, raw_id = cb.data.split(":", 1)
        video_id = int(raw_id)
    except Exception:
        await bot.send_message(cb.message.chat.id, "Не удалось обработать жалобу.")
        return

    db.add_complaint(cb.from_user.id, video_id)
    # Notify admin with the complained circle and a ban button.
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT owner_user_id, file_id FROM videos WHERE id=?;", (video_id,))
        row = cur.fetchone()
        if row:
            owner_user_id = int(row["owner_user_id"])
            file_id = str(row["file_id"])
            owner_username = db.get_username(owner_user_id)
            reporter_username = db.get_username(cb.from_user.id)
            try:
                await bot.send_video_note(
                    ADMIN_CHAT_ID,
                    file_id,
                    reply_markup=kb_admin_ban(owner_user_id),
                )
            except TelegramBadRequest:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    "(не удалось отправить кружок: неверный file_id)",
                )
            await bot.send_message(
                ADMIN_CHAT_ID,
                "Жалоба.\n"
                f"Кружок ID: {video_id}\n"
                f"Автор: {format_user_ref(owner_user_id, owner_username)}\n"
                f"Жалоба от: {format_user_ref(cb.from_user.id, reporter_username)}",
            )
    except Exception:
        log.exception("Failed to notify admin about complaint")
    # Try to show next circle; if none, do not spam the user with "no new circles"
    # right after a complaint.
    video = db.pick_next_video(cb.from_user.id)
    if not video:
        return
    db.mark_viewed(cb.from_user.id, video.id)
    try:
        await bot.send_video_note(
            cb.message.chat.id,
            video.file_id,
        )
        profile = db.get_profile(video.owner_user_id)
        await bot.send_message(
            cb.message.chat.id,
            format_profile_card(profile),
            reply_markup=kb_video(video.id, video.owner_user_id),
        )
    except TelegramBadRequest as e:
        msg = str(e)
        if "wrong file identifier" in msg or "wrong file identifier/HTTP URL specified" in msg:
            db.delete_video_by_id(video.id)
            return
        raise


@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(cb: CallbackQuery, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    # rating does not auto-advance; user can press "Следующее" manually
    try:
        _, raw_video_id, raw_value = cb.data.split(":", 2)
        video_id = int(raw_video_id)
        value = int(raw_value)
    except Exception:
        await cb.answer("Не удалось поставить оценку", show_alert=False)
        return

    try:
        db.rate(cb.from_user.id, video_id, value)
    except Exception:
        await cb.answer("Не удалось поставить оценку", show_alert=False)
        return

    await cb.answer("Оценка сохранена", show_alert=False)


@router.callback_query(F.data.startswith("chat_start:"))
async def cb_chat_start(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return

    try:
        _, raw_owner_id = cb.data.split(":", 1)
        owner_user_id = int(raw_owner_id)
    except Exception:
        await cb.answer("Не удалось начать чат", show_alert=True)
        return

    current_partner = db.get_active_chat_user(cb.from_user.id)
    if current_partner is not None and current_partner != owner_user_id:
        await cb.answer()
        await cb.message.answer(
            "Сейчас ты уже находишься в чате. Сначала заверши его командой /stopchat, чтобы начать новый."
        )
        return

    if current_partner == owner_user_id:
        await cb.answer()
        await cb.message.answer("Ты уже в чате с этим пользователем. Для завершения используй /stopchat.")
        return

    owner_partner = db.get_active_chat_user(owner_user_id)
    if owner_partner is not None and owner_partner != cb.from_user.id:
        await cb.answer()
        await cb.message.answer("Этот пользователь сейчас уже находится в другом чате.")
        return

    db.start_chat(cb.from_user.id, owner_user_id)
    await cb.answer()
    await cb.message.answer("Теперь вы в чате с этим пользователем. Чтобы закончить чат, используй /stopchat.")
    try:
        await bot.send_message(
            owner_user_id,
            "С тобой начали чат. Теперь вы в чате. Чтобы закончить чат, используй /stopchat.",
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "rewrite")
async def cb_rewrite(cb: CallbackQuery, state: FSMContext, db: DB) -> None:
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer()
    await state.set_state(RewriteStates.waiting_new_video)
    await cb.message.answer("Отправь новый кружок (video note), чтобы заменить текущий.")


@router.callback_query(F.data.startswith("admin_ban:"))
async def cb_admin_ban(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    touch_user(db, cb.from_user)
    if cb.from_user.id != ADMIN_CHAT_ID:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    try:
        _, raw_user_id = cb.data.split(":", 1)
        target_user_id = int(raw_user_id)
    except Exception:
        await cb.answer("Ошибка", show_alert=True)
        return

    db.ban_user(target_user_id)
    db.clear_user_video(target_user_id)
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
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    await send_next_video(bot, message.chat.id, message.from_user.id, db)


@router.message(F.text == "/stopchat")
async def cmd_stopchat(message: Message, bot: Bot, db: DB) -> None:
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return

    partner_user_id = db.end_chat(message.from_user.id)
    if partner_user_id is None:
        await message.answer("Сейчас у тебя нет активного чата.")
        return

    await message.answer("Чат завершён.")
    try:
        await bot.send_message(partner_user_id, "Собеседник завершил чат.")
    except TelegramBadRequest:
        pass


@router.message(F.text == "/my_video")
async def cmd_my_video(message: Message, bot: Bot, db: DB) -> None:
    user_id = message.from_user.id
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    video = db.get_user_video(user_id)
    if not video:
        await message.answer("Сначала отправь свой кружок (video note).", reply_markup=main_kb())
        return

    likes = db.get_video_likes(video.id)
    dislikes = db.get_video_dislikes(video.id)
    try:
        await bot.send_video_note(message.chat.id, video.file_id)
    except TelegramBadRequest as e:
        msg = str(e)
        if "wrong file identifier" in msg or "wrong file identifier/HTTP URL specified" in msg:
            db.clear_user_video(user_id)
            await message.answer(
                "Твой старый кружок больше недоступен (скорее всего менялся токен бота). Отправь кружок заново.",
                reply_markup=main_kb(),
            )
            return
        raise
    await message.answer(
        f"Твой кружок.\nЛайки: {likes}\nДизлайки: {dislikes}",
        reply_markup=kb_my_video(),
    )


@router.message(F.text == "/profile")
async def cmd_profile(message: Message, db: DB) -> None:
    user_id = message.from_user.id
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    profile = db.get_profile(user_id)
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
    touch_user(db, cb.from_user)
    if await guard_banned_callback(cb, db):
        return
    if await guard_partner_access_callback(cb, db):
        return
    await cb.answer()
    await state.set_state(ProfileStates.waiting_age)
    await cb.message.answer(
        "Ок, давай обновим профиль.\nСколько тебе лет? (числом) (18-100)",
        reply_markup=main_kb(),
    )


@router.message(F.text == "🔍 Искать")
async def btn_search(message: Message, bot: Bot, db: DB) -> None:
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    await send_next_video(bot, message.chat.id, message.from_user.id, db)


@router.message(F.text == "⭕️ Мой кружок")
async def btn_my_video(message: Message, bot: Bot, db: DB) -> None:
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    await cmd_my_video(message, bot, db)


@router.message(F.text == "👤 Мой профиль")
async def btn_profile(message: Message, db: DB) -> None:
    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return
    await cmd_profile(message, db)


@router.message(F.chat.id == PARTNER_GROUP_ID, F.text)
async def track_partner_group_confirmation(message: Message, db: DB) -> None:
    match = PARTNER_USAGE_PATTERN.match(message.text or "")
    if not match:
        return
    try:
        verified_user_id = int(match.group(1))
    except (TypeError, ValueError):
        return
    db.mark_partner_verified(verified_user_id)
    log.info("Partner verification captured for user_id=%s", verified_user_id)


@router.message()
async def relay_chat_messages(message: Message, bot: Bot, db: DB) -> None:
    if not message.from_user:
        return

    if message.chat.id == PARTNER_GROUP_ID:
        return

    touch_user(db, message.from_user)
    if await guard_banned_message(message, db):
        return
    if await guard_partner_access_message(message, db):
        return

    if message.text and message.text.startswith("/"):
        return

    partner_user_id = db.get_active_chat_user(message.from_user.id)
    if partner_user_id is None:
        return

    try:
        await bot.copy_message(
            partner_user_id,
            message.chat.id,
            message.message_id,
        )
    except TelegramBadRequest:
        await message.answer("Не удалось доставить сообщение собеседнику.")

async def main() -> None:
    # Load .env if present (local dev convenience)
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env var")

    # Используем /data на Railway для постоянного хранения
    if os.path.exists('/data'):
        db_path = '/data/krugbot.sqlite3'
        os.makedirs('/data', exist_ok=True)
    else:
        db_path = os.getenv("DB_PATH", "krugbot.sqlite3")
    db = DB(db_path)
    
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
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
