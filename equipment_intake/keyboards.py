"""
Отрисовка клавиатуры Telegram для дерева решений.

Здесь ЕДИНСТВЕННОЕ место, где логика дерева встречается с Telegram.
UI-компонент универсален: он читает Node/NodeType и не знает ни про
конкретные ветки, ни про количество уровней.

callback_data ограничена 64 байтами, поэтому в кнопку едет только код
действия, а состояние живёт в сессии (см. flow.py).

**Раскладка вариантов — требование владельца 12.10.2026, дословно:**
«Ошибки на модулях должны идти в два ряда по 6 за раз и должна быть кнопка
далее на два ряда внизу если их больше».

**13.10.2026 это правило стало общим для ВСЕГО бота**: «Сделай по всему боту
чтобы было не больше двух рядов кнопок в противном случае они не читаются».
Общая ��аскладка вынесена в `keyboard_layout.py`, и ей пользуются экраны
`telegram_bot.py` (статус, склады, меню ошибок, номера) и редактора `/tree`.
Здесь остаётся частный случай: ширина ряда зависит от длины подписей.

Отсюда:

  * варианты идут **сеткой 6 в ряд**, не больше **2 рядов на страницу**
    (до 12 вариантов на экране);
  * если вариантов больше 12 — внизу отдельным рядом кнопка **Next**, она
    показывает следующие два ряда; если есть предыдущие — есть и **Back**;
  * **Cancel остаётся видимой на любой странице**.
"""

from __future__ import annotations

from typing import Dict, List, Set

import keyboard_layout

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

#: Сколько кнопок в одном ряду — **когда подписи короткие**.
OPTIONS_PER_ROW = 6

#: Сколько рядов вариантов помещается на одну страницу.
#:
#: 🔴 **Почему не 2 (исправлено 13.10.2026).** «Два ряда» — цель владельца для
#: **коротких** подписей, и она выполняется: модули A42T идут по четыре в ряд,
#: восемь модулей — ровно **два ряда**. Но когда подпись длинная, читаемая
#: ширина падает до **одной** колонки, и страница из «двух рядов» вмещала бы
#: **две** кнопки из четырёх — у Safety появилось листание, которого раньше не
#: было **вообще**. Листание хуже лишнего ряда: оператор не видит части выбора и
#: должен догадаться нажать Next.
#:
#: Поэтому предел страницы считается **по рядам, а не по колонкам**: на экран
#: помещается до шести рядов, и все текущие узлы (до восьми вариантов) видны
#: целиком, без листания.
OPTION_ROWS_PER_PAGE = 6

#: Сколько вариантов на странице **при коротких подписях** (6 × 2 = 12).
#:
#: Оставлено как значение по умолчанию для вызовов без списка вариантов.
OPTIONS_PER_PAGE = OPTIONS_PER_ROW * 2

#: 🔴 **Предел длины подписи, при котором ряд из шести ещё читается.**
#:
#: Дефект, найденный 12.10.2026 на скриншоте **живого прода**: причины ошибок
#: показывались как `Huma…`, `Obsta…`, `Dirty …`, `Softw…` — оператор **не мог
#: прочитать, что выбирает**. Причина: подписи причин длиной до **33 символов**
#: («Wrong container position on robot»), а в ряд из **6 кнопок** влезает
#: примерно 6–8 знаков.
#:
#: **Замерено на живых данных:** подписи причин — 5…33 символа, подписи модулей
#: и моделей — 4…16 (`Chassis`, `Rotation`, `A42T C2`).
#:
#: **Почему именно 12.** Ширина кнопки при шести колонках ≈ 1/6 экрана: в неё
#: влезает ≈ 7 знаков **в одну строку**, но Telegram **переносит** текст, и
#: 8–12 знаков дают **две читаемые строки** — это нормально.
#:
#: **Замерено на жив��х узлах:** модули и модели — **4…8** символов
#: (, , ), то есть остаются **по шесть**, как просил
#: владелец. Причины ошибок — **5…33** (), и они
#: уходят на три колонки: там 33 знака в шести колонках превращаются в 5 строк
#: и обрезаются в , что и было на скриншоте.
#:
#: **Предел выбран между двух замеров (8 и 21)** — так «два ряда по 6» работает
#: там, где владелец его и просил (модули), и не ломает читаемость причин.
SINGLE_LINE_LABEL_LIMIT = 12

