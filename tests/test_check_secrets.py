"""Тесты проверки секретов. Фальшивые «секреты» собираются из кусков, чтобы сами тесты не срабатывали на сканер."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import check_secrets as cs

PROJECT = Path(__file__).resolve().parent.parent
HAS_GIT = shutil.which("git") is not None

TG_TOKEN = "123456789" + ":" + "A" * 35
OPENAI_KEY = "sk-" + "proj" + "x" * 30
AIZA_KEY = "AIza" + "S" * 35
AQ_KEY = "AQ." + "Ab8RN6LvW0RiJ38m" + "x" * 20
GH_TOKEN = "ghp_" + "a" * 36


def kinds(findings):
    return {f.kind for f in findings}


# ---------- поиск секретов в тексте ----------

@pytest.mark.parametrize("secret, expected", [
    (TG_TOKEN, "токен Telegram-бота"),
    (OPENAI_KEY, "ключ OpenAI"),
    (AIZA_KEY, "ключ Google (AIza...)"),
    (AQ_KEY, "ключ Google (AQ....)"),
    (GH_TOKEN, "ключ GitHub"),
])
def test_known_secret_formats_are_found(secret, expected):
    found = cs.scan_text("a.py", f"x = '{secret}'\n")
    assert expected in kinds(found)


def test_private_key_header_is_found():
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    assert "приватный ключ" in kinds(cs.scan_text("k.txt", header))


def test_env_style_assignment_is_found():
    assert "значение секрета в стиле .env" in kinds(cs.scan_text(".env", "BOT_TOKEN=" + "z" * 20))


def test_assignment_in_code_is_found():
    line = "pass" + 'word = "' + "q" * 14 + '"'
    assert "значение секрета в коде" in kinds(cs.scan_text("a.py", line))


@pytest.mark.parametrize("line", [
    "BOT_TOKEN=",
    "GEMINI_API_KEY=",
    "# OPENAI_API_KEY=ollama",
    'Settings.from_env({"BOT_TOKEN": "42:TEST"})',
    "self._token_param = 'max_tokens'",
    "Просто обычный текст про токены и ключи.",
])
def test_harmless_lines_are_not_flagged(line):
    assert cs.scan_text("a.py", line) == []


def test_line_numbers_are_reported():
    found = cs.scan_text("a.py", "ok\nok\n" + TG_TOKEN + "\n")
    assert found[0].line == 3


def test_full_secret_is_never_printed():
    for finding in cs.scan_text("a.py", TG_TOKEN + "\n" + OPENAI_KEY):
        assert TG_TOKEN not in str(finding) and OPENAI_KEY not in str(finding)
        assert len(finding.preview) <= 5


# ---------- запретные слова ----------

def test_deny_words_are_case_insensitive():
    found = cs.scan_text("a.txt", "Автор: ИВАНОВ иван", deny_words=["иванов иван"])
    assert len(found) == 1 and "запретное слово" in found[0].kind


def test_deny_words_preview_is_masked():
    found = cs.scan_text("a.txt", "мой телефон 89990001122", deny_words=["89990001122"])
    assert "89990001122" not in str(found[0])


def test_load_deny_words_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "deny.txt"
    f.write_text("# комментарий\n\nИванов\n  89990001122  \n", encoding="utf-8")
    assert cs.load_deny_words(f) == ["Иванов", "89990001122"]


def test_load_deny_words_missing_file(tmp_path):
    assert cs.load_deny_words(tmp_path / "нет.txt") == []


# ---------- запрещённые файлы ----------

@pytest.mark.parametrize("path", [
    ".env", "config/.env", ".env.local", "key.pem", "venv/Scripts/python.exe", "backup.zip",
    "bot.log", "scripts/deny_words.local.txt", "page.html", "saved_pages/plan.html",
])
def test_forbidden_paths(path):
    assert cs.is_forbidden_path(path)


@pytest.mark.parametrize("path", [
    ".env.example", "README.md", "altgu_bot/config.py", "tests/fixtures/plan.html",
    "scripts/deny_words.example.txt",
])
def test_allowed_paths(path):
    assert cs.is_forbidden_path(path) is None


def test_windows_style_paths_are_normalised():
    assert cs.is_forbidden_path("venv\\Lib\\x.py")
    assert cs.is_forbidden_path("tests\\fixtures\\plan.html") is None


def test_forbidden_file_is_reported_without_reading_content():
    found = cs.check([(".env", b"BOT_TOKEN=" + b"z" * 20)], [])
    assert len(found) == 1 and found[0].line == 0


def test_binary_files_are_skipped():
    assert cs.check([("logo.png", b"\x89PNG\0" + TG_TOKEN.encode())], []) == []


# ---------- сам проект чистый ----------

def _project_files():
    """Файлы проекта, которые попадут в репозиторий."""
    if HAS_GIT and (PROJECT / ".git").exists():
        # git сам учитывает .gitignore: ваш личный .env сюда не попадёт
        return cs.collect(staged=False, root=PROJECT)
    skip = {".git", "venv", ".venv", "__pycache__", ".pytest_cache", "v"}
    files = []
    for path in PROJECT.rglob("*"):
        rel = path.relative_to(PROJECT)
        if path.is_file() and not (set(rel.parts) & skip) and path.name != ".env":
            files.append((rel.as_posix(), path.read_bytes()))
    return files


def test_project_itself_is_clean():
    files = _project_files()
    assert files
    local_only = (".local.txt",)  # ваши локальные файлы: в .gitignore, в репозиторий не попадут
    problems = [
        f for f in cs.check(files, [])
        if f.path != ".env" and not f.path.endswith(local_only)
    ]
    assert problems == [], [str(p) for p in problems]


# ---------- настоящий git ----------

pytestmark_git = pytest.mark.skipif(not HAS_GIT, reason="git не установлен")


def _git(root, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=root, capture_output=True, text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    shutil.copy(PROJECT / "scripts" / "check_secrets.py", tmp_path / "scripts" / "check_secrets.py")
    return tmp_path


@pytestmark_git
def test_ignored_env_is_not_collected_and_reported_as_ignored(repo):
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repo / ".env").write_text("BOT_TOKEN=" + "z" * 20, encoding="utf-8")
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    names = [n for n, _ in cs.collect(staged=False, root=repo)]
    assert ".env" not in names and "main.py" in names
    assert cs.env_is_ignored(repo) is True


@pytestmark_git
def test_unignored_env_is_detected(repo):
    (repo / ".env").write_text("BOT_TOKEN=" + "z" * 20, encoding="utf-8")
    assert cs.env_is_ignored(repo) is False
    names = [n for n, _ in cs.collect(staged=False, root=repo)]
    assert ".env" in names
    assert cs.check(cs.collect(staged=False, root=repo), [])  # есть находки


@pytestmark_git
def test_env_missing_gives_none(repo):
    assert cs.env_is_ignored(repo) is None


@pytestmark_git
def test_staged_scan_sees_only_index(repo):
    (repo / "a.py").write_text(f"x = '{TG_TOKEN}'\n", encoding="utf-8")
    (repo / "b.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    staged = cs.collect(staged=True, root=repo)
    assert [n for n, _ in staged] == ["a.py"]
    assert "токен Telegram-бота" in kinds(cs.check(staged, []))


@pytestmark_git
def test_hook_blocks_commit_with_secret_and_allows_clean_commit(repo):
    hook = cs.install_hook(repo)
    assert hook.exists() and "--staged" in hook.read_text(encoding="utf-8")

    (repo / "leak.py").write_text(f"x = '{TG_TOKEN}'\n", encoding="utf-8")
    _git(repo, "add", "leak.py")
    blocked = _git(repo, "commit", "-m", "утечка", check=False)
    assert blocked.returncode != 0
    assert TG_TOKEN not in blocked.stdout + blocked.stderr  # секрет не напечатан

    _git(repo, "reset", "-q")
    (repo / "leak.py").unlink()
    (repo / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", "ok.py")
    assert _git(repo, "commit", "-m", "норм", check=False).returncode == 0


def test_install_hook_requires_git_repo(tmp_path):
    with pytest.raises(SystemExit):
        cs.install_hook(tmp_path)


@pytestmark_git
def test_cli_returns_nonzero_on_findings_and_zero_when_clean(repo):
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    script = repo / "scripts" / "check_secrets.py"
    ok = subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)
    assert ok.returncode == 0
    (repo / "b.py").write_text(f"x = '{OPENAI_KEY}'\n", encoding="utf-8")
    bad = subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)
    assert bad.returncode == 1 and OPENAI_KEY not in bad.stdout + bad.stderr


def test_secrets_ok_marker_silences_a_line():
    line = "pass" + 'word = "' + "q" * 14 + '"'
    assert cs.scan_text("a.py", line) != []
    assert cs.scan_text("a.py", line + "  # secrets-ok: тест") == []


def test_secrets_ok_does_not_hide_other_lines():
    text = f"x = '{TG_TOKEN}'  # secrets-ok\ny = '{OPENAI_KEY}'\n"
    found = cs.scan_text("a.py", text)
    assert [f.line for f in found] == [2]
