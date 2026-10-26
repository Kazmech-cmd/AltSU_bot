from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from math import ceil
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from openai import APITimeoutError, InternalServerError, OpenAIError, RateLimitError

from .ai import Assistant
from .config import Settings
from .formatters import format_course, format_messages, format_timetable, pack, timetable_to_text
from .parsers import StudyPlan, parse_messages, parse_study_plan, parse_timetable
from .portal import (
    MESSAGES_PAGE, PLAN_PAGE, TIMETABLE_PAGE,
    LoginFailed, Page, PortalClient, PortalError, SessionExpired,
)
from .sessions import LoginThrottle, SessionStore, TTLStore

logger = logging.getLogger(__name__)

BTN_LOGIN = "Войти в аккаунт"
BTN_HELP = "Что ты умеешь"
BTN_PLAN = "Мой учебный план"
BTN_MESSAGES = "Сообщения"
BTN_TIMETABLE = "Моё расписание"
BTN_LOGOUT = "Выйти"

MAX_QUESTION_LEN = 500

HELP_TEXT = (
    "Я могу показать:\n"
    "📘 Учебный план — /plan (текущий курс), /plan 1, /plan 2, /plan 3\n"
    "📨 Сообщения — /messages\n"
    "📅 Расписание — /timetable\n"
    "🤖 Вопрос по расписанию — «Бот: что у меня завтра?» или /ask\n"
    "🚪 Выйти — /logout\n\n"
    "Пароль от кабинета я не храню и удаляю из чата сразу после ввода."
)


@dataclass
class App:
    """Зависимости бота; передаются в хендлеры по имени параметра `app`."""
    settings: Settings
    portal: PortalClient
    sessions: SessionStore
    throttle: LoginThrottle
    assistant: Assistant
    plans: TTLStore[StudyPlan]  # разобранный учебный план, чтобы кнопки курсов не грузили кабинет заново


class LoginStates(StatesGroup):
    login = State()
    password = State()


def _kb(*rows: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row.split("|")] for row in rows],
        resize_keyboard=True,
    )


def start_keyboard() -> ReplyKeyboardMarkup:
    return _kb(BTN_LOGIN, BTN_HELP)


def menu_keyboard() -> ReplyKeyboardMarkup:
    return _kb(f"{BTN_PLAN}|{BTN_MESSAGES}", f"{BTN_TIMETABLE}|{BTN_LOGOUT}")


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        logger.debug("Не удалось удалить сообщение", exc_info=True)


# ---------- старт и справка ----------

