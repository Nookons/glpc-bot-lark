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


# ============================================================
# ПОСТРОЕНИЕ УЗЛОВ
# ============================================================

# Следующий шаг после выбора разновидности — всегда один и тот же:
# спросить номер оборудования, затем описание.
ASK_NUMBER = "ask_number"
ASK_DESCRIPTION = "ask_description"
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
            opt("a42t_c2", "A42T C2", "robot_module"),
            opt("a42t", "A42T", "robot_module"),
            opt("k50h", "K50H", "robot_module"),
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

    add(choice_node(
        "robot_module",
        "Which robot module?",
        key="module",
        summary_label="Module",
        description="Choose the faulty module or add an option via /tree.",
        options=(
            opt("lifting", "Lifting", ASK_NUMBER),
            opt("rotation", "Rotation", ASK_NUMBER),
            opt("tray", "Tray", ASK_NUMBER),
            opt("chassis", "Chassis", ASK_NUMBER),
        ),
    ))

    add(choice_node(
        "workstation_module",
        "Which workstation module?",
        key="module",
        summary_label="Module",
        description="Choose the faulty module or add an option via /tree.",
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
    # Вводится руками: у техники бывают составные номера вида «H108/1834»
    # (13% записей в реальной выгрузке). Проверку формата не навязываем,
    # чтобы не блокировать сотрудника на месте.
    add(Node(
        id=ASK_NUMBER,
        title="Device number",
        type=NodeType.INPUT,
        key="device_number",
        summary_label="Device",
        description="Unique equipment number.",
        placeholder="For example: 3780 or H108/1834",
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
        next_node=SUMMARY,
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
