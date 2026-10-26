from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv

FETCH_MODES = {"auto", "http", "selenium"}
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"


class ConfigError(RuntimeError):
    """Неверная или неполная конфигурация."""


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} должно быть больше нуля")
    return value


def _ai_settings(env: Mapping[str, str]) -> dict[str, str | None]:
    """Выбирает, чем отвечать на вопросы: OpenAI, Gemini или ничем.

    OPENAI_API_KEY имеет приоритет; если его нет, но есть GEMINI_API_KEY — используется Gemini
    (адрес и модель подставляются сами). Нет ни того ни другого — ИИ-помощник выключен.
    """
    openai_key = env.get("OPENAI_API_KEY", "").strip()
    gemini_key = env.get("GEMINI_API_KEY", "").strip()
    custom_url = env.get("OPENAI_BASE_URL", "").strip() or None
    if openai_key:
        return {
            "ai_api_key": openai_key,
            "ai_base_url": custom_url,
            "ai_model": env.get("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL,
        }
    if gemini_key:
        return {
            "ai_api_key": gemini_key,
            "ai_base_url": custom_url or GEMINI_BASE_URL,
            "ai_model": env.get("GEMINI_MODEL", "").strip() or GEMINI_DEFAULT_MODEL,
        }
    return {"ai_api_key": None, "ai_base_url": None, "ai_model": DEFAULT_OPENAI_MODEL}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    portal_url: str = "https://lk.asu.ru"
    fetch_mode: str = "auto"  # auto | http | selenium
    ai_api_key: str | None = None
    ai_model: str = DEFAULT_OPENAI_MODEL
    ai_base_url: str | None = None  # None — официальный сервер OpenAI
    timezone: str = "Asia/Barnaul"
    session_ttl: int = 3600  # сколько секунд хранить сессию пользователя
    max_login_attempts: int = 3
    login_window: int = 600  # окно подсчёта неудачных попыток, секунд
    max_browsers: int = 2  # одновременно запущенных Chrome

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Читает настройки из переменных окружения (и из файла .env)."""
        if env is None:
            load_dotenv()
            env = os.environ

        token = env.get("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните его.")

        mode = env.get("FETCH_MODE", "auto").strip().lower() or "auto"
        if mode not in FETCH_MODES:
            raise ConfigError(f"FETCH_MODE должен быть одним из {sorted(FETCH_MODES)}, получено: {mode!r}")

        return cls(
            bot_token=token,
            portal_url=(env.get("PORTAL_URL", "").strip() or cls.portal_url).rstrip("/"),
            fetch_mode=mode,
            **_ai_settings(env),
            timezone=env.get("TIMEZONE", "").strip() or cls.timezone,
            session_ttl=_int(env, "SESSION_TTL", cls.session_ttl),
            max_login_attempts=_int(env, "MAX_LOGIN_ATTEMPTS", cls.max_login_attempts),
            login_window=_int(env, "LOGIN_WINDOW", cls.login_window),
            max_browsers=_int(env, "MAX_BROWSERS", cls.max_browsers),
        )