async def cmd_start(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    logged_in = message.from_user is not None and app.sessions.get(message.from_user.id) is not None
    await message.answer(
        "Привет! Я бот АлтГУ. Что будем делать?",
        reply_markup=menu_keyboard() if logged_in else start_keyboard(),
    )


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


# ---------- вход / выход ----------

async def ask_login(message: Message, state: FSMContext, app: App) -> None:
    if message.from_user is None:
        return
    uid = message.from_user.id
    if not app.throttle.allowed(uid):
        minutes = ceil(app.throttle.retry_after(uid) / 60)
        await message.answer(f"Слишком много неудачных попыток входа. Попробуйте через {minutes} мин.")
        return
    await state.set_state(LoginStates.login)
    await message.answer("Введите логин:")


async def got_login(message: Message, state: FSMContext) -> None:
    await state.update_data(login=(message.text or "").strip())
    await state.set_state(LoginStates.password)
    await message.answer("Введите пароль. Сообщение с паролем я сразу удалю из чата.")


async def got_password(message: Message, state: FSMContext, app: App) -> None:
    if message.from_user is None:
        return
    uid = message.from_user.id
    password = message.text or ""
    await _safe_delete(message)  # пароль не должен оставаться в истории чата

    login = (await state.get_data()).get("login")
    if not login:
        await state.clear()
        await message.answer("Начнём заново: нажмите «Войти в аккаунт».", reply_markup=start_keyboard())
        return

    status = await message.answer("⏳ Пытаюсь войти…")
    try:
        cookies = await app.portal.login(login, password)
    except LoginFailed:
        app.throttle.register_failure(uid)
        if app.throttle.allowed(uid):
            await state.set_state(LoginStates.login)
            await status.edit_text("❌ Вход не удался. Проверьте данные и введите логин ещё раз:")
        else:
            await state.clear()
            await status.edit_text("❌ Вход не удался. Лимит попыток исчерпан, попробуйте позже.")
        return
    except PortalError:
        await state.clear()
        await status.edit_text("⚠️ Личный кабинет сейчас недоступен. Попробуйте позже.")
        return

    app.throttle.reset(uid)
    app.sessions.set(uid, cookies)
    await state.clear()
    await status.edit_text("✅ Успешный вход!")
    await message.answer("Выберите раздел:", reply_markup=menu_keyboard())


async def cmd_logout(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    if message.from_user is not None:
        app.sessions.drop(message.from_user.id)
        app.plans.drop(message.from_user.id)
    await message.answer("Вы вышли из аккаунта.", reply_markup=start_keyboard())


# ---------- данные из кабинета ----------

async def _load(message: Message, app: App, page: Page, loading_text: str) -> str | None:
    """Скачивает страницу или объясняет пользователю, почему не вышло."""
    if message.from_user is None:
        return None
    uid = message.from_user.id
    cookies = app.sessions.get(uid)
    if cookies is None:
        await message.answer("Сначала войдите в аккаунт: /login", reply_markup=start_keyboard())
        return None

    status = await message.answer(loading_text)
    try:
        html = await app.portal.fetch(page, cookies)
    except SessionExpired:
        app.sessions.drop(uid)
        await status.edit_text("Сессия истекла. Войдите снова: /login")
        return None
    except PortalError:
        logger.exception("Не удалось загрузить %s", page.path)
        await status.edit_text("⚠️ Не удалось получить данные из личного кабинета. Попробуйте позже.")
        return None
    await _safe_delete(status)
    return html


def _today(app: App) -> date:
    return datetime.now(ZoneInfo(app.settings.timezone)).date()


def _course_from_args(args: str | None) -> int | None:
    match = re.search(r"\d+", args or "")
    return int(match.group()) if match else None


def _course_keyboard(plan: StudyPlan, shown: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"• {n} курс" if n == shown else f"{n} курс", callback_data=f"plan:{n}")
        for n in sorted(plan.courses)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def _send_course(send: Callable[..., Awaitable[object]], plan: StudyPlan, course: int) -> None:
    chunks = format_course(plan.courses[course])
    keyboard = _course_keyboard(plan, course) if len(plan.courses) > 1 else None
    for i, chunk in enumerate(chunks):
        await send(chunk, reply_markup=keyboard if i == len(chunks) - 1 else None)


async def show_plan(message: Message, app: App, command: CommandObject | None = None) -> None:
    html = await _load(message, app, PLAN_PAGE, "📘 Загружаю учебный план…")
    if html is None:
        return
    plan = parse_study_plan(html)
    if plan is None:
        await message.answer("❌ Учебный план не найден.")
        return
    if message.from_user is not None:
        app.plans.set(message.from_user.id, plan)

    requested = _course_from_args(command.args if command else None)
    if requested is not None and requested not in plan.courses:
        available = ", ".join(str(n) for n in sorted(plan.courses))
        await message.answer(f"Курса {requested} в плане нет. Доступны: {available}.")
        return
    course = requested if requested is not None else plan.current_course(_today(app))
    if course is None:
        await message.answer("❌ Учебный план не найден.")
        return
    await _send_course(message.answer, plan, course)


async def choose_course(callback: CallbackQuery, bot: Bot, app: App) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    plan = app.plans.get(user_id)
    number = _course_from_args(callback.data)
    if plan is None or number not in plan.courses:
        await bot.send_message(user_id, "Данные устарели. Нажмите «Мой учебный план» ещё раз.")
        return
    await _send_course(partial(bot.send_message, user_id), plan, number)


async def show_messages(message: Message, app: App) -> None:
    html = await _load(message, app, MESSAGES_PAGE, "📨 Загружаю сообщения…")
    if html is None:
        return
    chunks = format_messages(parse_messages(html))
    if not chunks:
        await message.answer("Сообщений нет.")
        return
    for chunk in chunks:
        await message.answer(chunk)


async def show_timetable(message: Message, app: App) -> None:
    html = await _load(message, app, TIMETABLE_PAGE, "📅 Загружаю расписание…")
    if html is None:
        return
    chunks = format_timetable(parse_timetable(html))
    if not chunks:
        await message.answer("Расписание не найдено.")
        return
    for chunk in chunks:
        await message.answer(chunk)


# ---------- ИИ-помощник ----------

async def _answer_question(message: Message, app: App, question: str) -> None:
    if not app.assistant.enabled:
        await message.answer("ИИ-помощник выключен: в настройках не задан OPENAI_API_KEY.")
        return
    if not question:
        await message.answer("Напишите вопрос после «Бот:», например: «Бот: что у меня завтра?»")
        return
    if len(question) > MAX_QUESTION_LEN:
        await message.answer(f"Вопрос слишком длинный (максимум {MAX_QUESTION_LEN} символов).")
        return

    html = await _load(message, app, TIMETABLE_PAGE, "📅 Загружаю расписание…")
    if html is None:
        return
    status = await message.answer("🤖 Думаю над ответом…")
    try:
        answer = await app.assistant.answer(question, timetable_to_text(parse_timetable(html)))
    except APITimeoutError:
        logger.warning("ИИ не ответил за отведённое время")
        await status.edit_text("⚠️ ИИ слишком долго не отвечает. Попробуйте ещё раз через минуту.")
        return
    except (InternalServerError, RateLimitError):
        logger.warning("ИИ перегружен или исчерпан лимит", exc_info=True)
        await status.edit_text("⚠️ ИИ сейчас перегружен или исчерпан лимит запросов. Попробуйте через минуту.")
        return
    except OpenAIError:
        logger.exception("Ошибка обращения к ИИ")
        await status.edit_text("⚠️ Не удалось получить ответ от ИИ. Попробуйте позже.")
        return
    await _safe_delete(status)
    for chunk in pack([answer], sep="\n"):
        await message.answer(chunk, parse_mode=None)  # ответ модели не парсим как HTML


async def cmd_ask(message: Message, app: App, command: CommandObject) -> None:
    await _answer_question(message, app, (command.args or "").strip())


async def prefixed_question(message: Message, app: App) -> None:
    await _answer_question(message, app, (message.text or "")[4:].strip())


# ---------- всё остальное ----------

async def fallback(message: Message) -> None:
    await message.answer("Не понял. Список команд: /help")


def create_router() -> Router:
    """Новый роутер на каждый вызов: так его можно подключать к разным диспетчерам (в т.ч. в тестах)."""
    router = Router(name="main")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, F.text == BTN_HELP)
    router.message.register(cmd_help, Command("help"))
    router.message.register(ask_login, F.text == BTN_LOGIN)
    router.message.register(ask_login, Command("login"))
    router.message.register(got_login, LoginStates.login, F.text)
    router.message.register(got_password, LoginStates.password, F.text)
    router.message.register(cmd_logout, F.text == BTN_LOGOUT)
    router.message.register(cmd_logout, Command("logout"))
    router.message.register(show_plan, F.text == BTN_PLAN)
    router.message.register(show_plan, Command("plan"))
    router.message.register(show_messages, F.text == BTN_MESSAGES)
    router.message.register(show_messages, Command("messages"))
    router.message.register(show_timetable, F.text == BTN_TIMETABLE)
    router.message.register(show_timetable, Command("timetable"))
    router.message.register(cmd_ask, Command("ask"))
    router.message.register(prefixed_question, F.text.lower().startswith("бот:"))
    router.message.register(fallback)
    router.callback_query.register(choose_course, F.data.startswith("plan:"))
    return router
