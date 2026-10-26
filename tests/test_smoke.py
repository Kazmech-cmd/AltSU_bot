"""Проверяем, что приложение собирается: импорты, роутер, зависимости."""
from altgu_bot.ai import Assistant
from altgu_bot.handlers import create_router


def test_router_has_handlers():
    assert len(create_router().message.handlers) >= 10


def test_assistant_disabled_without_key():
    assistant = Assistant(api_key=None, model="m", timezone="Asia/Barnaul")
    assert assistant.enabled is False


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        message = type("M", (), {"content": "ответ"})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()


async def _ask(assistant):
    fake = _FakeCompletions()
    assistant._client = type("C", (), {"chat": type("Ch", (), {"completions": fake})()})()
    assert await assistant.answer("что завтра?", "Пн: математика") == "ответ"
    return fake.kwargs


async def test_openai_uses_max_completion_tokens():
    kwargs = await _ask(Assistant("key", "gpt", "Asia/Barnaul"))
    assert kwargs["max_completion_tokens"] == 1000 and "max_tokens" not in kwargs


async def test_custom_base_url_uses_max_tokens():
    kwargs = await _ask(Assistant("key", "m", "Asia/Barnaul", base_url="http://localhost:11434/v1"))
    assert kwargs["max_tokens"] == 1000 and "max_completion_tokens" not in kwargs


async def test_prompt_contains_date_and_timetable():
    kwargs = await _ask(Assistant("key", "m", "Asia/Barnaul"))
    user_text = kwargs["messages"][1]["content"]
    assert "Сегодня:" in user_text and "Пн: математика" in user_text and "что завтра?" in user_text
