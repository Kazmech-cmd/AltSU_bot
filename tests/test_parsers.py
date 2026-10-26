from datetime import date

import pytest

from altgu_bot.parsers import parse_messages, parse_study_plan, parse_timetable


# ---------- учебный план ----------

@pytest.fixture
def plan(load):
    return parse_study_plan(load("plan.html"))


def _subject(plan, title):
    for course in plan.courses.values():
        for semester in course.semesters:
            for subject in semester.subjects:
                if subject.title == title:
                    return subject
    raise AssertionError(f"нет дисциплины {title!r}")


def test_plan_contains_all_three_courses(plan):
    assert sorted(plan.courses) == [1, 2, 3]
    assert [plan.courses[n].year for n in (1, 2, 3)] == ["2025-2026", "2026-2027", "2027-2028"]


def test_plan_selected_course_is_taken_from_the_page(plan):
    assert plan.selected_course == 2


def test_first_semester_is_found_despite_broken_tbody_tag(plan):
    # в вёрстке кабинета тег <tbody без «>» «съедает» строку с заголовком первого семестра
    assert [s.title for s in plan.courses[1].semesters] == ["Семестр 1", "Семестр 2", "Переаттестованные дисциплины"]
    assert [s.title for s in plan.courses[2].semesters] == ["Семестр 3", "Семестр 4"]
    assert [s.title for s in plan.courses[3].semesters] == ["Семестр 5", "Семестр 6"]


def test_subjects_are_counted_per_semester(plan):
    assert [len(s.subjects) for s in plan.courses[1].semesters] == [7, 3, 2]
    assert [len(s.subjects) for s in plan.courses[2].semesters] == [6, 8]


def test_summary_counters(plan):
    assert (plan.courses[1].total, plan.courses[1].passed) == (11, 9)
    assert (plan.courses[2].total, plan.courses[2].passed) == (14, 1)


def test_course_without_counters_gives_none(plan):
    assert (plan.courses[3].total, plan.courses[3].passed) == (None, None)


def test_subject_columns(plan):
    s = _subject(plan, "Дискретная математика")
    assert (s.number, s.code) == ("9", "Б1.О.04")
    assert (s.lecture_hours, s.practice_hours, s.lab_hours, s.self_hours, s.total_hours) == ("16", "0", "26", "66", "108")
    assert (s.controls, s.grades, s.dates) == (("Зачет",), ("Зач",), ("16.12.2025",))


def test_subject_without_hours_keeps_empty_strings(plan):
    s = _subject(plan, "Математика")
    assert (s.lecture_hours, s.total_hours) == ("", "") and s.grades == ("Отл",)


def test_subject_without_control_cells_has_empty_tuples(plan):
    s = _subject(plan, "История России")
    assert (s.controls, s.grades, s.dates) == ((), (), ())


def test_empty_grade_div_is_kept_as_empty_string(plan):
    s = _subject(plan, "Основы программирования")
    assert s.grades == ("",) and s.dates == ("",)


def test_several_controls_in_one_subject(plan):
    s = _subject(plan, "Алгоритмизация и программирование")
    assert s.controls == ("Курсовой проект", "Экзамен")
    assert s.grades == ("Отл", "Хор") and s.dates == ("12.09.2025", "26.06.2026")


def test_title_is_taken_from_link_text(plan):
    assert _subject(plan, "Web-технологии").title == "Web-технологии"
    assert _subject(plan, "UX & UI дизайн")


def test_teachers_are_parsed_and_missing_ones_are_empty(plan):
    assert _subject(plan, "Дизайн и разработка Web-приложений").teachers == ("Сидоров Сергей Петрович", "Кузнецова Алина Александровна")
    assert _subject(plan, "Математика").teachers == ()


def test_groups_for_electives_and_modules(plan):
    assert _subject(plan, "Введение в профессию (адаптивная дисциплина для лиц с ограниченными возможностями здоровья)").group == "Факультативные дисциплины"
    assert _subject(plan, "Политика и управление").group == 'Практикум "Человек в современном мире" (дисциплины по выбору)'
    assert _subject(plan, "Основы управления IT-сервисами по ITIL").group == 'Профильный модуль "Управление IT-проектами"'
    assert _subject(plan, "Автоматизация управления предприятием: CRM").group == 'Профильный модуль "ERP-системы и прикладное программирование"'


