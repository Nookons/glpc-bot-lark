"""
Дерево решений производственного приёма ошибок.

Путь сотрудника — два уровня выбора, затем два текстовых шага:

    Object → Type → Device number → Description → Summary

    Robot            → A42T C2 | A42T | K50H → module → номер → описание
    Workstation      → Pick | Conveyor | Tally      → module → номер → описание
    Charging station → For A42T / C2 | For K50H     → номер → описание
    QR Code          → Shelf | Floor                → номер → описание

Почему так (по требованию заказчика):
  * сотруднику нужно выбрать всего две вещи — что это и какая разновидность;
  * уникальный номер оборудования вводится руками: список техники меняется,
    закрыть его кнопками нельзя;
  * описание — свободным текстом. Именно эти описания потом собираются и
    превращаются в готовые варианты ошибок (см. `data/ANALYSIS.md` — разбор
    реальной выгрузки и `tools/build_tree_from_errors.py`).

Дерево задано кодом осознанно: это зафиксированная бизнес-логика, а не
догадка. Расширять просто — добавить вариант в нужный узел.

`load_tree()` дополнительно принимает дерево из файла, строки JSON или URL —
на случай, когда структура поедет из backend или админки.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .types import (
    DecisionTree,
    Node,
    NodeType,
    choice_node,
    opt,
)
from .validation import (
    KIND_QR_FLOOR,
    KIND_QR_SHELF,
    KIND_ROBOT,
    KIND_STATION,
    KIND_WORKSTATION,
)


# ============================================================
# ПОСТРОЕНИЕ УЗЛОВ
# ============================================================

# Следующий шаг после выбора разновидности — всегда один и тот же:
# спросить номер оборудования, затем описание.
ASK_NUMBER = "ask_number"
ASK_DESCRIPTION = "ask_description"
#: **Второй номер зарядной ветки — робот, который стоял на станции.**
#:
#: Решение владельца 12.10.2026, дословно: «если зарядка то сначала спрашиваем
#: зарядку потом номер робота и храним данные на сервере корректно». В запись
#: идут **оба**: станция в `device_number`, робот в `robot_number` (историческое
#: `error_robot`). До этой правки ветка спрашивала **один** номер, и на вопрос
#: «Which charging station?» оператор отвечал номером робота — ровно тот класс
#: дефекта, из-за которого в `device_number` QR-строк оказались `3492` и `2668`.
ASK_CHARGING_ROBOT = "ask_charging_robot"
#: Вопрос о причине ошибки — **владелец: «добавить причину ошибки как ещё один
#: вопрос»**. Отв��чает на «почему это случилось», а не «что сломалось»:
#: описание проблемы и её причина — разные вещи, и раньше причину не спрашивали
#: вовсе, поэтому в отчётах её не было.
ASK_CAUSE = "ask_cause"
SUMMARY = "summary"


def _build_nodes() -> dict:
    nodes = {}

    def add(node: Node) -> None:
        nodes[node.id] = node

    # ------------------------------------------------------------
    # Шаг 1: что за оборудование
    # ------------------------------------------------------------
    add(choice_node(
        "root",
        "What is shown in the photo?",
        key="object",
        summary_label="Object",
        description="Choose what is in the photo.",
        options=(
            opt("robot", "Robot", "robot_type"),
            opt("workstation", "Workstation", "workstation_type"),
            opt("charger", "Charging station", "charger_type"),
            opt("qr", "QR Code", "qr_type"),
        ),
    ))

    # ------------------------------------------------------------
    # Шаг 2: разновидность внутри выбранного оборудования
    # ------------------------------------------------------------
    add(choice_node(
        "robot_type",
        "Which robot?",
        key="device_type",
        summary_label="Type",
        description="Choose the robot model.",
        options=(
            opt("a42t_c2", "A42T C2", "robot_module_a42t"),
            opt("a42t", "A42T", "robot_module_a42t"),
            opt("k50h", "K50H", "robot_module_k50h"),
        ),
    ))

    add(choice_node(
        "workstation_type",
        "Which workstation?",
        key="device_type",
        summary_label="Type",
        description="Choose the workstation type.",
        options=(
            opt("ws_pick", "Pick", "workstation_module"),
            opt("ws_conveyor", "Conveyor", "workstation_module"),
            opt("ws_tally", "Tally", "workstation_module"),
        ),
    ))

    # ------------------------------------------------------------
    # Модули робота — **отдельный узел на каждую модель**
    # ------------------------------------------------------------
    #
    # **Владелец 07.10.2026:** «нужно как-то сделать модуля отдельно на роботов,
    # потому что например на K50H мы имеем только 3 модуля это lift, chassis and
    # safety, больше нету — и нету смысла показывать все в таком случае».
    #
    # **Почему отдельные узлы, а не один с `only_for`.** Механизм `only_for` в
    # дереве есть и работае�� (`visible_options` фильтрует варианты по пути), но
    # валидатор запрещает **одинаковые id вариантов внутри узла** — а `lifting`
    # нужен и K50H, и A42T. Отдельные узлы снимают это ограничение честно:
    # у каждой модели свой список модулей, и его правит редактор независимо.
    # Побочно это точнее отвечает на вопрос «какие модули у этой модели».
    #
    # Идентификатор варианта остаётся **человеческим** (`lifting`, `chassis`):
    # он попадает в отчёт и в статистику, поэтому префикс модели к нему не
    # добавляю — иначе в отчётах появились бы `k50h_lifting` и `a42t_lifting`
    # как разные модули, хотя это один и тот же узел железа.
    # **Заголовок называет модель — иначе узлы не различить.**
    #
    # Дефект, найденный владельцем 07.10.2026: «не понятно как настроить модули
    # на роботах, текста одинаковые». Разделив модули по моделям, я дал обоим
    # узлам **один и тот же** заголовок «Which robot module?», и в редакторе
    # (`/tree`) они выглядели как две одинаковые строки, различимые только по
    # техническому id в скобках.
    #
    # Теперь заголовок отвечает на вопрос «какие модули у **этой** модели» и
    # работа��т как подпись в списке редактора.
    add(choice_node(
        "robot_module_k50h",
        "K50H: which module?",
        key="module",
        summary_label="Module",
        description="K50H has three modules: lifting, chassis and safety.",
        options=(
            opt("lifting", "Lifting", ASK_NUMBER),
            opt("chassis", "Chassis", ASK_NUMBER),
            opt("safety", "Safety", ASK_NUMBER),
            # Ошибка бывает не в модуле: сбой программы, грязный код пола,
            # посторонний предмет. Без этого варианта человек выбирал чужой
            # модуль наугад и портил статистику.
            opt("not_a_module", "Not a module", ASK_NUMBER),
        ),
    ))

    add(choice_node(
        "robot_module_a42t",
        "A42T / A42T C2: which module?",
        key="module",
        summary_label="Module",
        description="A42T and A42T C2 have lifting, rotation, tray and chassis.",
        options=(
            opt("lifting", "Lifting", ASK_NUMBER),
            opt("rotation", "Rotation", ASK_NUMBER),
            opt("tray", "Tray", ASK_NUMBER),
            opt("chassis", "Chassis", ASK_NUMBER),
            opt("not_a_module", "Not a module", ASK_NUMBER),
        ),
    ))

    add(choice_node(
        "workstation_module",
        "Workstation: which module?",
        key="module",
        summary_label="Module",
        description=(
            "Same list for Pick, Conveyor and Tally. "
            "Choose the faulty module or add an option via /tree."
        ),
        options=(
            opt("offline", "Offline", ASK_NUMBER),
            opt("wrong_task", "Wrong task", ASK_NUMBER),
        ),
    ))

    add(choice_node(
        "charger_type",
        "Which charging station?",
        key="device_type",
        summary_label="Type",
        description="Choose which robot this station is for.",
        options=(
            # Обе ветки ведут на ввод номера **станции**; робот спрашивается
            # отдельным шагом сразу после (см. `ask_charging_robot`).
            opt("cs_a42t_c2", "Charge for big robot", ASK_NUMBER),
            opt("cs_k50h", "Charge for small robot", ASK_NUMBER),
        ),
    ))

    add(choice_node(
        "qr_type",
        "Which QR code?",
        key="device_type",
        summary_label="Type",
        description="Choose where the QR code is located.",
        options=(
            opt("qr_shelf", "Shelf", ASK_NUMBER),
            opt("qr_floor", "Floor", ASK_NUMBER),
        ),
    ))

    # ------------------------------------------------------------
    # Шаг 3: уникальный номер оборудования
    # ------------------------------------------------------------
    # Вводится руками: у техники бывают составные номера вида «H108/1834».
    #
    # **Подсказка и проверка зависят от ветки, а узел — один.**
    # `placeholder` у узла один, поэтому на ветке QR оператор видел пример
    # **номера робота** и вводил робота (дефект владельца 12.10.2026: 16
    # QR-строк с номерами `3492`, `2668`). Вид номера выводится из ПУТИ
    # (`number_kind_for` ниже): карта живёт в коде и наложением правок
    # редактора не теряется. Отдельные узлы на каждую ветку разошлись бы —
    # редактор правит по одному, и в дереве появились бы мёртвые двойники.
    #
    # `answer_kind` на узле — запасной ответ для веток, которых нет в карте.
    add(Node(
        id=ASK_NUMBER,
        title="Device number",
        type=NodeType.INPUT,
        key="device_number",
        summary_label="Device",
        description="Unique equipment number.",
        placeholder="For example: 3780 or H108/1834",
        answer_kind="robot",
        # Зарядная ветка: после номера станции спрашиваем робота, который на
        # ней стоял. Остальные ветки идут сразу к описанию.
        next_node_by_kind=((KIND_STATION, ASK_CHARGING_ROBOT),),
        next_node=ASK_DESCRIPTION,
    ))

    # ------------------------------------------------------------
    # Шаг 3б: робот, стоявший на зарядной станции
    # ------------------------------------------------------------
    # **Владелец 12.10.2026:** «если зарядка то сначала спрашиваем зарядку
    # потом номер робота и храним данные на сервере корректно». Поэтому
    # станция и робот — **два номера и две колонки**: станция в
    # `device_number`, робот в `robot_number`. Прежняя ветка спрашивала один
    # номер, и на вопрос про станцию оператор отвечал роботом.
    #
    # Робот нужен, потому что без него ошибка зарядки не связана с роботом и
    # не попадает в метрики по устройству.
    add(Node(
        id=ASK_CHARGING_ROBOT,
        title="Which robot was charging?",
        type=NodeType.INPUT,
        key="error_robot",
        summary_label="Robot",
        description="The robot that was on this charging station.",
        placeholder="For example: 3780 or H108/1834",
        answer_kind="robot",
        next_node=ASK_DESCRIPTION,
    ))

    # ------------------------------------------------------------
    # Шаг 4: описание проблемы
    # ------------------------------------------------------------
    # Свободный текст — главный сбор сырых данных. По нему потом видно,
    # какие формулировки повторяются, и из них делаются готовые варианты.
    add(Node(
        id=ASK_DESCRIPTION,
        title="Describe the problem",
        type=NodeType.INPUT,
        key="description",
        summary_label="Problem",
        description="Describe the problem in your own words.",
        placeholder="For example: does not lift the forks, red light is blinking",
        next_node=ASK_CAUSE,
    ))

    # ------------------------------------------------------------
    # Шаг 5: причина ошибки
    # ------------------------------------------------------------
    # **Владелец: «добавить причину ошибки как ещё один вопрос».** Это не
    # повтор описания: описание отвечает «что не так» («не поднимает вилы»),
    # причина — «почему» («наехал на посторонний предмет»). Раньше причина не
    # спрашивалась, поэтому в отчётах её не было, и разбор повторяющихся ошибок
    # упирался в догадки.
    #
    # Список — стартовый набор из практики склада; правится через редактор
    # (`/tree`), как и остальные узлы. «Other» оставлен намеренно: закрытый
    # список без него заставляет выбирать неточное и портит статистику.
    add(choice_node(
        ASK_CAUSE,
        "What caused it?",
        key="cause",
        summary_label="Cause",
        description="Why it happened — helps to prevent a repeat.",
        options=(
            opt("human_error", "Human error", SUMMARY),
            opt("obstacle", "Obstacle on the path", SUMMARY),
            opt("dirty_code", "Dirty or damaged floor code", SUMMARY),
            opt("mechanical_wear", "Mechanical wear", SUMMARY),
            opt("software_fault", "Software fault", SUMMARY),
            opt("battery_charge", "Battery or charging", SUMMARY),
            opt("other", "Other", SUMMARY),
        ),
    ))

    # ------------------------------------------------------------
    # Финал
    # ------------------------------------------------------------
    add(Node(
        id=SUMMARY,
        title="Inspection result",
        type=NodeType.FINAL,
        summary_title="Inspection result",
        final_action="save_inspection",
    ))

    return nodes


# ============================================================
# ВИД НОМЕРА — ЗАВИСИТ ОТ ВЕТКИ, А НЕ ОТ УЗЛА
# ============================================================

#: Какой номер вводится на этом шаге — по варианту, который оператор уже
#: выбрал. Ключ — `option.id` из дерева (или из правок редактора: id тот же).
#:
#: **Зачем карта, если у узла есть `answer_kind`.** `ask_number` один на всё
#: дерево, а `ask_charging_robot` — соседний узел. Вид номера задаёт **ветка**,
#: и её видно в пути. Карта живёт в коде, поэтому наложение правок редактора её
#: не теряет: правки меняют подписи и переходы, но не id вариантов.
_NUMBER_KIND_BY_OPTION = {
    # Шаг 1 — что за оборудование.
    "robot": KIND_ROBOT,
    "workstation": KIND_WORKSTATION,
    "charger": KIND_STATION,
    "qr": KIND_QR_FLOOR,
    # Шаг 2 — разновидность.
    "a42t_c2": KIND_ROBOT,
    "a42t": KIND_ROBOT,
    "k50h": KIND_ROBOT,
    "ws_pick": KIND_WORKSTATION,
    "ws_conveyor": KIND_WORKSTATION,
    "ws_tally": KIND_WORKSTATION,
    "qr_shelf": KIND_QR_SHELF,
    "qr_floor": KIND_QR_FLOOR,
}

#: Вид номера по конкретному узлу (там, где ветку определяет сам узел).
_NUMBER_KIND_BY_NODE = {
    ASK_CHARGING_ROBOT: KIND_ROBOT,
}


def number_kind_for(path=None, node=None) -> str:
    """
    Вид номера на текущем шаге: `robot`, `station`, `workstation`,
    `qr_floor`, `qr_shelf`.

    **Почему по пути, а не по узлу.** Узел `ask_number` один, и подсказка у
    него одна — поэтому оператор на ветке QR видел пример **номера робота** и
    вводил робота (16 таких QR-строк на 12.10.2026). Вид вводимого значения
    задаёт **ветка**, а ветка — это выбранные варианты в пути.

    Порядок разбора: сначала конкретный узел (робот на зарядке), затем
    варианты пути от последнего к первому — так более близкий выбор уточняет
    более ранний. Пустой путь или неизвестная ветка дают `robot`: это обычный
    узел номера, каким он и был до правки.
    """
    if node is not None and getattr(node, "id", None) in _NUMBER_KIND_BY_NODE:
        return _NUMBER_KIND_BY_NODE[node.id]

    for step in reversed(list(path or ())):
        kind = _NUMBER_KIND_BY_OPTION.get(str(getattr(step, "option_id", "") or ""))

        if kind:
            return kind

    return KIND_ROBOT


#: Пример ввода для каждой ветки — то, что оператор видит подсказкой.
#: **Пример обязан совпадать с веткой:** именно несовпадение и рождало
#: «номер робота в поле кода полки».
NUMBER_EXAMPLES = {
    KIND_ROBOT: "For example: 3780 or H108/1834",
    KIND_STATION: "For example: 137",
    KIND_WORKSTATION: "For example: C2-H143",
    KIND_QR_FLOOR: "For example: SS512-020118-0101",
    KIND_QR_SHELF: "For example: Shelf-138-039-1",
}


def number_example(kind: str) -> str:
    """Подсказка-пример для ветки (по умолчанию — как у робота)."""
    return NUMBER_EXAMPLES.get(kind, NUMBER_EXAMPLES[KIND_ROBOT])


# ============================================================
# КОРНЕВОЕ ДЕРЕВО
# ============================================================

# Подписи кнопок навигации.
#
# Намеренно БЕЗ эмодзи и коротко: на телефоне и в перчатках кнопка должна
# читаться мгновенно, а не пестрить. Списки вариантов тоже без иконок.
BACK_LABEL = "Back"
RESTART_LABEL = "Restart"
CANCEL_LABEL = "Cancel"
DONE_LABEL = "Done"
SKIP_LABEL = "Skip"


# Файл со сгенерированным из данных деревом (результат
# tools/build_tree_from_errors.py). По умолчанию НЕ используется: логика
# выше — авторитетная. Данные остаются материалом для будущего расширения,
# когда описаний накопится достаточно.
GENERATED_TREE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tree.generated.json"
)


def _authoritative_tree() -> DecisionTree:
    """Дерево по бизнес-логике заказчика."""
    tree = DecisionTree(root_id="root", nodes=_build_nodes())
    tree.normalize()
    tree.validate()

    return tree


def _load_generated_tree() -> Optional[DecisionTree]:
    """
    Дерево из `tree.generated.json`, если файл есть и читается.

    Нужно только для явного запроса (см. load_tree("generated")). Ошибку
    глотаем намеренно — отсутствие файла не должно ломать импорт пакета.
    """
    try:
        with open(GENERATED_TREE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None

    return DecisionTree.from_dict(data)


# Дерево по умолчанию — зафиксированная логика заказчика.
DEFAULT_TREE = _authoritative_tree()


# ============================================================
# ЗАГРУЗКА ДЕРЕВА (статический конфиг ИЛИ backend)
# ============================================================

def load_tree(source: Optional[str] = None) -> DecisionTree:
    """
    Возвращает дерево решений.

    Без аргумента — встроенная логика заказчика.

    source допускает:
      * None/"default" — встроенное дерево;
      * "generated" — дерево, собранное из выгрузки ошибок;
      * путь к JSON-файлу (локальная проверка);
      * строку с JSON (например, ответ backend);
      * URL http(s):// — дерево грузится с backend и кэшируется.

    Это и есть точка расширения: чтобы дерево приходило из БД, достаточно
    отдать JSON того же формата (DecisionTree.to_dict()).
    """
    if not source or source == "default":
        return DEFAULT_TREE

    if source == "generated":
        return _load_generated_tree() or DEFAULT_TREE

    if source.startswith("http://") or source.startswith("https://"):
        return _load_tree_from_url(source)

    if os.path.exists(source):
        with open(source, "r", encoding="utf-8") as handle:
            return DecisionTree.from_dict(json.load(handle))

    return DecisionTree.from_dict(json.loads(source))


# Кэш загруженного дерева: backend не дёргаем на каждый шаг.
_CACHE: dict = {}


def _load_tree_from_url(url: str) -> DecisionTree:
    if url in _CACHE:
        return _CACHE[url]

    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    tree = DecisionTree.from_dict(payload)
    _CACHE[url] = tree

    return tree


def clear_cache() -> None:
    """Сброс кэша backend-дерева (например, после правок в админке)."""
    _CACHE.clear()
