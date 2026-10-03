#!/usr/bin/env python3
"""
Генератор дерева решений из реального экспорта ошибок.

Дерево НЕ пишется руками: структура целиком выводится из TSV-экспорта
(по умолчанию data/sample_errors_2026-09-23_28.tsv). Это принципиально:
список объектов, категорий, типов, причин и решений меняется от выгрузки
к выгрузке, и поддерживать его в коде вручную невозможно.

Формат результата — ровно ``DecisionTree.to_dict()``
(equipment_intake/types.py): ``root_id`` + словарь ``nodes``. Поэтому
файл читается ``DecisionTree.from_dict(...)`` без правок движка и UI.

Уровни (как в данных; индексы колонок 0-based):
    L1 OBJECT      (колонка 1)  «What is shown in the photo?»
    L2 CATEGORY    (колонка 4)  «What is the source of the problem?»
    L3 TYPE        (колонка 5)  «What is the problem?»
    L4 SUBTYPE     (колонка 6)  «What is the cause?»
    L5 RESOLUTION  (колонка 8)  «How was it resolved?»

Прочие колонки: 0 date, 2 пусто, 3 equipment, 7 свободное описание
(слишком свободный текст — в дерево не идёт), 9 status, 10 employee,
11–13 время.

ГЛАВНОЕ — ПЕРЕИСПОЛЬЗОВАНИЕ, А НЕ КОПИРОВАНИЕ.
Списки вариантов совпадают у разных родителей:
  * типы встречаются у нескольких объектов (行走异常Unable to drive — у 5);
  * причины встречаются под несколькими типами (程序逻辑BUG — под 7).
Поэтому узлы уровня 4 строятся ПО ТИПУ, а уровня 5 — ПО ПРИЧИНЕ, и один
такой узел указывается из многих родителей. Дополнительно узлы каждого
уровня дедуплицируются по сигнатуре списка вариантов: две одинаковые
сигнатуры дают один узел. Порядок вариантов — по частоте (убывание), как
в данных.

Запуск:
    python3 tools/build_tree_from_errors.py [TSV] [OUT.json]

Скрипт идемпотентен: на новой выгрузке достаточно перезапустить и
закоммитить новый tree.generated.json.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from equipment_intake.types import (  # noqa: E402
    DecisionTree,
    Node,
    NodeType,
    Option,
)


# ============================================================
# ИСТОЧНИК ДАННЫХ
# ============================================================

DEFAULT_TSV = os.path.join(
    PROJECT_ROOT, "data", "sample_errors_2026-09-23_28.tsv"
)
DEFAULT_OUT = os.path.join(
    PROJECT_ROOT, "equipment_intake", "tree.generated.json"
)

# Индексы колонок (0-based). Проверены на реальной выгрузке (14 колонок).
COL_OBJECT = 1
COL_CATEGORY = 4
COL_TYPE = 5
COL_SUBTYPE = 6
COL_RESOLUTION = 8

# Подписи уровней в итоговом экране.
LABEL_OBJECT = "Object"
LABEL_CATEGORY = "Category"
LABEL_PROBLEM = "Problem"
LABEL_CAUSE = "Cause"
LABEL_RESOLUTION = "Resolution"

# Ключи уровней: стабильные имена полей в итоговой записи.
KEY_OBJECT = "object"
KEY_CATEGORY = "category"
KEY_PROBLEM_TYPE = "problem_type"
KEY_CAUSE = "cause"
KEY_RESOLUTION = "resolution"

TITLE_OBJECT = "What is shown in the photo?"
TITLE_CATEGORY = "What is the source of the problem?"
TITLE_PROBLEM_TYPE = "What is the problem?"
TITLE_CAUSE = "What is the cause?"
TITLE_RESOLUTION = "How was it resolved?"

ROOT_ID = "root"
FINAL_ID = "summary"


def read_rows(path: str) -> List[List[str]]:
    """Читает TSV в список строк-колонок. Пустые строки пропускаются."""
    rows: List[List[str]] = []

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n").rstrip("\r")

            if not line.strip():
                continue

            rows.append(line.split("\t"))

    return rows


# ============================================================
# ID: ASCII-слаги
# ============================================================

def slug(label: str) -> str:
    """
    Стабильный ASCII-слаг из метки.

    Китайские иероглифы слаг выкидывает, а латинская часть метки
    сохраняется: «行走异常Unable to drive» → «unable-to-drive». Так id
    остаётся читаемым и безопасным для callback_data (ASCII, без пробелов).
    """
    parts: List[str] = []

    for char in label.strip().lower():
        if char.isascii() and char.isalnum():
            parts.append(char)
        else:
            parts.append("-")

    result = "".join(parts)

    while "--" in result:
        result = result.replace("--", "-")

    return result.strip("-")


class IdPool:
    """Выдаёт уникальные id: при совпадении добавляет числовой суффикс."""

    def __init__(self) -> None:
        self._taken: set = set()

    def take(self, base: str) -> str:
        root = base or "node"
        candidate = root
        counter = 2

        while candidate in self._taken:
            candidate = f"{root}-{counter}"
            counter += 1

        self._taken.add(candidate)

        return candidate


def option_ids() -> set:
    """Новый набор занятых id вариантов (уникальность — в пределах узла)."""
    return set()


def take_option_id(label: str, taken: set) -> str:
    """Уникальный ASCII-id варианта внутри одного узла."""
    root = slug(label) or "option"
    candidate = root
    counter = 2

    while candidate in taken:
        candidate = f"{root}-{counter}"
        counter += 1

    taken.add(candidate)

    return candidate


# ============================================================
# ГРУППИРОВКА
# ============================================================

Counter = collections.Counter

# Сигнатура списка вариантов: (метка, частота, цель). Две сигнатуры
# совпадают — узлы можно слить в один. Цель входит в сигнатуру, чтобы
# переиспользование не склеило два узла с одинаковыми подписями, но
# разными продолжениями.
Signature = Tuple[Tuple[str, int, Optional[str]], ...]


def ordered(counter: Counter) -> List[Tuple[str, int]]:
    """Варианты в порядке убывания частоты (самый вероятный — первым)."""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def count_by(rows: Sequence[Sequence[str]], key: int, value: int) -> Dict[str, Counter]:
    """Считает частоты значений колонки value в разрезе колонки key."""
    result: Dict[str, Counter] = {}

    for row in rows:
        left = row[key].strip()
        right = row[value].strip()

        if left and right:
            result.setdefault(left, Counter())[right] += 1

    return result


def count_pairs(
    rows: Sequence[Sequence[str]], first: int, second: int, value: int
) -> Dict[Tuple[str, str], Counter]:
    """Частоты колонки value в разрезе пары (first, second)."""
    result: Dict[Tuple[str, str], Counter] = {}

    for row in rows:
        left = row[first].strip()
        middle = row[second].strip()
        right = row[value].strip()

        if left and middle and right:
            result.setdefault((left, middle), Counter())[right] += 1

    return result


def make_options(
    counter: Counter,
    targets: Dict[str, Optional[str]],
) -> Tuple[Option, ...]:
    """
    Собирает варианты по счётчику: по частоте, с целью из targets.

    В description кладётся число наблюдений («224 times») — сотрудник
    видит, что первый вариант действительно самый частый.
    """
    taken = option_ids()
    options: List[Option] = []

    for label, count in ordered(counter):
        options.append(Option(
            id=take_option_id(label, taken),
            label=label,                       # полная метка, как в данных
            value=label,                       # value == label (единообразно)
            next_node=targets.get(label),
            description=f"{count} times",
        ))

    return tuple(options)


def signature_of(node: Node) -> Signature:
    """Сигнатура узла: список (метка, частота, цель) в текущем порядке."""
    return tuple(
        (option.label, _count_of(option), option.next_node)
        for option in node.options
    )


def _count_of(option: Option) -> int:
    """Достаёт число наблюдений обратно из description («224 times»)."""
    text = (option.description or "").split(" ", 1)[0]

    try:
        return int(text)
    except ValueError:
        return 0


# ============================================================
# ПОСТРОЕНИЕ ДЕРЕВА
# ============================================================

def build_tree(rows: Sequence[Sequence[str]]) -> Tuple[DecisionTree, Dict[str, int]]:
    """
    Собирает дерево из строк экспорта.

    Возвращает (дерево, статистика). Статистика нужна отчёту генератора:
    сколько родителей на уровне и во сколько узлов они схлопнулись.
    """
    ids = IdPool()
    nodes: Dict[str, Node] = {}

    stats: Dict[str, int] = {}

    # --- Счётчики из данных ----------------------------------------------
    obj_cat = count_by(rows, COL_OBJECT, COL_CATEGORY)
    objcat_type = count_pairs(rows, COL_OBJECT, COL_CATEGORY, COL_TYPE)
    type_sub = count_by(rows, COL_TYPE, COL_SUBTYPE)
    sub_res = count_by(rows, COL_SUBTYPE, COL_RESOLUTION)

    # Сколько узлов было бы, если каждую ветку не переиспользовать: по
    # одному узлу на каждый РЕАЛЬНО наблюдаемый контекст уровня.
    naive_l2 = len(obj_cat)                       # (object)
    naive_l3 = len(objcat_type)                   # (object, category)
    naive_l4 = len({                              # (object, category, type)
        (row[COL_OBJECT].strip(), row[COL_CATEGORY].strip(), row[COL_TYPE].strip())
        for row in rows
    })
    naive_l5 = len({                              # (object, category, type, subtype)
        (row[COL_OBJECT].strip(), row[COL_CATEGORY].strip(),
         row[COL_TYPE].strip(), row[COL_SUBTYPE].strip())
        for row in rows
    })

    # --- L5: решения по ПРИЧИНЕ (subtype) ---------------------------------
    # Один узел на причину; сигнатурная дедупликация склеит причины с
    # одинаковым набором решений. На узел ссылаются все типы, где эта
    # причина встречается, — поэтому причины не дублируются.
    resolution_by_subtype: Dict[str, str] = {}
    resolution_by_sig: Dict[Signature, str] = {}
    l5_nodes = 0

    for subtype in sorted(sub_res, key=lambda s: (-sum(sub_res[s].values()), s)):
        counter = sub_res[subtype]
        options = make_options(counter, {label: None for label in counter})
        node = Node(
            id="",
            title=TITLE_RESOLUTION,
            type=NodeType.CHOICE,
            key=KEY_RESOLUTION,
            summary_label=LABEL_RESOLUTION,
            options=options,
            next_node=FINAL_ID,
        )
        sig = signature_of(node)
        node_id = resolution_by_sig.get(sig)

        if node_id is None:
            node_id = ids.take(f"resolution-{slug(subtype)}")
            node = Node(
                id=node_id,
                title=TITLE_RESOLUTION,
                type=NodeType.CHOICE,
                key=KEY_RESOLUTION,
                summary_label=LABEL_RESOLUTION,
                options=options,
                next_node=FINAL_ID,
            )
            nodes[node_id] = node
            resolution_by_sig[sig] = node_id
            l5_nodes += 1

        resolution_by_subtype[subtype] = node_id

    # --- L4: причины по ТИПУ ---------------------------------------------
    # Узел причины общий для всех объектов/категорий, где тип встречается:
    # типы переиспользуются между объектами.
    cause_by_type: Dict[str, str] = {}
    cause_by_sig: Dict[Signature, str] = {}
    l4_nodes = 0

    for problem_type in sorted(
        type_sub, key=lambda t: (-sum(type_sub[t].values()), t)
    ):
        counter = type_sub[problem_type]
        targets = {
            subtype: resolution_by_subtype.get(subtype)
            for subtype in counter
        }
        options = make_options(counter, targets)
        sig = signature_of(Node(
            id="", title="", options=options,
        ))
        node_id = cause_by_sig.get(sig)

        if node_id is None:
            node_id = ids.take(f"cause-{slug(problem_type)}")
            nodes[node_id] = Node(
                id=node_id,
                title=TITLE_CAUSE,
                type=NodeType.CHOICE,
                key=KEY_CAUSE,
                summary_label=LABEL_CAUSE,
                options=options,
            )
            cause_by_sig[sig] = node_id
            l4_nodes += 1

        cause_by_type[problem_type] = node_id

    # --- L3: типы по паре (объект, категория) ----------------------------
    type_by_context: Dict[Tuple[str, str], str] = {}
    type_by_sig: Dict[Signature, str] = {}
    l3_nodes = 0

    for context in sorted(
        objcat_type, key=lambda c: (-sum(objcat_type[c].values()), c)
    ):
        counter = objcat_type[context]
        targets = {
            problem_type: cause_by_type.get(problem_type)
            for problem_type in counter
        }
        options = make_options(counter, targets)
        sig = signature_of(Node(id="", title="", options=options))
        node_id = type_by_sig.get(sig)

        if node_id is None:
            node_id = ids.take(
                f"problem-{slug(context[0])}-{slug(context[1])}"
            )
            nodes[node_id] = Node(
                id=node_id,
                title=TITLE_PROBLEM_TYPE,
                type=NodeType.CHOICE,
                key=KEY_PROBLEM_TYPE,
                summary_label=LABEL_PROBLEM,
                options=options,
            )
            type_by_sig[sig] = node_id
            l3_nodes += 1

        type_by_context[context] = node_id

    # --- L2: категории по объекту ----------------------------------------
    category_by_object: Dict[str, str] = {}
    category_by_sig: Dict[Signature, str] = {}
    l2_nodes = 0

    for obj in sorted(obj_cat, key=lambda o: (-sum(obj_cat[o].values()), o)):
        counter = obj_cat[obj]
        targets = {
            category: type_by_context.get((obj, category))
            for category in counter
        }
        options = make_options(counter, targets)
        sig = signature_of(Node(id="", title="", options=options))
        node_id = category_by_sig.get(sig)

        if node_id is None:
            node_id = ids.take(f"category-{slug(obj)}")
            nodes[node_id] = Node(
                id=node_id,
                title=TITLE_CATEGORY,
                type=NodeType.CHOICE,
                key=KEY_CATEGORY,
                summary_label=LABEL_CATEGORY,
                options=options,
            )
            category_by_sig[sig] = node_id
            l2_nodes += 1

        category_by_object[obj] = node_id

    # --- L1: объекты ------------------------------------------------------
    object_counter = Counter()

    for row in rows:
        obj = row[COL_OBJECT].strip()

        if obj:
            object_counter[obj] += 1

    root_targets = {
        obj: category_by_object.get(obj) for obj in object_counter
    }
    nodes[ROOT_ID] = Node(
        id=ROOT_ID,
        title=TITLE_OBJECT,
        type=NodeType.CHOICE,
        key=KEY_OBJECT,
        summary_label=LABEL_OBJECT,
        options=make_options(object_counter, root_targets),
    )

    # --- Финал ------------------------------------------------------------
    nodes[FINAL_ID] = Node(
        id=FINAL_ID,
        title="Inspection result",
        type=NodeType.FINAL,
        summary_title="Inspection result",
        final_action="save_inspection",
    )

    stats.update({
        "l2_parents": naive_l2, "l2_nodes": l2_nodes,
        "l3_parents": naive_l3, "l3_nodes": l3_nodes,
        "l4_parents": naive_l4, "l4_nodes": l4_nodes,
        "l5_parents": naive_l5, "l5_nodes": l5_nodes,
        "naive_total": 1 + naive_l2 + naive_l3 + naive_l4 + naive_l5 + 1,
        "actual_total": len(nodes),
    })

    tree = DecisionTree(root_id=ROOT_ID, nodes=nodes)
    tree.normalize()
    tree.validate()

    return tree, stats


# ============================================================
# ПРОВЕРКА ПОКРЫТИЯ
# ============================================================

def walk_labels(tree: DecisionTree, labels: Sequence[str]) -> Optional[Node]:
    """
    Проходит дерево по меткам, как это делает сотрудник кнопками.

    None — если хотя бы одного варианта нет в узле (строка не покрыта).
    """
    node: Optional[Node] = tree.node(tree.root_id)

    for label in labels:
        if node is None:
            return None

        option = next(
            (item for item in node.options if item.label == label), None
        )

        if option is None:
            return None

        node = tree.node(option.next_node or node.next_node)

    return node


def coverage(
    tree: DecisionTree, rows: Sequence[Sequence[str]]
) -> Tuple[int, List[str]]:
    """Сколько строк проходимо по дереву целиком (до FINAL) и какие нет."""
    covered = 0
    failures: List[str] = []

    for line_no, row in enumerate(rows, start=1):
        labels = [
            row[COL_OBJECT].strip(),
            row[COL_CATEGORY].strip(),
            row[COL_TYPE].strip(),
            row[COL_SUBTYPE].strip(),
            row[COL_RESOLUTION].strip(),
        ]

        node = walk_labels(tree, labels)

        if node is not None and node.type == NodeType.FINAL:
            covered += 1
        else:
            failures.append(f"строка {line_no}: " + " → ".join(labels))

    return covered, failures


def top_paths(
    rows: Sequence[Sequence[str]], limit: int = 5
) -> List[Tuple[int, Tuple[str, ...]]]:
    """Самые частые полные пути (object → … → resolution)."""
    counter: Counter = Counter()

    for row in rows:
        counter[(
            row[COL_OBJECT].strip(),
            row[COL_CATEGORY].strip(),
            row[COL_TYPE].strip(),
            row[COL_SUBTYPE].strip(),
            row[COL_RESOLUTION].strip(),
        )] += 1

    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))

    return [(count, path) for path, count in ranked][:limit]


# ============================================================
# ЗАПИСЬ И ОТЧЁТ
# ============================================================

def write_tree(tree: DecisionTree, path: str) -> None:
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tree.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def longest_label(tree: DecisionTree) -> Tuple[str, int, int]:
    """Самая длинная метка: (текст, символов, байт UTF-8)."""
    longest = ""

    for node in tree.nodes.values():
        for option in node.options:
            if len(option.label) > len(longest):
                longest = option.label

    return longest, len(longest), len(longest.encode("utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    tsv_path = args[0] if args else DEFAULT_TSV
    out_path = args[1] if len(args) > 1 else DEFAULT_OUT

    rows = read_rows(tsv_path)
    tree, stats = build_tree(rows)

    write_tree(tree, out_path)

    covered, failures = coverage(tree, rows)
    option_count = sum(len(node.options) for node in tree.nodes.values())
    label, label_chars, label_bytes = longest_label(tree)

    actual = stats["actual_total"]
    naive = stats["naive_total"]

    print("=" * 64)
    print(f"Источник:        {tsv_path}")
    print(f"Записей:         {len(rows)}")
    print(f"Записано в:      {out_path}")
    print("-" * 64)
    print(f"Узлов:           {actual} "
          f"(без переиспользования было бы {naive}; "
          f"сэкономлено {naive - actual})")
    print(f"Вариантов:       {option_count}")
    print("Глубина:         5 уровней + final")
    print("-" * 64)
    print("Уровень          родителей   узлов   (дедуп по сигнатуре)")
    print(f"  L1 object      {1:>9} {1:>7}")
    print(f"  L2 category    {stats['l2_parents']:>9} {stats['l2_nodes']:>7}")
    print(f"  L3 problem     {stats['l3_parents']:>9} {stats['l3_nodes']:>7}")
    print(f"  L4 cause       {stats['l4_parents']:>9} {stats['l4_nodes']:>7}")
    print(f"  L5 resolution  {stats['l5_parents']:>9} {stats['l5_nodes']:>7}")
    print(f"  final summary  {1:>9} {1:>7}")
    print(f"  ИТОГО          {naive:>9} {actual:>7}")
    print("-" * 64)
    print(f"Покрытие 911 строк: {covered}/{len(rows)} "
          f"({100.0 * covered / max(1, len(rows)):.1f}%)")
    print(f"Самая длинная метка: {label_chars} симв. / {label_bytes} байт — "
          f"{label!r}")
    print("-" * 64)
    print("Топ-5 полных путей:")

    for count, path in top_paths(rows):
        print(f"  {count:>4}× " + " → ".join(path))

    if failures:
        print("-" * 64)
        print(f"НЕ ПОКРЫТО ({len(failures)}):")

        for failure in failures[:20]:
            print("  -", failure)

    print("=" * 64)

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