def test_group_does_not_leak_into_next_semester(plan):
    assert _subject(plan, "Web-технологии").group == ""
    assert _subject(plan, "Big Data и ETL-системы").group == ""


def test_alternative_electives_keep_the_same_number(plan):
    electives = [s for s in plan.courses[2].semesters[0].subjects if s.code == "Б1.О.01.ДВ.01"]
    assert len(electives) == 3 and {s.number for s in electives} == {"9"}


def test_service_rows_are_not_subjects(plan):
    titles = [s.title for c in plan.courses.values() for sem in c.semesters for s in sem.subjects]
    assert not any(t.startswith("Итого") for t in titles)


def test_plan_without_courses_is_none():
    assert parse_study_plan("<html><body>ничего</body></html>") is None


def test_plan_empty_string_is_none():
    assert parse_study_plan("") is None


# --- какой курс считать текущим ---

def test_current_course_prefers_the_one_selected_on_the_page(plan):
    assert plan.current_course(date(2025, 10, 1)) == 2


@pytest.mark.parametrize("today, expected", [
    (date(2025, 10, 1), 1),
    (date(2026, 10, 24), 2),
    (date(2027, 8, 31), 2),
    (date(2027, 9, 1), 3),
])
def test_current_course_falls_back_to_academic_year(load, today, expected):
    unselected = load("plan.html").replace("toolbar-tab selected", "toolbar-tab")
    assert parse_study_plan(unselected).selected_course is None
    assert parse_study_plan(unselected).current_course(today) == expected


def test_current_course_after_graduation_is_the_last_one(load):
    unselected = load("plan.html").replace("toolbar-tab selected", "toolbar-tab")
    assert parse_study_plan(unselected).current_course(date(2031, 1, 1)) == 3


# ---------- сообщения ----------

def test_messages_parse_and_skip_footer_row(load):
    messages = parse_messages(load("messages.html"))
    assert [m.subject for m in messages] == ["Изменение в расписании", "Напоминание об оплате"]


def test_messages_recipient_is_last_div(load):
    assert parse_messages(load("messages.html"))[0].recipient == "Студент Тестов"


def test_messages_recipient_without_div_falls_back_to_cell_text(load):
    assert parse_messages(load("messages.html"))[1].recipient == "Студент Тестов"


def test_messages_fields(load):
    first = parse_messages(load("messages.html"))[0]
    assert (first.sender, first.date) == ("Деканат", "01.10.2026")


def test_messages_without_table_is_empty():
    assert parse_messages("<p>пусто</p>") == []


def test_messages_work_without_tbody():
    html = ('<table class="list_table"><tr><td></td><td></td><td>Тема</td>'
            "<td>Я</td><td>Они</td><td>03.10.2026</td></tr></table>")
    assert len(parse_messages(html)) == 1


# ---------- расписание ----------

def test_timetable_days(load):
    assert list(parse_timetable(load("timetable.html"))) == ["Понедельник", "Вторник", "Среда"]


def test_timetable_lessons(load):
    monday = parse_timetable(load("timetable.html"))["Понедельник"]
    assert len(monday) == 3
    assert (monday[0].number, monday[0].time) == ("1", "08:30–10:05")
    assert monday[0].text == "Математика · ауд. 101"


def test_timetable_empty_cell_is_empty_text(load):
    tuesday = parse_timetable(load("timetable.html"))["Вторник"]
    assert tuesday[0].text == ""
    assert tuesday[1].text == "Программирование · ауд. 305"


def test_timetable_without_table_is_none():
    assert parse_timetable("<html></html>") is None


def test_timetable_without_day_columns_is_none():
    html = '<table class="timetable-week"><thead><tr><th>№</th><th>Время</th></tr></thead></table>'
    assert parse_timetable(html) is None
