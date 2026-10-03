"""
Модель дерева решений (Decision Tree).

Модель используется пакетом equipment_intake и ботом telegram_bot.py.

Здесь нет ни Telegram, ни HTTP, ни базы — только данные и их проверка.
Благодаря этому дерево можно позже грузить из backend/БД, не меняя UI:
достаточно вернуть такие же Node/Option/DecisionTree.

Термины:
  * Node   — вопрос (шаг) с типом и вариантами;
  * Option — вариант ответа; может вести на следующий узел (next_node);
  * Step   — уже принятое пользователем решение (элемент пути).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class NodeType(str, Enum):
    """Тип вопроса. От него зависит и ввод, и клавиатура."""

    CHOICE = "choice"      # один вариант из списка кнопок
    MULTI = "multi"        # несколько вариантов (переключатели + Done)
    YESNO = "yesno"        # да / нет
    INPUT = "input"        # произвольный текст
    NUMBER = "number"      # число (с проверкой диапазона)
    FINAL = "final"        # конечный узел: показываем summary


# Типы, где ответ вводится текстом, а не кнопкой.
TEXT_NODE_TYPES = (NodeType.INPUT, NodeType.NUMBER)


@dataclass(frozen=True)
class Option:
    """
    Вариант ответа на шаге.

    next_node — id следующего узла. Если None, ветка заканчивается
    (либо продолжается next_node самого узла-родителя).
    value — что попадёт в ответ; по умолчанию label.
    """

    id: str
    label: str
    next_node: Optional[str] = None
    description: str = ""
    icon: str = ""
    status: str = ""          # цветовой/статусный маркер (цвет, "warning"…)
    value: Any = None
    # Показывать вариант только когда в пути уже есть одно из этих
    # значений (по node.key). Пример: «Left Horn» — только для A42T Hook.
    # Точность важнее полноты: новичок не должен видеть лишнего.
    only_for: Tuple[str, ...] = ()

    def answer_value(self) -> Any:
        return self.label if self.value is None else self.value


@dataclass(frozen=True)
class Node:
    """
    Узел дерева — один вопрос.

    key — имя ответа в итоговом словаре (динамическое, не «step1»).
    options — варианты для CHOICE/MULTI/YESNO.
    next_node — куда идти после текстового ответа (INPUT/NUMBER) или после
    выбора, если у самого варианта next_node не задан.
    """

    id: str
    title: str
    type: NodeType = NodeType.CHOICE
    key: str = ""
    description: str = ""
    options: Tuple[Option, ...] = ()
    next_node: Optional[str] = None
    placeholder: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    final_action: str = ""      # для FINAL: что сделать на Confirm
    summary_title: str = ""     # для FINAL: заголовок итогового экрана
    summary_label: str = ""     # наглядное имя поля в итоговом экране
    # Черновая ветка: узла/вариантов ещё нет, но место под них заведено.
    # Нужна, чтобы дерево можно было пустить в работу СРАЗУ и наполнять
    # по частям, не переписывая структуру. Такой узел показывается как
    # «пока не заполнено» и позволяет отправить ошибку текстом.
    is_stub: bool = False
    stub_hint: str = ""         # что здесь должен описать сотрудник

    def option(self, option_id: str) -> Optional[Option]:
        for item in self.options:
            if item.id == option_id:
                return item
        return None

    def answer_key(self) -> str:
        """Имя ответа; если key не задан — используем id узла."""
        return self.key or self.id


@dataclass
class Step:
    """Одно принятое решение: выбранный вариант и куда он ведёт."""

    node_id: str
    key: str
    label: str
    value: Any = None
    option_id: Optional[str] = None
    next_node: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "option_id": self.option_id,
            "next_node": self.next_node,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        return cls(
            node_id=data["node_id"],
            key=data["key"],
            label=data["label"],
            value=data.get("value"),
            option_id=data.get("option_id"),
            next_node=data.get("next_node"),
        )


@dataclass
class DecisionTree:
    """
    Дерево целиком: root_id + словарь узлов.

    Словарь, а не вложенные объекты, — чтобы дерево легко сериализовалось
    в JSON и грузилось из БД, а ссылки между узлами были по id.
    """

    root_id: str
    nodes: Dict[str, Node] = field(default_factory=dict)

    def node(self, node_id: Optional[str]) -> Optional[Node]:
        if node_id is None:
            return None
        return self.nodes.get(node_id)

    def require(self, node_id: str) -> Node:
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Узел {node_id!r} не найден в дереве")
        return node

    # ------------------------------------------------------------------
    # Сериализация: тот же формат, что придёт из backend/БД.
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_id": self.root_id,
            "nodes": {
                node_id: {
                    "id": node.id,
                    "title": node.title,
                    "type": node.type.value,
                    "key": node.key,
                    "description": node.description,
                    "next_node": node.next_node,
                    "placeholder": node.placeholder,
                    "min_value": node.min_value,
                    "max_value": node.max_value,
                    "final_action": node.final_action,
                    "summary_title": node.summary_title,
                    "summary_label": node.summary_label,
                    "is_stub": node.is_stub,
                    "stub_hint": node.stub_hint,
                    "options": [
                        {
                            "id": option.id,
                            "label": option.label,
                            "next_node": option.next_node,
                            "description": option.description,
                            "icon": option.icon,
                            "status": option.status,
                            "value": option.value,
                            "only_for": list(option.only_for),
                        }
                        for option in node.options
                    ],
                }
                for node_id, node in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionTree":
        """Собирает дерево из JSON-структуры (например, ответа backend)."""
        nodes: Dict[str, Node] = {}

        for node_id, raw in (data.get("nodes") or {}).items():
            options = tuple(
                Option(
                    id=item["id"],
                    label=item["label"],
                    next_node=item.get("next_node"),
                    description=item.get("description", ""),
                    icon=item.get("icon", ""),
                    status=item.get("status", ""),
                    value=item.get("value"),
                    only_for=tuple(item.get("only_for") or ()),
                )
                for item in (raw.get("options") or ())
            )

            node = Node(
                id=raw.get("id", node_id),
                title=raw.get("title", ""),
                type=NodeType(raw.get("type", "choice")),
                key=raw.get("key", ""),
                description=raw.get("description", ""),
                options=options,
                next_node=raw.get("next_node"),
                placeholder=raw.get("placeholder", ""),
                min_value=raw.get("min_value"),
                max_value=raw.get("max_value"),
                final_action=raw.get("final_action", ""),
                summary_title=raw.get("summary_title", ""),
                summary_label=raw.get("summary_label", ""),
                is_stub=bool(raw.get("is_stub", False)),
                stub_hint=raw.get("stub_hint", ""),
            )
            nodes[node.id] = node

        tree = cls(root_id=data["root_id"], nodes=nodes)
        tree.normalize()
        tree.validate()
        return tree

    # ------------------------------------------------------------------
    # Нормализация и проверка целостности.
    # ------------------------------------------------------------------

    def normalize(self) -> None:
        """
        Дополняет узлы, которые можно описать кратко.

        Yes/No без явных вариантов получает стандартные Yes/No — в конфиге
        писать их каждый раз не нужно, а UI всё равно нужны две кнопки.
        """
        from dataclasses import replace

        for node_id, node in list(self.nodes.items()):
            if node.type == NodeType.YESNO and not node.options:
                self.nodes[node_id] = replace(
                    node,
                    options=(
                        Option(id="yes", label="Yes", value=True),
                        Option(id="no", label="No", value=False),
                    ),
                )

    def validate(self) -> None:
        if self.root_id not in self.nodes:
            raise ValueError(f"root_id {self.root_id!r} отсутствует в nodes")

        for node in self.nodes.values():
            if node.type in (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO):
                # Черновой узел без вариантов — это норма: место заведено,
                # наполняется позже. В работе он попросит описать текстом.
                if not node.options and not node.is_stub:
                    raise ValueError(
                        f"Узел {node.id!r} типа {node.type.value} без вариантов"
                    )

            if node.type == NodeType.FINAL:
                continue

            targets: List[Optional[str]] = [node.next_node]

            for option in node.options:
                targets.append(option.next_node)

            for target in targets:
                if target is not None and target not in self.nodes:
                    raise ValueError(
                        f"Узел {node.id!r} ссылается на несуществующий "
                        f"узел {target!r}"
                    )

            seen: set = set()

            for option in node.options:
                if option.id in seen:
                    raise ValueError(
                        f"Узел {node.id!r}: дублирующийся вариант {option.id!r}"
                    )
                seen.add(option.id)

        # Достижимость от корня: недостижимые узлы — почти всегда опечатка.
        reachable: set = set()
        stack = [self.root_id]

        while stack:
            current = stack.pop()

            if current in reachable:
                continue

            reachable.add(current)
            node = self.nodes.get(current)

            if node is None or node.type == NodeType.FINAL:
                continue

            if node.next_node:
                stack.append(node.next_node)

            for option in node.options:
                if option.next_node:
                    stack.append(option.next_node)

        unreachable = set(self.nodes) - reachable

        if unreachable:
            raise ValueError(
                "Недостижимые узлы дерева: " + ", ".join(sorted(unreachable))
            )


def choice_node(node_id: str, title: str, *options: Option, **kwargs) -> Node:
    """
    Удобный конструктор узла-выбора для конфига.

    Варианты можно передать позиционно (options=(...)) или ключом
    options=(...), чтобы конфиг читался единообразно.
    """
    explicit = kwargs.pop("options", ())

    return Node(
        id=node_id,
        title=title,
        type=NodeType.CHOICE,
        options=tuple(options) + tuple(explicit),
        **kwargs,
    )


def opt(
    option_id: str,
    label: str,
    next_node: Optional[str] = None,
    **kwargs,
) -> Option:
    """Удобный конструктор варианта для конфига."""
    only_for = kwargs.pop("only_for", ())

    if isinstance(only_for, str):
        only_for = (only_for,)

    return Option(
        id=option_id,
        label=label,
        next_node=next_node,
        only_for=tuple(only_for),
        **kwargs,
    )
