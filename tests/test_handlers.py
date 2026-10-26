"""Сквозные тесты диалога: настоящие хендлеры aiogram, но без Telegram и без портала."""
from __future__ import annotations

import datetime
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from altgu_bot.config import Settings
from altgu_bot.handlers import App, create_router
from altgu_bot.portal import LoginFailed, PortalUnavailable, SessionExpired
from altgu_bot.sessions import LoginThrottle, SessionStore, TTLStore

COOKIES = [{"name": "sid", "value": "abc", "domain": "lk.asu.ru", "path": "/"}]


class FakeSession(BaseSession):
    """Вместо сети запоминает, что бот пытался отправить."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            reply = Message(
                message_id=1000 + len(self.requests), date=datetime.datetime.now(),
                chat=Chat(id=1, type="private"), text=getattr(method, "text", ""),
            )
            # настоящая сессия привязывает ответ к боту сама; здесь делаем это вручную
            return Message.model_validate(reply.model_dump(exclude_none=True), context={"bot": bot})
        return True


class FakePortal:
    def __init__(self, html: dict[str, str], login_errors: list[Exception] | None = None) -> None:
        self.html = html
        self.login_errors = list(login_errors or [])
        self.logins: list[tuple[str, str]] = []
        self.fetch_error: Exception | None = None

    async def login(self, login: str, password: str):
        self.logins.append((login, password))
        if self.login_errors:
            raise self.login_errors.pop(0)
        return COOKIES

    async def fetch(self, page, cookies):
        if self.fetch_error:
            raise self.fetch_error
        return self.html[page.path]


class FakeAssistant:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str]] = []

    async def answer(self, question: str, timetable_text: str) -> str:
        self.calls.append((question, timetable_text))
        return "Завтра математика в 101."


class Harness:
    def __init__(self, portal: FakePortal, assistant: FakeAssistant, max_attempts: int = 3) -> None:
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(create_router())
        self.app = App(
            settings=Settings(bot_token="42:TEST"), portal=portal,
            sessions=SessionStore(ttl=3600), throttle=LoginThrottle(max_attempts, 600),
            assistant=assistant, plans=TTLStore(600),
        )
        self.dp["app"] = self.app
        self._ids = itertools.count(1)

    async def say(self, text: str, uid: int = 1) -> None:
        n = next(self._ids)
        update = Update(update_id=n, message=Message(
            message_id=n, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name="Тест"), text=text,
        ))
        await self.dp.feed_update(self.bot, update)

    async def press(self, data: str, uid: int = 1) -> None:
        """Нажатие на inline-кнопку."""
        n = next(self._ids)
        update = Update(update_id=n, callback_query=CallbackQuery(
            id=str(n), from_user=User(id=uid, is_bot=False, first_name="Тест"),
            chat_instance="test", data=data,
        ))
        await self.dp.feed_update(self.bot, update)

    def sent(self) -> list:
        return [m for m in self.session.requests if isinstance(m, SendMessage)]

    def texts(self) -> list[str]:
        return [m.text for m in self.session.requests if isinstance(m, (SendMessage, EditMessageText))]

    @property
    def last(self) -> str:
        return self.texts()[-1]

    def deleted(self) -> int:
        return sum(isinstance(m, DeleteMessage) for m in self.session.requests)


@pytest.fixture
def make(load):
    def _make(login_errors=None, assistant=None, max_attempts=3) -> Harness:
        portal = FakePortal(
            {"/student/plan/": load("plan.html"), "/messages/?limit=100": load("messages.html"),
             "/timetable/": load("timetable.html")},
            login_errors,
        )
        return Harness(portal, assistant or FakeAssistant(), max_attempts)
    return _make


async def login(h: Harness, password: str = "secret") -> None:
    await h.say("/login")
    await h.say("student")
    await h.say(password)


# ---------- вход ----------

async def test_successful_login(make):
    h = make()
    await login(h)
    assert h.app.portal.logins == [("student", "secret")]
    assert h.app.sessions.get(1) == COOKIES
    assert "Выберите раздел" in h.last


async def test_password_message_is_deleted_and_never_echoed(make):
    h = make()
    await login(h, password="sup3r-secret")  # secrets-ok: тестовое значение
    assert h.deleted() >= 1
    assert all("sup3r-secret" not in t for t in h.texts())


async def test_failed_login_asks_again(make):
    h = make(login_errors=[LoginFailed()])
    await login(h)
    assert h.app.sessions.get(1) is None
    assert "Вход не удался" in h.last
    await h.say("student")  # снова вводим логин -> просит пароль
    assert "пароль" in h.last.lower()


async def test_login_is_blocked_after_too_many_failures(make):
    h = make(login_errors=[LoginFailed(), LoginFailed()], max_attempts=2)
    await login(h)
    await h.say("student")
    await h.say("wrong")
    assert "Лимит попыток" in h.last
    await h.say("/login")
    assert "Слишком много" in h.last
    assert len(h.app.portal.logins) == 2  # третьей попытки к порталу не было


async def test_portal_down_during_login(make):
    h = make(login_errors=[PortalUnavailable()])
    await login(h)
    assert "недоступен" in h.last
    assert h.app.sessions.get(1) is None


# ---------- данные ----------

async def test_data_requires_login(make):
    h = make()
    await h.say("/plan")
    assert "Сначала войдите" in h.last


async def test_plan_shows_current_course_by_default(make):
    h = make()
    await login(h)
    await h.say("Мой учебный план")
    text = "\n".join(h.texts())
    assert "2 курс, 2026-2027" in text and "Семестр 3" in text and "Семестр 4" in text
    assert "Семестр 1" not in text and "Семестр 5" not in text  # чужие курсы не показываем


async def test_plan_command_with_course_number(make):
    h = make()
    await login(h)
    await h.say("/plan 1")
    text = "\n".join(h.texts())
    assert "1 курс, 2025-2026" in text and "Семестр 1" in text and "Переаттестованные дисциплины" in text
    assert "Семестр 3" not in text


async def test_plan_with_unknown_course_explains_what_is_available(make):
    h = make()
    await login(h)
    await h.say("/plan 7")
    assert "Курса 7 в плане нет" in h.last and "1, 2, 3" in h.last


async def test_plan_last_message_has_course_buttons_and_marks_current(make):
    h = make()
    await login(h)
    await h.say("/plan")
    keyboard = h.sent()[-1].reply_markup.inline_keyboard
    buttons = [b for row in keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["plan:1", "plan:2", "plan:3"]
    assert [b.text for b in buttons] == ["1 курс", "• 2 курс", "3 курс"]


async def test_only_last_plan_message_carries_buttons(make):
    h = make()
    await login(h)
    await h.say("/plan")
    plan_messages = [m for m in h.sent() if "Семестр" in (m.text or "")]
    assert len(plan_messages) >= 2
    assert all(m.reply_markup is None for m in plan_messages[:-1])
    assert plan_messages[-1].reply_markup is not None


async def test_course_button_switches_course_without_reloading_the_portal(make):
    h = make()
    await login(h)
    await h.say("/plan")
    h.app.portal.fetch_error = PortalUnavailable()  # если бот полезет в кабинет, ответ будет ошибкой
    await h.press("plan:3")
    text = h.texts()[-2] + h.last
    assert "3 курс, 2027-2028" in text and "Семестр 5" in text
    assert "Не удалось получить данные" not in "\n".join(h.texts())


async def test_course_button_marks_the_newly_selected_course(make):
    h = make()
    await login(h)
    await h.say("/plan")
    await h.press("plan:1")
    buttons = [b for row in h.sent()[-1].reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["• 1 курс", "2 курс", "3 курс"]


async def test_course_button_with_expired_data_asks_to_reload(make):
    h = make()
    await login(h)
    await h.press("plan:2")
    assert "Данные устарели" in h.last


async def test_course_button_with_unknown_course_asks_to_reload(make):
    h = make()
    await login(h)
    await h.say("/plan")
    await h.press("plan:9")
    assert "Данные устарели" in h.last


async def test_logout_forgets_the_cached_plan(make):
    h = make()
    await login(h)
    await h.say("/plan")
    await h.say("/logout")
    await h.press("plan:3")
    assert "Данные устарели" in h.last


async def test_course_buttons_work_per_user(make):
    h = make()
    await login(h)
    await h.say("/plan")
    await h.press("plan:3", uid=2)
    assert "Данные устарели" in h.last


async def test_missing_plan_page_is_reported(make):
    h = make()
    await login(h)
    h.app.portal.html["/student/plan/"] = "<html>пусто</html>"
    await h.say("/plan")
    assert "Учебный план не найден" in h.last


async def test_timetable_after_login(make):
    h = make()
    await login(h)
    await h.say("/timetable")
    assert "Понедельник" in h.last and "Математика" in h.last


async def test_messages_after_login(make):
    h = make()
    await login(h)
    await h.say("Сообщения")
    assert "Изменение в расписании" in h.last


async def test_expired_session_is_dropped(make):
    h = make()
    await login(h)
    h.app.portal.fetch_error = SessionExpired()
    await h.say("/plan")
    assert "Сессия истекла" in h.last
    assert h.app.sessions.get(1) is None


async def test_portal_error_on_fetch(make):
    h = make()
    await login(h)
    h.app.portal.fetch_error = PortalUnavailable()
    await h.say("/timetable")
    assert "Не удалось получить данные" in h.last
    assert h.app.sessions.get(1) == COOKIES  # сессия при этом остаётся


async def test_sessions_are_isolated_between_users(make):
    h = make()
    await login(h)
    await h.say("/plan", uid=2)
    assert "Сначала войдите" in h.last


async def test_logout(make):
    h = make()
    await login(h)
    await h.say("/logout")
    assert h.app.sessions.get(1) is None
    await h.say("/plan")
    assert "Сначала войдите" in h.last


# ---------- ИИ ----------

async def test_ai_question_with_prefix(make):
    assistant = FakeAssistant()
    h = make(assistant=assistant)
    await login(h)
    await h.say("Бот: что у меня завтра?")
    assert h.last == "Завтра математика в 101."
    question, timetable = assistant.calls[0]
    assert question == "что у меня завтра?"
    assert "Понедельник" in timetable and "<" not in timetable


async def test_ai_question_via_command(make):
    assistant = FakeAssistant()
    h = make(assistant=assistant)
    await login(h)
    await h.say("/ask когда физика?")
    assert assistant.calls[0][0] == "когда физика?"


async def test_ai_disabled_without_key(make):
    h = make(assistant=FakeAssistant(enabled=False))
    await login(h)
    await h.say("Бот: привет")
    assert "OPENAI_API_KEY" in h.last


async def test_ai_empty_and_too_long_question(make):
    assistant = FakeAssistant()
    h = make(assistant=assistant)
    await login(h)
    await h.say("Бот:")
    assert "Напишите вопрос" in h.last
    await h.say("Бот: " + "а" * 600)
    assert "слишком длинный" in h.last
    assert assistant.calls == []


async def test_ai_requires_login(make):
    h = make()
    await h.say("Бот: привет")
    assert "Сначала войдите" in h.last


# ---------- прочее ----------

async def test_unknown_text_gets_hint(make):
    h = make()
    await h.say("абракадабра")
    assert "/help" in h.last


async def test_help_mentions_commands(make):
    h = make()
    await h.say("/help")
    assert "/plan" in h.last and "/logout" in h.last
