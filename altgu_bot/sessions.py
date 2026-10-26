"""Хранение сессий и защита входа от перебора пароля (требование из ТЗ диплома)."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

Cookies = list[dict[str, Any]]
T = TypeVar("T")


class TTLStore(Generic[T]):
    """Значения по id пользователя в памяти, с ограниченным временем жизни."""

    def __init__(self, ttl: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._items: dict[int, tuple[float, T]] = {}

    def set(self, user_id: int, value: T) -> None:
        self._items[user_id] = (self._clock() + self._ttl, value)

    def get(self, user_id: int) -> T | None:
        item = self._items.get(user_id)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._items[user_id]
            return None
        return value

    def drop(self, user_id: int) -> None:
        self._items.pop(user_id, None)


class SessionStore(TTLStore[Cookies]):
    """Cookie пользователя. Пароли не хранятся нигде."""


class LoginThrottle:
    """Не даёт пробовать пароль бесконечно: не более `max_attempts` ошибок за `window` секунд."""

    def __init__(self, max_attempts: int, window: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_attempts
        self._window = window
        self._clock = clock
        self._failures: dict[int, list[float]] = {}

    def _recent(self, user_id: int) -> list[float]:
        now = self._clock()
        recent = [t for t in self._failures.get(user_id, []) if now - t < self._window]
        if recent:
            self._failures[user_id] = recent
        else:
            self._failures.pop(user_id, None)
        return recent

    def allowed(self, user_id: int) -> bool:
        return len(self._recent(user_id)) < self._max

    def register_failure(self, user_id: int) -> None:
        self._recent(user_id)
        self._failures.setdefault(user_id, []).append(self._clock())

    def reset(self, user_id: int) -> None:
        self._failures.pop(user_id, None)

    def retry_after(self, user_id: int) -> int:
        """Через сколько секунд можно пробовать снова (0 — уже можно)."""
        recent = self._recent(user_id)
        if len(recent) < self._max:
            return 0
        return max(0, int(recent[0] + self._window - self._clock()) + 1)
