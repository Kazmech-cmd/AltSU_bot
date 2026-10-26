#!/usr/bin/env python3
"""Проверка проекта перед публикацией на GitHub: не утекут ли секреты и личные данные.

Использование (из корня проекта, внутри git-репозитория):

    python scripts/check_secrets.py                 # проверить всё, что попадёт в репозиторий
    python scripts/check_secrets.py --staged        # только то, что уже добавлено в коммит (для хука)
    python scripts/check_secrets.py --install-hook  # проверять автоматически перед каждым коммитом

Код возврата 0 — всё чисто, 1 — найдено что-то опасное. Сами секреты скрипт НЕ печатает целиком,
показывает только первые символы.

Ложное срабатывание (например, тестовый пароль) можно погасить комментарием «# secrets-ok» в этой строке.

Свои «запретные слова» (ФИО, телефон, e-mail...) положите по одному в строке в файл
scripts/deny_words.local.txt — он в .gitignore и никуда не уходит. Образец: deny_words.example.txt.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENY_FILE = ROOT / "scripts" / "deny_words.local.txt"
IGNORE_MARKER = "secrets-ok"  # допишите в комментарий к строке, если это заведомо не секрет
MAX_FILE_SIZE = 1_000_000  # байт; крупнее — почти наверняка не исходный код

# (название, регулярка). Регулярки намеренно простые и с запасом: лучше лишнее предупреждение, чем утечка.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("токен Telegram-бота", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")),
    ("ключ OpenAI", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("ключ Google (AIza...)", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("ключ Google (AQ....)", re.compile(r"\bAQ\.[A-Za-z0-9_-]{20,}")),
    ("ключ GitHub", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}")),
    ("ключ AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("приватный ключ", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    (
        "значение секрета в коде",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|token)\b\s*[=:]\s*['"][^'"\s]{12,}['"]"""),
    ),
    (
        "значение секрета в стиле .env",
        re.compile(r"(?m)^\s*[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*[^\s#'\"]{12,}"),
    ),
]

# Файлы, которым в публичном репозитории делать нечего (маски по пути с «/»).
FORBIDDEN_PATHS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx",
    "venv/*", ".venv/*", "*.log", "*.zip", "*.7z", "*.rar", "*.sqlite", "*.sqlite3", "*.db",
    "scripts/deny_words.local.txt", "*.local.txt",
]
ALLOWED_PATHS = [".env.example"]
# Сохранённые страницы личного кабинета легко содержат ФИО и номера: HTML разрешён только в tests/fixtures.
HTML_ALLOWED_DIRS = ("tests/fixtures/",)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int  # 0 — проблема в самом имени файла
    kind: str
    preview: str = ""

    def __str__(self) -> str:
        where = self.path if self.line == 0 else f"{self.path}:{self.line}"
        return f"{where}: {self.kind}" + (f"  [{self.preview}]" if self.preview else "")


def mask(value: str) -> str:
    """Показываем только начало, чтобы по выводу нельзя было восстановить секрет."""
    value = value.strip()
    return value[:4] + "…" if len(value) > 4 else "…"


def is_forbidden_path(path: str) -> str | None:
    """Возвращает причину, если файл нельзя публиковать, иначе None."""
    path = path.replace("\\", "/")
    if any(fnmatch.fnmatch(path, pattern) for pattern in ALLOWED_PATHS):
        return None
    name = path.rsplit("/", 1)[-1]
    for pattern in FORBIDDEN_PATHS:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern):
            return f"файл не должен попадать в репозиторий (маска {pattern})"
    if name.lower().endswith((".html", ".htm")) and not path.startswith(HTML_ALLOWED_DIRS):
        return "HTML-страница вне tests/fixtures (в сохранённых страницах кабинета бывают ваши данные)"
    return None


def load_deny_words(path: Path = DENY_FILE) -> list[str]:
    if not path.exists():
        return []
    words = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


def scan_text(path: str, text: str, deny_words: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    deny = [w.lower() for w in (deny_words or [])]
    for number, line in enumerate(text.splitlines(), start=1):
        if IGNORE_MARKER in line:  # осознанное исключение для ложного срабатывания
            continue
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(Finding(path, number, kind, mask(match.group(0))))
        lowered = line.lower()
        for word in deny:
            if word in lowered:
                findings.append(Finding(path, number, "ваше «запретное слово» из deny_words.local.txt", mask(word)))
    return findings


def _git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def _split_z(data: bytes) -> list[str]:
    return [p.decode("utf-8", errors="replace") for p in data.split(b"\0") if p]


def collect(staged: bool, root: Path = ROOT) -> list[tuple[str, bytes]]:
    """Список (путь, содержимое) того, что окажется в репозитории."""
    if staged:
        names = _split_z(_git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z", cwd=root))
        return [(n, _git("show", f":{n}", cwd=root)) for n in names]
    names = _split_z(_git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=root))
    files = []
    for name in names:
        file = root / name
        if file.is_file():
            files.append((name, file.read_bytes()))
    return files


def env_is_ignored(root: Path = ROOT) -> bool | None:
    """True/False — игнорируется ли .env; None — файла .env нет."""
    if not (root / ".env").exists():
        return None
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=root)
    return result.returncode == 0


def check(files: list[tuple[str, bytes]], deny_words: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path, data in files:
        reason = is_forbidden_path(path)
        if reason:
            findings.append(Finding(path, 0, reason))
            continue
        if len(data) > MAX_FILE_SIZE or b"\0" in data:
            continue  # крупные и двоичные файлы пропускаем
        findings.extend(scan_text(path, data.decode("utf-8", errors="ignore"), deny_words))
    return findings


HOOK_TEMPLATE = """#!/bin/sh
# Установлено scripts/check_secrets.py --install-hook
"{python}" scripts/check_secrets.py --staged || {{
  echo "Коммит отменён: найдены секреты или личные данные (см. выше)."
  exit 1
}}
"""


def install_hook(root: Path = ROOT) -> Path:
    hooks = root / ".git" / "hooks"
    if not hooks.parent.exists():
        raise SystemExit("Это не git-репозиторий. Сначала выполните: git init")
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    # абсолютный путь к тому Python, которым запущен скрипт, — так хук не зависит от PATH
    hook.write_text(HOOK_TEMPLATE.format(python=sys.executable.replace("\\", "/")), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return hook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка секретов перед публикацией")
    parser.add_argument("--staged", action="store_true", help="проверять только файлы, добавленные в коммит")
    parser.add_argument("--install-hook", action="store_true", help="установить проверку перед каждым коммитом")
    args = parser.parse_args(argv)

    if args.install_hook:
        hook = install_hook()
        print(f"Готово: проверка будет запускаться перед каждым коммитом ({hook}).")
        return 0

    try:
        _git("rev-parse", "--git-dir")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Не найден git-репозиторий (или не установлен git). Сначала: git init", file=sys.stderr)
        return 1

    findings = check(collect(args.staged), load_deny_words())
    if not args.staged and env_is_ignored() is False:
        findings.append(Finding(".env", 0, ".env существует, но НЕ игнорируется git — добавьте его в .gitignore"))

    if findings:
        print("НАЙДЕНО (публиковать нельзя, пока не исправите):", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        print(
            "\nЕсли секрет уже попал в коммит, одного удаления файла мало: он останется в истории. "
            "Замените токен/ключ на новый.",
            file=sys.stderr,
        )
        return 1

    print("Проверено: секретов и запрещённых файлов не найдено.")
    if not load_deny_words():
        print("Совет: добавьте свои ФИО/телефон/почту в scripts/deny_words.local.txt (образец: deny_words.example.txt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
