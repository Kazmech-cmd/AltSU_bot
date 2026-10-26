#!/usr/bin/env python3
"""Проверка ИИ-помощника без запуска бота и без личного кабинета.

Запуск из корня проекта (окружение venv должно быть включено):

    python scripts/check_ai.py

Скрипт читает настройки из .env так же, как бот, и по шагам проверяет: ключ и связь, наличие модели,
простой запрос и запрос «как у бота». Сам ключ нигде не печатается.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openai  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from altgu_bot.ai import Assistant  # noqa: E402
from altgu_bot.config import ConfigError, Settings  # noqa: E402

TEST_TIMETABLE = "Понедельник:\n  1 (08:30–10:05): Математика · ауд. 101\nВторник:\n  занятий нет"


def hint(exc: Exception) -> str:
    """Подсказка по типу ошибки."""
    checks = [
        ("APITimeoutError", "сервер не ответил вовремя: перегрузка, медленная модель или проблемы со связью"),
        ("APIConnectionError", "не удалось соединиться: проверьте интернет и VPN (если он нужен для Google)"),
        ("NotFoundError", "модель с таким именем недоступна: возьмите имя из списка выше и впишите в GEMINI_MODEL"),
        ("AuthenticationError", "ключ не принят: проверьте GEMINI_API_KEY в .env (без пробелов и кавычек)"),
        ("PermissionDeniedError", "доступ запрещён: возможно, регион или права ключа"),
        ("RateLimitError", "превышен лимит запросов: подождите или попробуйте позже"),
        ("BadRequestError", "сервер не принял запрос: возможно, параметры не подходят этой модели"),
        ("InternalServerError", "ошибка на стороне сервиса: попробуйте позже"),
    ]
    for name, text in checks:
        cls = getattr(openai, name, None)
        if cls is not None and isinstance(exc, cls):
            return text
    return "неизвестная ошибка, пришлите вывод скрипта целиком"


async def timed(awaitable):
    start = time.perf_counter()
    try:
        return await awaitable, time.perf_counter() - start, None
    except Exception as exc:  # noqa: BLE001 — это диагностика, нужно поймать всё
        return None, time.perf_counter() - start, exc


def report_error(exc: Exception, took: float) -> None:
    print(f"   [ОШИБКА] через {took:.1f} с: {type(exc).__name__}")
    print(f"   Сообщение: {str(exc)[:500]}")
    print(f"   Что это значит: {hint(exc)}")


async def list_models(client: AsyncOpenAI) -> list[str]:
    return sorted([m.id async for m in client.models.list()])


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка ИИ-помощника")
    parser.add_argument("--timeout", type=float, default=60, help="сколько секунд ждать ответа (по умолчанию 60)")
    parser.add_argument("--model", help="проверить другую модель, не меняя .env (например: --model gemini-flash-lite-latest)")
    parser.add_argument("--all", action="store_true", help="показать все модели из списка, а не только flash/lite")
    args = parser.parse_args()

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Ошибка настроек: {exc}")
        return 1
    if not settings.ai_api_key:
        print("Ключ не найден: впишите GEMINI_API_KEY (или OPENAI_API_KEY) в .env")
        return 1

    model = args.model or settings.ai_model
    print(f"Сервер: {settings.ai_base_url or 'OpenAI (по умолчанию)'}")
    print(f"Модель: {model}" + ("  (из параметра --model)" if args.model else ""))
    print(f"Ключ:   задан ({len(settings.ai_api_key)} символов, сам ключ не показываем)")

    client = AsyncOpenAI(
        api_key=settings.ai_api_key, base_url=settings.ai_base_url, timeout=args.timeout, max_retries=0
    )
    failures = 0

    print("\n1. Список моделей (проверка ключа и связи)")
    ids, took, exc = await timed(list_models(client))
    if exc:
        report_error(exc, took)
        print("   Дальше проверяем всё равно.")
    else:
        names = {i.removeprefix("models/") for i in ids}
        print(f"   [OK] ответ за {took:.1f} с, моделей: {len(ids)}")
        if model in names:
            print(f"   [OK] модель {model} есть в списке")
        else:
            print(f"   [ВНИМАНИЕ] модели {model} нет в списке")
        shown = sorted(names) if args.all else sorted(n for n in names if "flash" in n or "lite" in n)
        print("   Подходящие для бота модели (быстрые): " + (", ".join(shown) if shown else "не нашлось, запустите с --all"))
        print("   Проверить любую из них: python scripts\\check_ai.py --model ИМЯ")

    print(f"\n2. Простой запрос к модели (ждём до {args.timeout:.0f} с)")
    token_param = "max_tokens" if settings.ai_base_url else "max_completion_tokens"
    response, took, exc = await timed(client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Ответь одним словом: привет"}],
        **{token_param: 300},
    ))
    if exc:
        failures += 1
        report_error(exc, took)
    else:
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", "?")
        print(f"   [OK] ответ за {took:.1f} с, причина остановки: {choice.finish_reason}, токенов: {tokens}")
        print(f"   Ответ модели: {(choice.message.content or '').strip()[:200]!r}")
        if not (choice.message.content or "").strip():
            print("   [ВНИМАНИЕ] ответ пустой: возможно, модель потратила лимит на «размышления»")

    print("\n3. Запрос «как у бота» (с расписанием и инструкцией)")
    assistant = Assistant(settings.ai_api_key, model, settings.timezone, settings.ai_base_url)
    answer, took, exc = await timed(assistant.answer("Что у меня завтра?", TEST_TIMETABLE))
    if exc:
        failures += 1
        report_error(exc, took)
    else:
        if answer.startswith("Не удалось сформулировать"):
            failures += 1
            print(f"   [ВНИМАНИЕ] за {took:.1f} с пришёл пустой ответ: модель не уложилась в лимит токенов")
        else:
            print(f"   [OK] ответ за {took:.1f} с")
            print(f"   Ответ: {answer[:300]!r}")

    print("\nИтог: " + ("всё работает." if not failures else f"проблемных шагов: {failures}. Пришлите этот вывод."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
