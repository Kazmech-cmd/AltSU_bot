"""Работа с личным кабинетом: вход и получение HTML-страниц.

Вход выполняется через Selenium (форма с JS). Дальше страницы по умолчанию берутся
обычным HTTP-запросом с cookie — это в разы быстрее запуска браузера на каждый запрос.
Если страница по HTTP пришла без нужной таблицы, клиент сам откатывается на браузер
(режим FETCH_MODE=auto).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .sessions import Cookies

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOGIN_FIELD = "auth_user_name"
PASSWORD_FIELD = "auth_user_pass"
LOGOUT_ICON = 'img-icon[data-action="logout"]'
ALLOWED_COOKIE_KEYS = {"name", "value", "domain", "path", "expiry", "secure", "httpOnly", "sameSite"}


class PortalError(Exception):
    """Базовая ошибка работы с личным кабинетом."""


class LoginFailed(PortalError):
    """Неверный логин/пароль (или портал не показал страницу после входа)."""


class SessionExpired(PortalError):
    """Cookie больше не действуют — портал просит войти заново."""


class PortalUnavailable(PortalError):
    """Портал или браузер недоступны."""


@dataclass(frozen=True)
class Page:
    path: str
    marker_css: str  # селектор таблицы, по которому понимаем, что страница загрузилась


PLAN_PAGE = Page("/student/plan/", "table.decanat-student-plan")
MESSAGES_PAGE = Page("/messages/?limit=100", "table.list_table")
TIMETABLE_PAGE = Page("/timetable/", "table.timetable-week")


def looks_like_login_page(html: str) -> bool:
    return f'name="{LOGIN_FIELD}"' in html or f"name='{LOGIN_FIELD}'" in html


def has_marker(html: str, css: str) -> bool:
    return BeautifulSoup(html, "lxml").select_one(css) is not None


class PortalClient:
    def __init__(self, base_url: str, fetch_mode: str = "auto", max_browsers: int = 2,
                 page_timeout: int = 20, http_timeout: int = 15) -> None:
        self._base = base_url.rstrip("/")
        self._mode = fetch_mode
        self._slots = asyncio.Semaphore(max_browsers)  # ограничиваем число одновременных Chrome
        self._page_timeout = page_timeout
        self._http_timeout = http_timeout

    # ---------- публичный API ----------

    async def login(self, login: str, password: str) -> Cookies:
        async with self._slots:
            return await asyncio.to_thread(self._login_sync, login, password)

    async def fetch(self, page: Page, cookies: Cookies) -> str:
        if self._mode in ("auto", "http"):
            try:
                html = await self._fetch_http(page, cookies)
            except httpx.HTTPError as exc:
                logger.warning("HTTP-запрос к %s не удался: %s", page.path, exc)
                if self._mode == "http":
                    raise PortalUnavailable("Портал не отвечает") from exc
            else:
                if looks_like_login_page(html):
                    raise SessionExpired("Сессия истекла")
                if self._mode == "http" or has_marker(html, page.marker_css):
                    return html
                logger.info("На %s нет таблицы в HTTP-ответе, пробую браузер", page.path)

        async with self._slots:
            html = await asyncio.to_thread(self._fetch_browser_sync, page, cookies)
        if looks_like_login_page(html):
            raise SessionExpired("Сессия истекла")
        return html

    # ---------- HTTP ----------

    async def _fetch_http(self, page: Page, cookies: Cookies) -> str:
        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        async with httpx.AsyncClient(
            base_url=self._base, cookies=jar, headers={"User-Agent": USER_AGENT},
            timeout=self._http_timeout, follow_redirects=True,
        ) as client:
            response = await client.get(page.path)
            response.raise_for_status()
            return response.text

    # ---------- Selenium (выполняется в потоке) ----------

    @staticmethod
    def _new_driver() -> webdriver.Chrome:
        options = Options()
        for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"):
            options.add_argument(arg)
        return webdriver.Chrome(options=options)  # драйвер подберёт Selenium Manager

    def _login_sync(self, login: str, password: str) -> Cookies:
        driver = None
        try:
            driver = self._new_driver()
            driver.get(self._base)
            WebDriverWait(driver, self._page_timeout).until(
                EC.presence_of_element_located((By.NAME, LOGIN_FIELD))
            )
            driver.find_element(By.NAME, LOGIN_FIELD).send_keys(login)
            driver.find_element(By.NAME, PASSWORD_FIELD).send_keys(password)
            driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, LOGOUT_ICON))
                )
            except TimeoutException as exc:
                raise LoginFailed("После отправки формы кнопка выхода не появилась") from exc
            return driver.get_cookies()
        except WebDriverException as exc:
            # логин и пароль в логи не пишем
            logger.error("Браузер или портал недоступны при входе: %s", type(exc).__name__)
            raise PortalUnavailable("Не удалось открыть личный кабинет") from exc
        finally:
            if driver is not None:
                driver.quit()

    def _fetch_browser_sync(self, page: Page, cookies: Cookies) -> str:
        driver = None
        try:
            driver = self._new_driver()
            driver.get(self._base)  # нужен домен, чтобы добавить cookie
            for cookie in cookies:
                try:
                    driver.add_cookie({k: v for k, v in cookie.items() if k in ALLOWED_COOKIE_KEYS})
                except WebDriverException:
                    continue
            driver.get(self._base + page.path)
            try:  # ждём таблицу, а не спим фиксированные 3 секунды
                WebDriverWait(driver, self._page_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, page.marker_css))
                )
            except TimeoutException:
                pass  # вернём что есть: парсер сообщит, что данных нет
            return driver.page_source
        except WebDriverException as exc:
            logger.error("Браузер не смог открыть %s: %s", page.path, type(exc).__name__)
            raise PortalUnavailable("Не удалось загрузить страницу") from exc
        finally:
            if driver is not None:
                driver.quit()
