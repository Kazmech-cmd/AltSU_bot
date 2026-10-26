"""Разбор HTML-страниц личного кабинета.

Все функции чистые: на вход HTML-строка, на выходе данные. Никакой сети и браузера —
поэтому их легко тестировать на сохранённых страницах.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Tag


@dataclass(frozen=True)
class Subject:
    number: str
    code: str  # индекс дисциплины, например «Б1.О.01»
    title: str
    lecture_hours: str
    practice_hours: str
    lab_hours: str
    self_hours: str
    total_hours: str
    controls: tuple[str, ...] = ()  # виды контроля: у одной дисциплины их может быть несколько
    grades: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    teachers: tuple[str, ...] = ()
    group: str = ""  # «Факультативные дисциплины», «Профильный модуль …» и т.п.


@dataclass(frozen=True)
class Semester:
    title: str  # «Семестр 3» или «Переаттестованные дисциплины»
    subjects: tuple[Subject, ...]


@dataclass(frozen=True)
class CoursePlan:
    course: int
    year: str  # учебный год, например «2026-2027»
    semesters: tuple[Semester, ...]
    total: int | None = None  # «Всего дисциплин с промежуточной аттестацией»
    passed: int | None = None  # «Сдано»


@dataclass(frozen=True)
class StudyPlan:
    """Учебный план целиком: на странице кабинета лежат сразу все курсы."""

    courses: dict[int, CoursePlan]
    selected_course: int | None = None  # курс, который кабинет показывает сам

    def current_course(self, today: date) -> int | None:
        """Текущий курс: выбранный на странице, иначе тот, чей учебный год сейчас идёт."""
        if self.selected_course in self.courses:
            return self.selected_course
        for number, course in sorted(self.courses.items()):
            if _year_contains(course.year, today):
                return number
        return max(self.courses) if self.courses else None


@dataclass(frozen=True)
class Message:
    subject: str
    recipient: str
    sender: str
    date: str


@dataclass(frozen=True)
class Lesson:
    number: str
    time: str
    text: str  # пустая строка — занятия нет


# день недели -> занятия по порядку
Timetable = dict[str, list[Lesson]]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(node: Tag) -> str:
    return node.get_text(" ", strip=True)


def _year_contains(year: str, today: date) -> bool:
    """Учебный год «2026-2027» идёт с 1 сентября 2026 по 31 августа 2027."""
    match = re.search(r"(\d{4})\D+(\d{4})", year)
    if not match:
        return False
    return date(int(match.group(1)), 9, 1) <= today <= date(int(match.group(2)), 8, 31)


def _divs(cell: Tag) -> tuple[str, ...]:
    return tuple(_text(div) for div in cell.find_all("div"))


def _teachers(cell: Tag) -> tuple[str, ...]:
    names = (_text(block.find("span")) for block in cell.select("div.t_gray") if block.find("span"))
    return tuple(name for name in names if name)


def _counter(block: Tag, selector: str) -> int | None:
    node = block.select_one(f"{selector} span:last-child")
    match = re.search(r"\d+", _text(node)) if node else None
    return int(match.group()) if match else None


def _parse_subject(cells: list[Tag], group: str) -> Subject:
    title_node = cells[2].select_one(".decanat-subject-title span") or cells[2]
    return Subject(
        number=_text(cells[0]),
        code=_text(cells[1]),
        title=_text(title_node),
        lecture_hours=_text(cells[3]),
        practice_hours=_text(cells[4]),
        lab_hours=_text(cells[5]),
        self_hours=_text(cells[6]),
        total_hours=_text(cells[7]),
        controls=_divs(cells[8]),
        grades=_divs(cells[9]),
        dates=_divs(cells[10]),
        teachers=_teachers(cells[11]),
        group=group,
    )


def _parse_course(number: int, block: Tag, table: Tag) -> CoursePlan:
    year_node = block.select_one(".toolbar_2 b")
    semesters: list[tuple[str, list[Subject]]] = []
    group = ""

    # Идём по строкам и «осиротевшим» ячейкам в порядке следования на странице.
    # Осиротевшая ячейка — это заголовок первого семестра: в вёрстке кабинета сломан тег <tbody
    # (нет закрывающей «>»), из-за чего строка с «Семестр N» теряет свой <tr>.
    for element in table.find_all(["tr", "td"]):
        heading = None
        if element.name == "td":
            if element.parent.name == "tr":
                continue
            heading = _text(element)
        else:
            cells = element.find_all("td", recursive=False)
            if len(cells) >= 12:
                if not semesters:
                    semesters.append(("", []))
                semesters[-1][1].append(_parse_subject(cells, group))
                continue
            if len(cells) == 1 and cells[0].get("colspan") == "12":
                heading = _text(cells[0])
        if not heading:
            continue
        if heading.startswith(("Семестр", "Переаттестованные")):
            semesters.append((heading, []))
            group = ""
        else:  # «Факультативные дисциплины», «Практикум …», «Профильный модуль …»
            group = heading

    return CoursePlan(
        course=number,
        year=_text(year_node) if year_node else "",
        semesters=tuple(Semester(title, tuple(subjects)) for title, subjects in semesters if subjects),
        total=_counter(block, ".decanat-summary_controls-total"),
        passed=_counter(block, ".decanat-summary_controls-passed"),
    )


def parse_study_plan(html: str) -> StudyPlan | None:
    """None — если на странице нет ни одного курса."""
    soup = _soup(html)
    courses: dict[int, CoursePlan] = {}
    for block in soup.select("div.tool-tab-semester"):
        match = re.search(r"tab-semester-(\d+)", block.get("data-tab", ""))
        table = block.find("table", class_="decanat-student-plan")
        if match and table is not None:
            number = int(match.group(1))
            courses[number] = _parse_course(number, block, table)
    if not courses:
        return None

    selected = soup.select_one(".decanat-student_plan-semesters .toolbar-tab.selected[data-plan-course]")
    selected_course = int(selected["data-plan-course"]) if selected and selected["data-plan-course"].isdigit() else None
    return StudyPlan(courses=courses, selected_course=selected_course)


def parse_messages(html: str) -> list[Message]:
    messages: list[Message] = []
    # без "tbody": в «сыром» HTML (не из браузера) этого тега может не быть
    for row in _soup(html).select("table.list_table tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        divs = cells[3].find_all("div")
        recipient = _text(divs[-1]) if divs else _text(cells[3])
        messages.append(
            Message(
                subject=_text(cells[2]),
                recipient=recipient,
                sender=_text(cells[4]),
                date=_text(cells[5]),
            )
        )
    return messages


def parse_timetable(html: str) -> Timetable | None:
    """None — если таблицы расписания на странице нет."""
    table = _soup(html).find("table", class_="timetable-week")
    if table is None:
        return None

    head = table.find("thead") or table.find("tr")
    if head is None:
        return None
    days = [_text(th) for th in head.find_all("th")[2:]]
    if not days:
        return None

    timetable: Timetable = {day: [] for day in days}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < len(days) + 2:
            continue
        number, time = _text(cells[0]), _text(cells[1])
        for i, day in enumerate(days):
            lines = [line for line in cells[i + 2].get_text("\n", strip=True).split("\n") if line]
            timetable[day].append(Lesson(number=number, time=time, text=" · ".join(lines)))
    return timetable
