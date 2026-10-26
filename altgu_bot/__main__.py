"""Запуск: python -m altgu_bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .ai import Assistant
from .config import ConfigError, Settings
from .handlers import App, create_router
from .portal import PortalClient
from .sessions import LoginThrottle, SessionStore, TTLStore


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    app = App(
        settings=settings,
        portal=PortalClient(settings.portal_url, settings.fetch_mode, settings.max_browsers),
        sessions=SessionStore(settings.session_ttl),
        throttle=LoginThrottle(settings.max_login_attempts, settings.login_window),
        plans=TTLStore(600),
        assistant=Assistant(settings.ai_api_key, settings.ai_model, settings.timezone, settings.ai_base_url),
    )

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_router())
    dp["app"] = app

    if app.assistant.enabled:
        logging.info("ИИ-помощник включён (модель: %s)", settings.ai_model)
    else:
        logging.info("ИИ-помощник выключен (нет GEMINI_API_KEY / OPENAI_API_KEY)")
    logging.info("Бот запускается (режим загрузки страниц: %s)", settings.fetch_mode)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        raise SystemExit(f"Ошибка настройки: {exc}")
    except KeyboardInterrupt:
        pass
