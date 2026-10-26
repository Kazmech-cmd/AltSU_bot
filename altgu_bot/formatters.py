"""Превращение данных в текст для Telegram (parse_mode=HTML).

Любой текст из личного кабинета экранируется: в нём могут быть символы < > &,
которые ломают разметку (в старой версии на этом падал Markdown).
"""
from __future__ import annotations

import html
from collections.abc import Iterable

from .parsers import CoursePlan, Message, Subject, Timetable

# Лимит Telegram — 4096 символов; берём с запасом, т.к. экранирование удлиняет текст.
SAFE_LIMIT = 3500


def pack(parts: Iterable[str], limit: int = SAFE_LIMIT, sep: str = "\n", prefix: str = "") -> list[str]:
    """Склеивает части в сообщения не длиннее `limit`; `prefix` повторяется в каждом."""
    room = limit - len(prefix)
    pieces: list[str] = []
    for part in parts:  # слишком длинную часть режем жёстко
        pieces.extend(part[i : i + room] for i in range(0, max(len(part), 1), room))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        extra = len(piece) + (len(sep) if current else 0)
        if current and size + extra > room:
            chunks.append(prefix + sep.join(current))
            current, size, extra = [], 0, len(piece)
        current.append(piece)
        size += extra
    if current:
        chunks.append(prefix + sep.join(current))
    return chunks


def _cut(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


SHORT_CONTROLS = {"Курсовой проект": "КП", "Курсовая работа": "КР", "Зачет с оценкой": "Зач/оц"}
# ширины колонок таблицы (моноширинный шрифт): предмет, лек, пр, лаб, контроль, оценка, дата
W_TITLE, W_HOURS, W_CONTROL, W_GRADE, W_DATE = 22, 3, 11, 7, 10
TABLE_HEADER = (
    f"{'Предмет':<{W_TITLE}} {'Лек':>{W_HOURS}} {'Пр':>{W_HOURS}} {'Лаб':>{W_HOURS}} "
    f"{'Контроль':<{W_CONTROL}} {'Оценка':<{W_GRADE}} {'Дата':<{W_DATE}}"
).rstrip()
TABLE_RULE = "-" * len(TABLE_HEADER)


def _subject_line(s: Subject) -> str:
    controls = "+".join(SHORT_CONTROLS.get(c, c) for c in s.controls if c)
    grades = "/".join(g for g in s.grades if g)
    date_ = next((d for d in reversed(s.dates) if d), "")  # при нескольких контролях — последняя дата
    return (
        f"{_cut(s.title, W_TITLE):<{W_TITLE}} {s.lecture_hours:>{W_HOURS}} {s.practice_hours:>{W_HOURS}} "
        f"{s.lab_hours:>{W_HOURS}} {_cut(controls, W_CONTROL):<{W_CONTROL}} "
        f"{_cut(grades, W_GRADE):<{W_GRADE}} {_cut(date_, W_DATE):<{W_DATE}}"
    ).rstrip()


def format_course(course: CoursePlan) -> list[str]:
    """Один курс: заголовок, затем по сообщению (или нескольким) на каждый семестр."""
    header = f"📘 <b>{course.course} курс, {_esc(course.year)}</b>"
    if course.total is not None and course.passed is not None:
        header += f"\nСдано: {course.passed} из {course.total}"

    messages: list[str] = []
    for semester in course.semesters:
        lines: list[str] = []
        last_group = ""
        for subject in semester.subjects:
            if subject.group != last_group:
                last_group = subject.group
                if subject.group:
                    lines.append("▸ " + _cut(subject.group, len(TABLE_HEADER) - 2))
            lines.append(_subject_line(subject))

        # экранируем до разбиения, чтобы длина сообщения считалась честно
        prefix = _esc(TABLE_HEADER) + "\n" + _esc(TABLE_RULE) + "\n"
        chunks = pack([_esc(line) for line in lines], limit=3800, prefix=prefix)
        for i, chunk in enumerate(chunks):
            title = _esc(semester.title) + (" (продолжение)" if i else "")
            messages.append(f"<b>{title}</b>\n<pre>{chunk}</pre>")

    if not messages:
        return [header + "\n\nДисциплин нет."]
    messages[0] = header + "\n\n" + messages[0]
    return messages


def format_timetable(timetable: Timetable | None) -> list[str]:
    if not timetable:
        return []
    blocks = []
    for day, lessons in timetable.items():
        lines = [f"{html.escape(l.number)} ({html.escape(l.time)}): {html.escape(l.text)}" for l in lessons if l.text]
        blocks.append(f"<b>{html.escape(day)}</b>\n" + ("\n".join(lines) if lines else "<i>занятий нет</i>"))
    return pack(blocks, sep="\n\n", prefix="📅 <b>Расписание на неделю</b>\n\n")


def timetable_to_text(timetable: Timetable | None) -> str:
    """Простой текст без разметки — для передачи в ИИ."""
    if not timetable:
        return "Расписание отсутствует."
    out = []
    for day, lessons in timetable.items():
        lines = [f"  {l.number} ({l.time}): {l.text}" for l in lessons if l.text]
        out.append(day + ":\n" + ("\n".join(lines) if lines else "  занятий нет"))
    return "\n".join(out)


def format_messages(messages: list[Message], limit: int = 10) -> list[str]:
    blocks = [
        f"📬 <b>{html.escape(m.subject)}</b>\nОт: {html.escape(m.sender)}\n"
        f"Кому: {html.escape(m.recipient)}\nДата: {html.escape(m.date)}"
        for m in messages[:limit]
    ]
    return pack(blocks, sep="\n\n")
