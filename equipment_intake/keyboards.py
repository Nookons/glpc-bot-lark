"""
Отрисовка клавиатуры Telegram для дерева решений.

Здесь ЕДИНСТВЕННОЕ место, где логика дерева встречается с Telegram.
UI-компонент универсален: он читает Node/NodeType и не знает ни про
конкретные ветки, ни про количество уровней.

callback_data ограничена 64 байтами, поэтому в кнопку едет только код
действия, а состояние живёт в сессии (см. flow.py).

**Раскладка вариантов — требование владельца 12.10.2026, дословно:**
«Ошибки на модулях должны идти в два ряда по 6 за раз и должна быть кнопка
далее на два ряда внизу если их больше». Отсюда:

  * варианты идут **сеткой 6 в ряд**, не больше **2 рядов на страницу**
    (до 12 вариантов на экране);
  * если вариантов больше 12 — внизу отдельным рядом кнопка **Next**, она
    показывает следующие два ряда; если есть предыдущие — есть и **Back**;
  * **Cancel остаётся видимой на любой странице**.
"""

from __future__ import annotations

from typing import Dict, List, Set

from .engine import Session
from .tree_config import (
    CANCEL_LABEL,
    DONE_LABEL,
)
from .types import Node, NodeType


# Колбэки: dt:<action>[:<arg>]. Короткие, чтобы влезали в 64 байта.
CB_PREFIX = "dt"
CB_SELECT = "s"     # выбрать вариант
CB_TOGGLE = "t"     # отметить вариант в Multi
CB_DONE = "d"       # завершить Multi
CB_BACK = "b"       # назад на шаг
CB_PAGE = "p"       # пролистать страницу вариантов (arg = номер страницы)
CB_CRUMB = "c"      # прыжок по breadcrumb (arg = сколько шагов оставить)
CB_RESTART = "r"    # полный сброс
CB_CONFIRM = "k"    # подтвердить финальный результат
CB_EDIT = "e"       # вернуться к редактированию с финального экрана
CB_CANCEL = "x"     # закрыть меню


# ============================================================
# РАСКЛАДКА: СЕТКА 6 × 2 НА СТРАНИЦУ
# ============================================================

#: Сколько кнопок в одном ряду.
OPTIONS_PER_ROW = 6

#: Сколько рядов вариантов помещается на одну страницу.
OPTION_ROWS_PER_PAGE = 2

#: Сколько вариантов на странице: 6 × 2 = 12.
OPTIONS_PER_PAGE = OPTIONS_PER_ROW * OPTION_ROWS_PER_PAGE

#: Ключ в `session.data`, где лежит страница вариантов **каждого узла**.
#: Словарь `{node_id: page}`, а не одно число: тогда возврат по breadcrumb
#: на прежний узел показывает ту же страницу, и сбрасывать её при каждом
#: переходе не нужно — новый узел просто начинается с нуля.
OPTION_PAGE_KEY = "option_pages"


def callback(action: str, arg: str = "") -> str:
    return f"{CB_PREFIX}:{action}" + (f":{arg}" if arg else "")


def option_callback(node: Node, option_id: str) -> str:
    if node.type == NodeType.MULTI:
        return callback(CB_TOGGLE, option_id)

    return callback(CB_SELECT, option_id)


def _button(node: Node, option, selected: bool = False) -> Dict[str, str]:
    """
    Одна кнопка варианта.

    Компактно и без эмодзи: только текст варианта. Иконки из конфига
    намеренно не рисуем — список из десятка пунктов со смайликами
    читается хуже, чем простой ровный список. Стрелка → остаётся: она
    честно показывает, что выбор ведёт на следующий шаг.
    """
    marker = "✓ " if selected else ""

    if node.type == NodeType.MULTI:
        has_next = False
    else:
        has_next = bool(option.next_node or node.next_node)

    label = f"{marker}{option.label}"

    if has_next:
        label = f"{label}  →"

    return {
        "text": label,
        "callback_data": option_callback(node, option.id),
    }


