"""
Отрисовка клавиатуры Telegram для дерева решений.

Здесь ЕДИНСТВЕННОЕ место, где логика дерева встречается с Telegram.
UI-компонент универсален: он читает Node/NodeType и не знает ни про
конкретные ветки, ни про количество уровней.

callback_data ограничена 64 байтами, поэтому в кнопку едет только код
действия, а состояние живёт в сессии (см. flow.py).
"""

from __future__ import annotations

from typing import Dict, List

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
CB_CRUMB = "c"      # прыжок по breadcrumb (arg = сколько шагов оставить)
CB_RESTART = "r"    # полный сброс
CB_CONFIRM = "k"    # подтвердить финальный результат
CB_EDIT = "e"       # вернуться к редактированию с финального экрана
CB_CANCEL = "x"     # закрыть меню


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


def node_keyboard(session: Session) -> Dict[str, list]:
    """
    Клавиатура текущего шага: ТОЛЬКО варианты текущего уровня.

    Никаких «показать всё дерево сразу»: глубина раскрывается по шагам.
    """
    node = session.current_node()

    rows: List[list] = []

    if node is None or node.type == NodeType.FINAL:
        return {"inline_keyboard": []}

    # Черновая ветка: вариантов ещё нет — просим описать словами.
    # Оставляем только Cancel, как и на прочих текстовых шагах.
    if node.is_stub and not node.options:
        return {"inline_keyboard": [[
            {"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)},
        ]]}

    if node.type in (NodeType.CHOICE, NodeType.YESNO, NodeType.MULTI):
        # Только варианты, доступные при текущем пути (only_for).
        visible = session.visible_options(node)

        # **Две колонки, когда вариантов много.**
        #
        # Владелец: «нужно сделать кнопки в два столбца на модулях, так будет
        # лучше, так как модулей много». Одна колонка растягивала экран: у
        # robot_module пять вариантов — это пять строк подряд, и на телефоне
        # приходилось листать, чтобы увидеть последний.
        #
        # Порог в три варианта выбран по замеру: список из трёх и меньше
        # читается сверху вниз одним взглядом, и две колонки там только дробят
        # внимание. С четырёх — уже выгодно.
        #
        # **Подписи переносятся, а не обрезаются.** Telegram не даёт задать
        # ширину кнопки, поэтому в две колонки длинный текст («Dirty or damaged
        # floor code») сам переносится на вторую строку внутри кнопки. Это
        # приемлемо: важнее видеть все варианты на одном экране, чем читать
        # каждый в полную ширину по очереди.
        if len(visible) >= 4:
            for index in range(0, len(visible), 2):
                pair = [
                    _button(node, option, option.id in session.draft)
                    for option in visible[index : index + 2]
                ]
                rows.append(pair)
        else:
            for option in visible:
                rows.append([_button(node, option, option.id in session.draft)])

        if node.type == NodeType.MULTI:
            rows.append([{
                "text": f"{DONE_LABEL} ({len(session.draft)})",
                "callback_data": callback(CB_DONE),
            }])

    # Текстовые шаги: кнопок выбора нет. Оставляем только Cancel —
    # Back и Restart убраны, чтобы не засорять экран.
    if node.type in (NodeType.INPUT, NodeType.NUMBER) or (
        node.is_stub and not node.options
    ):
        return {
            "inline_keyboard": [[
                {"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)},
            ]]
        }

    # Навигация: только Cancel.
    rows.append([{"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)}])

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
    rows.append([
        {"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)},
    ])

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
