import pytest

from altgu_bot.config import ConfigError, Settings
from altgu_bot.portal import has_marker, looks_like_login_page


# ---------- настройки ----------

def test_settings_defaults():
    s = Settings.from_env({"BOT_TOKEN": "123:abc"})
    assert s.bot_token == "123:abc"
    assert s.fetch_mode == "auto" and s.portal_url == "https://lk.asu.ru"
    assert s.ai_api_key is None


def test_settings_requires_token():
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        Settings.from_env({})


def test_settings_blank_token_is_missing():
    with pytest.raises(ConfigError):
        Settings.from_env({"BOT_TOKEN": "   "})


def test_settings_rejects_unknown_fetch_mode():
    with pytest.raises(ConfigError, match="FETCH_MODE"):
        Settings.from_env({"BOT_TOKEN": "t", "FETCH_MODE": "magic"})


def test_settings_rejects_non_numeric_ttl():
    with pytest.raises(ConfigError, match="SESSION_TTL"):
        Settings.from_env({"BOT_TOKEN": "t", "SESSION_TTL": "часик"})


def test_settings_rejects_non_positive_numbers():
    with pytest.raises(ConfigError):
        Settings.from_env({"BOT_TOKEN": "t", "MAX_LOGIN_ATTEMPTS": "0"})


def test_settings_reads_overrides_and_strips_trailing_slash():
    s = Settings.from_env({
        "BOT_TOKEN": "t", "PORTAL_URL": "https://example.test/", "FETCH_MODE": "HTTP",
        "OPENAI_API_KEY": "k", "SESSION_TTL": "120",
    })
    assert s.portal_url == "https://example.test"
    assert s.fetch_mode == "http" and s.ai_api_key == "k" and s.session_ttl == 120


# ---------- определение страниц ----------

def test_login_page_detected(load):
    assert looks_like_login_page(load("login.html"))


def test_regular_page_is_not_login_page(load):
    assert not looks_like_login_page(load("timetable.html"))


def test_marker_found_and_missing(load):
    html = load("timetable.html")
    assert has_marker(html, "table.timetable-week")
    assert not has_marker(html, "table.decanat-student-plan")


def test_settings_base_url_default_is_none_and_can_be_set():
    assert Settings.from_env({"BOT_TOKEN": "t"}).ai_base_url is None
    s = Settings.from_env({"BOT_TOKEN": "t", "OPENAI_API_KEY": "ollama", "OPENAI_BASE_URL": " http://localhost:11434/v1 "})
    assert s.ai_base_url == "http://localhost:11434/v1"


# ---------- выбор ИИ-провайдера ----------

from altgu_bot.config import DEFAULT_OPENAI_MODEL, GEMINI_BASE_URL, GEMINI_DEFAULT_MODEL  # noqa: E402


def test_gemini_key_alone_selects_gemini_defaults():
    s = Settings.from_env({"BOT_TOKEN": "t", "GEMINI_API_KEY": "g"})
    assert (s.ai_api_key, s.ai_base_url, s.ai_model) == ("g", GEMINI_BASE_URL, GEMINI_DEFAULT_MODEL)


def test_gemini_model_can_be_overridden():
    s = Settings.from_env({"BOT_TOKEN": "t", "GEMINI_API_KEY": "g", "GEMINI_MODEL": "gemini-x"})
    assert s.ai_model == "gemini-x"


def test_openai_key_wins_over_gemini():
    s = Settings.from_env({"BOT_TOKEN": "t", "GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o"})
    assert s.ai_api_key == "o" and s.ai_base_url is None and s.ai_model == DEFAULT_OPENAI_MODEL


def test_blank_keys_disable_ai():
    s = Settings.from_env({"BOT_TOKEN": "t", "GEMINI_API_KEY": "  ", "OPENAI_API_KEY": ""})
    assert s.ai_api_key is None and s.ai_base_url is None


def test_openai_model_setting_does_not_leak_into_gemini():
    s = Settings.from_env({"BOT_TOKEN": "t", "GEMINI_API_KEY": "g", "OPENAI_MODEL": "gpt-x"})
    assert s.ai_model == GEMINI_DEFAULT_MODEL