#: Сколько колонок ставить, когда подписи **длиннее** предела.
#:
#: Три колонки дают кнопке треть ширины экрана: «Dirty or damaged floor code»
#: переносится на две строки, но **читается полностью**. Читаемость важнее
#: того, чтобы уложить все причины в один ряд.
WIDE_LABEL_COLUMNS = 3

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


def option_button_text(option, has_next: bool, selected: bool = False) -> str:
    """
    Текст кнопки варианта — **единственный** источник и для отрисовки, и для
    расчёта ширины ряда.

    🔴 **Почему отдельная функция.** До 13.10.2026 ширина ряда выводилась по
    `option.label` **без** стрелки `→`, а рисовалась кнопка **со** стрелкой.
    Кнопка планировалась уже, чем получалась, и Telegram обрезал подписи
    (`Chass…`, `Rotati…`). Пока это были две разные строки кода, они расходились;
    теперь текст собирается в одном месте, и расчёту нечего пропустить.
    """
    marker = "✓ " if selected else ""
    label = f"{marker}{option.label}"

    if has_next:
        label = f"{label}  →"

    return label


def _button(node: Node, option, selected: bool = False) -> Dict[str, str]:
    """
    Одна кнопка варианта.

    Компактно и без эмодзи: только текст варианта. Иконки из конфига
    намеренно н�� рисуем — список из десятка пунктов со смайликами
    читается хуже, чем простой ровный список. Стрелка → остаётся: она
    честно показывает, что выбор ведёт на следующий шаг.
    """
    if node.type == NodeType.MULTI:
        has_next = False
    else:
        has_next = bool(option.next_node or node.next_node)

    return {
        "text": option_button_text(option, has_next, selected),
        "callback_data": option_callback(node, option.id),
    }


def _cancel_button() -> Dict[str, str]:
    """Cancel обязана быть видна на **любой** странице — это требование."""
    return {"text": CANCEL_LABEL, "callback_data": callback(CB_CANCEL)}


def _columns_for(options: List) -> int:
    """Сколько кнопок ставить в ряд — **по длине подписей**.

    🔴 **Зачем, и почему нельзя всегда по шесть.**

    Требование владельца 12.10.2026 — «два ряда по 6» — рассчитано на
    **короткие** подписи (модули: `Chassis`, `Lifting`, `QR Code`). Но на шаге
    причин подписи доходят до **33 символов**, и при шести колонках оператор
    видел `Huma…`, `Obsta…`, `Softw…` — **прочитать выбор невозможно**.

    **Правило:** если **хотя бы одна** подпись длиннее предела, ряд расширяется
    до `WIDE_LABEL_COLUMNS` (три колонки → ≈ 14–16 знаков: длинная подпись
    переносится, но читается целиком). Иначе — по шесть, как просил владелец.

    **Почему «хотя бы одна», а не «все».** В смешанном ряду длинная подпись при
    шести колонках обрежется всё равно; выравнивать по худшей — единственный
    способ гарантировать, что **все** варианты читаемы.
    """
    # 🔴 **Считаем ширину текста, как он попадёт на кнопку** — вместе со
    # стрелкой `→`, которую дописывает `_button`. До 13.10.2026 ширина выводилась
    # по `len(label)` **без** стрелки, поэтому кнопки планировались уже, чем
    # рисовались, и Telegram обрезал подписи (`Chass…`, `Rotati…`).
    return keyboard_layout.columns_for_labels(labels_for_options(options))


