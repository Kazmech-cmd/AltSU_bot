import pytest

from altgu_bot.formatters import (
    SAFE_LIMIT, format_course, format_messages, format_timetable, pack, timetable_to_text,
)
from altgu_bot.parsers import (
    CoursePlan, Lesson, Message, Semester, Subject, parse_messages, parse_study_plan, parse_timetable,
)

TELEGRAM_LIMIT = 4096


def _subject(title: str, **extra) -> Subject:
    fields = dict(
        number="1", code="Б1.О", title=title, lecture_hours="16", practice_hours="16", lab_hours="0",
        self_hours="40", total_hours="72", controls=("Экзамен",), grades=("Отл",), dates=("15.01.2026",),
    )
    fields.update(extra)
    return Subject(**fields)


def _course(*semesters: Semester, total=None, passed=None) -> CoursePlan:
    return CoursePlan(course=2, year="2026-2027", semesters=tuple(semesters), total=total, passed=passed)


# ---------- pack ----------

def test_pack_respects_limit():
    chunks = pack(["a" * 40] * 10, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 1


def test_pack_repeats_prefix_in_every_chunk():
    chunks = pack(["x" * 30] * 6, limit=80, prefix="HEAD\n")
    assert len(chunks) > 1 and all(c.startswith("HEAD\n") for c in chunks)


def test_pack_splits_one_oversized_part():
    chunks = pack(["z" * 250], limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "z" * 250


def test_pack_empty_input():
    assert pack([]) == []


# ---------- учебный план ----------

@pytest.fixture
def course2(load):
    return parse_study_plan(load("plan.html")).courses[2]


def test_course_header_and_counters(course2):
    first = format_course(course2)[0]
    assert first.startswith("📘 <b>2 курс, 2026-2027</b>\nСдано: 1 из 14")


def test_course_without_counters_has_no_passed_line(load):
    first = format_course(parse_study_plan(load("plan.html")).courses[3])[0]
    assert "Сдано" not in first and first.startswith("📘 <b>3 курс, 2027-2028</b>")


def test_each_semester_gets_its_own_heading_and_pre_block(course2):
    text = "\n".join(format_course(course2))
    assert "<b>Семестр 3</b>\n<pre>" in text and "<b>Семестр 4</b>\n<pre>" in text
    assert text.count("<pre>") == text.count("</pre>") == 2


def test_table_header_is_present(course2):
    assert "Предмет" in format_course(course2)[0] and "Контроль" in format_course(course2)[0]


def test_group_labels_are_shown_before_their_subjects(course2):
    text = "\n".join(format_course(course2))
    assert '▸ Практикум "Человек в современном мире"' in text
    assert '▸ Профильный модуль "Управление IT-проектами"' in text
    assert text.index('▸ Профильный модуль "Управление') < text.index("Основы управления IT")


def test_group_label_is_not_repeated_for_every_subject(course2):
    text = "\n".join(format_course(course2))
    assert text.count('▸ Практикум "Человек в современном мире"') == 1


def test_html_in_titles_is_escaped(course2):
    text = "\n".join(format_course(course2))
    assert "UX &amp; UI дизайн" in text and "UX & UI" not in text


def test_hostile_title_cannot_inject_markup():
    course = _course(Semester("Семестр <1>", (_subject("<script>x</script> & <b>"),)))
    text = "\n".join(format_course(course))
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "Семестр &lt;1&gt;" in text


def test_several_controls_are_shortened_and_joined(load):
    retaken = parse_study_plan(load("plan.html")).courses[1].semesters[2]
    text = "\n".join(format_course(_course(retaken)))
    assert "КП+Экзамен" in text and "Отл/Хор" in text
    assert "26.06.2026" in text and "12.09.2025" not in text  # для составного контроля берётся последняя дата


def test_credit_with_mark_is_shortened(course2):
    assert "Зач/оц" in "\n".join(format_course(course2))


def test_empty_hours_and_grade_do_not_break_alignment():
    subject = _subject("История", lecture_hours="", practice_hours="", lab_hours="", controls=(), grades=(), dates=())
    line = format_course(_course(Semester("Семестр 1", (subject,))))[0].splitlines()[-1]
    assert "История" in line.replace("<pre>", "").replace("</pre>", "")


def test_long_title_is_truncated(course2):
    assert "…" in "\n".join(format_course(course2))


def test_course_without_semesters_says_so():
    assert "Дисциплин нет" in format_course(_course())[0]


def test_long_semester_splits_into_valid_messages_with_repeated_header():
    subjects = tuple(_subject(f"Дисциплина <{i}> & ещё") for i in range(400))
    messages = format_course(_course(Semester("Семестр 3", subjects)))
    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)
    assert all("Предмет" in m and m.count("<pre>") == 1 for m in messages)
    assert "(продолжение)" in messages[1] and "(продолжение)" not in messages[0]


def test_pre_tags_are_balanced_in_every_message(course2):
    for message in format_course(course2):
        assert message.count("<pre>") == message.count("</pre>") == 1


# ---------- расписание ----------

def test_timetable_skips_empty_lessons_and_marks_free_days(load):
    text = format_timetable(parse_timetable(load("timetable.html")))[0]
    assert "<b>Понедельник</b>" in text
    assert "Математика · ауд. 101" in text
    assert text.count("занятий нет") == 0  # у каждого дня есть пара
    assert "12:10–13:45" not in text  # третья пара пустая везде


def test_timetable_free_day_message():
    timetable = {"Суббота": [Lesson("1", "08:30", "")]}
    assert "занятий нет" in format_timetable(timetable)[0]


def test_timetable_escapes_text():
    timetable = {"Пн": [Lesson("1", "08:30", "<b>хак</b>")]}
    assert "&lt;b&gt;хак&lt;/b&gt;" in format_timetable(timetable)[0]


def test_timetable_none_gives_no_chunks():
    assert format_timetable(None) == []


def test_timetable_to_text_is_plain(load):
    text = timetable_to_text(parse_timetable(load("timetable.html")))
    assert "<" not in text and "Понедельник:" in text and "Математика" in text


def test_timetable_to_text_when_missing():
    assert timetable_to_text(None) == "Расписание отсутствует."


# ---------- сообщения ----------

def test_messages_limit_and_escape(load):
    messages = parse_messages(load("messages.html"))
    out = format_messages(messages, limit=1)
    assert len(out) == 1 and "Изменение в расписании" in out[0]
    assert "Напоминание" not in out[0]


def test_messages_escape_html():
    out = format_messages([Message("<i>тема</i>", "я", "они", "01.01.2026")])
    assert "&lt;i&gt;тема&lt;/i&gt;" in out[0]


def test_safe_limit_below_telegram_limit():
    assert SAFE_LIMIT < TELEGRAM_LIMIT