def _cancel_button() -> Dict[str, str]:
    """Cancel обязана быть видна на **любой** странице — это требование."""
    return {"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)}


def _option_rows(
    node: Node,
    options: List,
    selected: Set[str],
) -> List[list]:
    """
    Сетка вариантов: по 6 кнопок в ряд.

    Если вариантов шесть и меньше — один ряд (не растягиваем пустоту);
    если 7–12 — два ряда. Больше на страницу не попадает: остальное
    отдаётся следующей странице (см. `_pager_row`).
    """
    rows: List[list] = []

    for index in range(0, len(options), OPTIONS_PER_ROW):
        chunk = options[index : index + OPTIONS_PER_ROW]

        rows.append([
            _button(node, option, option.id in selected) for option in chunk
        ])

    return rows


def _option_pages(session: Session) -> Dict[str, int]:
    """
    Карта «узел → выбранная страница».

    Лежит в уже существующем `session.data` (словарь для нужд флоу), поэтому
    новых полей в `types.py` не требуется. Если `data` нет (упрощённая
    сессия в тесте) — работаем с пустой картой, не падая.
    """
    data = getattr(session, "data", None)

    if not isinstance(data, dict):
        return {}

    pages = data.get(OPTION_PAGE_KEY)

    if not isinstance(pages, dict):
        pages = {}
        data[OPTION_PAGE_KEY] = pages

    return pages


def total_option_pages(count: int) -> int:
    """Сколько страниц нужно для `count` вариантов (минимум одна)."""
    if count <= 0:
        return 1

    return (count + OPTIONS_PER_PAGE - 1) // OPTIONS_PER_PAGE


def option_page(session: Session, node: Node) -> int:
    """Номер текущей страницы вариантов для узла (с приведением к диапазону)."""
    pages = _option_pages(session)
    raw = pages.get(node.id, 0)

    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = 0

    return max(0, min(page, total_option_pages(len(session.visible_options(node))) - 1))


def set_option_page(session: Session, page: int) -> None:
    """
    Запомнить страницу вариантов для **текущего** узла.

    Вызывается из флоу на колбэк `dt:p:<page>`. Состояние сессии (путь, шаг)
    не трогается: меняется только номер страницы, поэтому листание не сбрасывает
    диалог и не создаёт новую сессию.
    """
    node = session.current_node()

    if node is None:
        return

    try:
        value = int(page)
    except (TypeError, ValueError):
        value = 0

    _option_pages(session)[node.id] = max(0, value)


def _pager_row(page: int, total: int) -> list:
    """
    Ряд листания под сеткой: Back (если есть предыдущая) и Next (если есть
    следующая страница).

    В ряд попадает не больше двух кнопок, поэтому «далее» всегда стоит
    отдельным рядом под вариантами, как и просил владелец.
    """
    if total <= 1:
        return []

    buttons: List[Dict[str, str]] = []

    if page > 0:
        buttons.append({
            "text": f"◀ Back {page}/{total}",
            "callback_data": callback(CB_PAGE, str(page - 1)),
        })

    if page < total - 1:
        buttons.append({
            "text": f"Next ▶ {page + 2}/{total}",
            "callback_data": callback(CB_PAGE, str(page + 1)),
        })

    return buttons


def node_keyboard(session: Session) -> Dict[str, list]:
    """
    Клавиатура текущего шага: варианты текущего уровня страницами по 12.

    Никаких «показать всё дерево сразу»: глубина раскрывается по шагам, а
    длинные списки — по страницам.
    """
    node = session.current_node()

    rows: List[list] = []

    if node is None or node.type == NodeType.FINAL:
        return {"inline_keyboard": []}

    # Черновая ветка: вариантов ещё нет — просим описать словами.
    # Оставляем только Cancel, как и на прочих текстовых шагах.
    if node.is_stub and not node.options:
        return {"inline_keyboard": [[_cancel_button()]]}

    if node.type in (NodeType.CHOICE, NodeType.YESNO, NodeType.MULTI):
        # Только варианты, доступные при текущем пути (only_for).
        visible = session.visible_options(node)

        page = option_page(session, node)
        total = total_option_pages(len(visible))

        start = page * OPTIONS_PER_PAGE
        page_options = visible[start : start + OPTIONS_PER_PAGE]

        selected = set(session.draft or ())

        rows.extend(_option_rows(node, page_options, selected))

        if node.type == NodeType.MULTI:
            rows.append([{
                "text": f"{DONE_LABEL} ({len(session.draft)})",
                "callback_data": callback(CB_DONE),
            }])

        pager = _pager_row(page, total)

        if pager:
            rows.append(pager)

    # Текстовые шаги: кнопок выбора нет. Оставляем только Cancel —
    # Back и Restart убраны, чтобы не засорять экран.
    if node.type in (NodeType.INPUT, NodeType.NUMBER) or (
        node.is_stub and not node.options
    ):
        return {"inline_keyboard": [[_cancel_button()]]}

    # Навигация: Cancel есть на любой странице, включая последнюю.
    rows.append([_cancel_button()])

    return {"inline_keyboard": rows}


def breadcrumb_keyboard(session: Session, limit: int = 5) -> Dict[str, list]:
    """
    Кликабельный breadcrumb отдельной строкой.

    Каждый предыдущий элемент ведёт ровно на свой уровень. Длинный путь
    укорачиваем, чтобы клавиатура не «расползлась».
    """
    crumbs = session.breadcrumbs()

    if len(crumbs) > limit:
        crumbs = crumbs[:1] + crumbs[-(limit - 1):]

    row: List[Dict[str, str]] = []

    for crumb in crumbs:
        label = f"[{crumb.label}]" if crumb.active else crumb.label

        row.append({
            "text": label[:24],
            "callback_data": callback(CB_CRUMB, str(crumb.index)),
        })

    return {"inline_keyboard": [row]} if row else {"inline_keyboard": []}


def final_keyboard(session: Session) -> Dict[str, list]:
    """Кнопки финального экрана: Edit / Confirm / Cancel."""
    rows: List[list] = []

    rows.append([
        {"text": "Edit selection", "callback_data": callback(CB_EDIT)},
        {"text": "Confirm", "callback_data": callback(CB_CONFIRM)},
    ])
    rows.append([_cancel_button()])

    return {"inline_keyboard": rows}


def keyboard_with_breadcrumb(session: Session) -> Dict[str, list]:
    """
    Клавиатура текущего шага: только варианты и Cancel.

    Breadcrumb-кнопки сверху убраны: путь и так виден в подписи к фото,
    а крошки отнимали место у вариантов. Имя функции оставлено прежним,
    чтобы не менять точки вызова.
    """
    node = session.current_node()

    # Финал: либо явный Final-узел, либо ветка закончилась (current_node None).
    if node is None or node.type == NodeType.FINAL or session.is_completed:
        return final_keyboard(session)

    return node_keyboard(session)