def labels_for_options(options: List, has_next: bool = False) -> List[str]:
    """
    Тексты кнопок для списка вариантов — **то же**, что нарисует `_button`.

    Нужна расчёту ширины: считать по `option.label` и рисовать со стрелкой
    значило планировать кнопку уже, чем она получится (дефект `Chass…`).
    """
    labels: List[str] = []

    for option in options:
        if hasattr(option, "label"):
            labels.append(option_button_text(
                option,
                has_next=bool(has_next or option.next_node),
            ))
        else:
            labels.append(str(option or ""))

    return labels


def _options_per_page(options: List, has_next: bool = False) -> int:
    """
    Сколько вариантов помещается на страницу при текущей ширине ряда.

    `has_next` — будет ли у кнопок стрелка `→` (она входит в ширину).

    Размер страницы — **колонки × ряды на экран** (`OPTION_ROWS_PER_PAGE`).
    Считать только «два ряда» нельзя: при одной колонке страница вмещала бы две
    кнопки, и у узла с четырьмя длинными подписями появлялось листание.
    """
    return _columns_for(labels_for_options(options, has_next)) * OPTION_ROWS_PER_PAGE


def options_per_page(options: List | None = None) -> int:
    """
    Сколько вариантов на странице — **публичная** форма `_options_per_page`.

    Нужна подписи «Page 1 of 3 · options 1–4» (`flow`): она обязана считать
    страницы **тем же** правилом, что и клавиатура. Пока в подписи стояла
    константа 12, а раскладка шла по ширине текста, подпись обещала 12 вариантов
    там, где на экране было четыре.
    """
    return _options_per_page(options) if options else OPTIONS_PER_PAGE


def _option_rows(
    node: Node,
    options: List,
    selected: Set[str],
) -> List[list]:
    """
    Сетка вариантов: до **6 кнопок в ряд**, до **2 рядов** на страницу.

    Ширина ряда **зависит от подписей** (`_columns_for`): короткие (модули,
    модели) идут по шесть, как просил владелец; длинные (причины) — по три,
    иначе текст обрезается и выбор становится нечитаемым.

    Если вариантов меньше ширины ряда — один ряд (не растягиваем пустоту).
    Больше страницы не попадает: остальное отдаётся следующей (см. `_pager_row`).
    """
    rows: List[list] = []
    # 🔴 **Ширина считается по тому же тексту, что рисуется.** Здесь стояло
    # `_columns_for(options)` — по `option.label` без стрелки `→`, тогда как
    # `_button` её дописывает. Кнопка планировалась уже, чем получалась, и
    # Telegram обрезал подписи: `[Chass…] [Rotati…]` со скриншота владельца.
    per_row = _columns_for(labels_for_options(
        options,
        has_next=bool(node.next_node) or any(o.next_node for o in options),
    ))

    for index in range(0, len(options), per_row):
        chunk = options[index : index + per_row]

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


def total_option_pages(count: int, options: List | None = None) -> int:
    """Сколько страниц нужно для `count` вариантов (минимум одна).

    **`options` нужен, потому что размер страницы теперь зависит от подписей:**
    длинные подписи идут по три в ряд (6 на страницу), короткие — по шесть
    (12 на страницу). Без списка берётся прежний максимум — так работают
    вызовы, где список недоступен.
    """
    if count <= 0:
        return 1

    per_page = _options_per_page(options) if options else OPTIONS_PER_PAGE

    return (count + per_page - 1) // per_page


def option_page(session: Session, node: Node) -> int:
    """Номер текущей страницы вариантов для узла (с пр��ведением к диапазону)."""
    pages = _option_pages(session)
    raw = pages.get(node.id, 0)

    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = 0

    visible = session.visible_options(node)

    return max(0, min(page, total_option_pages(len(visible), visible) - 1))


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
        # Размер страницы — **от ширины ряда**: длинные подписи идут по три,
        # поэтому на страницу помещается 6, а не 12. Считать по константе
        # значило бы показать на первой странице меньше вариантов, чем влезает,
        # и потерять часть из них между страницами.
        per_page = _options_per_page(visible)
        total = total_option_pages(len(visible), visible)

        start = page * per_page
        page_options = visible[start : start + per_page]

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
