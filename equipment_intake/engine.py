"""
Движок дерева решений.

Чистая логика без Telegram и без сети — её можно тестировать отдельно.
Здесь живут:
  * переходы по дереву (вперёд, Back, прыжок по breadcrumb, Restart);
  * сохранение выбранного пути и ответов (динамическая структура, без
    зашитых step1/step2/...);
  * сброс дочерних выборов при смене родительского;
  * сборка breadcrumb и финального summary.

Правило сброса ветки держится на одном инварианте: путь — это стек, и
любой ответ пишется в позицию self.depth, обрезая всё, что было глубже.
Поэтому изменить родительский выбор и сохранить устаревших детей нельзя
по построению.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .types import (
    DecisionTree,
    Node,
    NodeType,
    Option,
    Step,
    TEXT_NODE_TYPES,
)


class EngineError(Exception):
    """Некорректное действие (неизвестный вариант, пустой ввод и т.п.)."""


@dataclass
class Breadcrumb:
    """Элемент пути в навигации. index — сколько шагов останется после клика."""

    index: int
    label: str
    active: bool = False
    is_root: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "active": self.active,
            "is_root": self.is_root,
        }


@dataclass
class Session:
    """
    Состояние одной сессии выбора в дереве.

    Хранит ровно то, что просит задача:
      current node, selected path, uploaded image, answers, is_completed.
    """

    tree: DecisionTree
    current_id: Optional[str] = None
    path: List[Step] = field(default_factory=list)
    depth: int = 0
    draft: List[str] = field(default_factory=list)
    is_completed: bool = False
    final_title: str = "Inspection result"
    # Служебные данные флоу (путь к фото, подпись, топик и т.п.).
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.current_id is None and not self.path:
            self.current_id = self.tree.root_id

    # ------------------------------------------------------------------
    # Текущее состояние
    # ------------------------------------------------------------------

    def current_node(self) -> Optional[Node]:
        if self.is_completed and self.current_id is None:
            return None

        return self.tree.node(self.current_id)

    def is_text_step(self) -> bool:
        """Шаг принимает текст: Input/Number или черновая ветка."""
        node = self.current_node()

        if node is None:
            return False

        if node.is_stub and not node.options:
            return True

        return node.type in TEXT_NODE_TYPES

    def is_multi_step(self) -> bool:
        node = self.current_node()
        return bool(node and node.type == NodeType.MULTI)

    def is_stub_step(self) -> bool:
        """Текущий шаг — черновая ветка (варианты ещё не заведены)."""
        node = self.current_node()
        return bool(node and node.is_stub and not node.options)

    def visible_options(self, node: Optional[Node] = None) -> List[Option]:
        """
        Варианты, доступные при текущем пути.

        Учитывает only_for: вариант показывается, только если в пути уже
        есть одно из его значений. Так «Left Horn» не появится у модели,
        у которой его нет — новичок не видит лишнего и не ошибается.
        """
        target = node if node is not None else self.current_node()

        if target is None:
            return []

        if not target.options:
            return []

        chosen = set()
        for step in self.path:
            chosen.add(str(step.value))

        options: List[Option] = []

        for option in target.options:
            if not option.only_for:
                options.append(option)
                continue

            if any(str(value) in chosen for value in option.only_for):
                options.append(option)

        return options

    def uploaded_image(self) -> Optional[str]:
        return self.data.get("image")

    def answers(self) -> Dict[str, Any]:
        """
        Ответы, собранные по ключам узлов.

        Структура динамическая: ключи берутся из node.key, поэтому
        добавление уровня не ломает формат.
        """
        result: Dict[str, Any] = {}

        for step in self.path:
            result[step.key] = step.value

        return result

    def snapshot(self) -> Dict[str, Any]:
        """Сериализуемое состояние — для хранения/сохранения результата."""
        return {
            "current_node": self.current_id,
            "depth": self.depth,
            "uploaded_image": self.uploaded_image(),
            "answers": self.answers(),
            "path": [step.to_dict() for step in self.path],
            "is_completed": self.is_completed,
            "final_title": self.final_title,
        }

    # ------------------------------------------------------------------
    # Переходы
    # ------------------------------------------------------------------

    def _target_after(
        self,
        node: Node,
        option: Optional[Option] = None,
    ) -> Optional[str]:
        """Куда идти дальше: у варианта приоритет, затем у узла."""
        if option is not None and option.next_node:
            return option.next_node

        return node.next_node

    def _enter(self, target: Optional[str], default_title: str) -> None:
        """Переход к узлу target; None — ветка закончилась, показываем итог."""
        if target is None:
            self.current_id = None
            self.is_completed = True
            # Явный Final-узел задаёт заголовок; иначе — нейтральный.
            self.final_title = default_title or "Inspection result"
            return

        node = self.tree.node(target)

        if node is None:
            raise EngineError(f"Следующий узел {target!r} не найден")

        self.current_id = target
        self.is_completed = node.type == NodeType.FINAL
        self.final_title = node.summary_title or node.title

    def _push(
        self,
        node: Node,
        key: str,
        label: str,
        value: Any,
        option_id: Optional[str],
        next_node: Optional[str],
    ) -> None:
        """
        Записывает ответ в позицию depth, обрезая более глубокие выборы.

        Это и есть сброс дочерних значений при смене родительского.
        """
        self.path = self.path[: self.depth]
        self.path.append(
            Step(
                node_id=node.id,
                key=key,
                label=label,
                value=value,
                option_id=option_id,
                next_node=next_node,
            )
        )
        self.depth = len(self.path)
        self.draft = []

    def select(self, option_id: str) -> None:
        """Выбор варианта на текущем узле (Choice/YesNo/Multi-toggle)."""
        node = self.current_node()

        if node is None:
            raise EngineError("Дерево уже завершено")

        if node.type == NodeType.MULTI:
            self.toggle(option_id)
            return

        if node.type not in (NodeType.CHOICE, NodeType.YESNO):
            raise EngineError(
                f"Узел {node.id!r} не поддерживает выбор варианта"
            )

        option = node.option(option_id)

        if option is None:
            raise EngineError(f"Неизвестный вариант {option_id!r}")

        # Скрытый для этого пути вариант — как если бы его не было.
        if option not in self.visible_options(node):
            raise EngineError(
                "Этот вариант недоступен для выбранной модели"
            )

        target = self._target_after(node, option)
        self._push(
            node,
            node.answer_key(),
            option.label,
            option.answer_value(),
            option.id,
            target,
        )
        self._enter(target, node.summary_title)

    def toggle(self, option_id: str) -> None:
        """Отметить/снять вариант в Multi Choice."""
        node = self.current_node()

        if node is None or node.type != NodeType.MULTI:
            raise EngineError("Текущий шаг не поддерживает множественный выбор")

        if node.option(option_id) is None:
            raise EngineError(f"Неизвестный вариант {option_id!r}")

        if option_id in self.draft:
            self.draft.remove(option_id)
        else:
            self.draft.append(option_id)

    def done_multi(self) -> None:
        """Завершить Multi Choice: зафиксировать выбранное и идти дальше."""
        node = self.current_node()

        if node is None or node.type != NodeType.MULTI:
            raise EngineError("Текущий шаг не является множественным выбором")

        if not self.draft:
            raise EngineError("Не выбрано ни одного варианта")

        # Порядок значений — как в дереве, а не как нажимал сотрудник:
        # иначе одинаковый выбор давал бы разные записи.
        labels = [
            option.label
            for option in node.options
            if option.id in self.draft
        ]
        target = self._target_after(node)

        self._push(
            node,
            node.answer_key(),
            ", ".join(labels),
            list(labels),
            None,
            target,
        )
        self._enter(target, node.summary_title)

    def submit_text(self, raw: str) -> None:
        """
        Ответ текстом.

        Работает и для INPUT/NUMBER, и для ЧЕРНОВОЙ ветки: если варианты
        ещё не заведены, сотрудник просто описывает проблему словами. Так
        дерево можно пустить в работу сразу и наполнять по частям.
        """
        node = self.current_node()

        if node is None:
            raise EngineError("Текущий шаг не принимает текстовый ввод")

        text = (raw or "").strip()

        if not text:
            raise EngineError("Пустой ввод")

        if node.is_stub and not node.options:
            # Черновая ветка: текст и есть ответ, дальше — сразу итог.
            self._push(
                node,
                node.answer_key(),
                text,
                text,
                None,
                node.next_node,
            )
            self._enter(node.next_node, node.summary_title)
            return

        if node.type not in TEXT_NODE_TYPES:
            raise EngineError("Текущий шаг не принимает текстовый ввод")

        if node.type == NodeType.NUMBER:
            value = self._parse_number(node, text)
        else:
            value = text

        target = self._target_after(node)
        self._push(node, node.answer_key(), text, value, None, target)
        self._enter(target, node.summary_title)

    @staticmethod
    def _parse_number(node: Node, text: str) -> float:
        cleaned = text.replace(",", ".").strip()
        # Разрешаем «3 шт», «3.5» — берём первое число.
        #
        # ВАЖНО: `\d` в Python и `float()` принимают Unicode-цифры, поэтому
        # «٣٧٨» стало бы числом 378. Ограничиваем набор ASCII явно.
        import re

        match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", cleaned)

        if match is None:
            raise EngineError("Введите число")

        try:
            value = float(match.group(0))
        except ValueError:
            raise EngineError("Введите число")

        if node.min_value is not None and value < node.min_value:
            raise EngineError(f"Минимум: {_num(node.min_value)}")

        if node.max_value is not None and value > node.max_value:
            raise EngineError(f"Максимум: {_num(node.max_value)}")

        return int(value) if value.is_integer() else value

    def back(self) -> bool:
        """Шаг назад по дереву. False — уже в корне."""
        if self.is_completed and self.current_id is None and self.path:
            # С финального экрана назад — к последнему вопросу.
            last = self.path[-1]
            self.path = self.path[:-1]
            self.depth = len(self.path)
            self.current_id = last.node_id
            self.is_completed = False
            self.draft = []
            return True

        if not self.path:
            return False

        last = self.path[-1]
        self.path = self.path[:-1]
        self.depth = len(self.path)
        self.current_id = last.node_id
        self.is_completed = False
        self.draft = []
        return True

    def navigate_to(self, keep_steps: int) -> None:
        """
        Прыжок по breadcrumb.

        keep_steps — сколько выборов остаётся. Крошка с индексом i
        возвращает ровно к вопросу, на котором был сделан i-й выбор
        (индекс 0 — корень). Все более глубокие ответы отбрасываются.
        """
        keep = max(0, min(int(keep_steps), len(self.path)))

        if keep == 0:
            self.current_id = self.tree.root_id
        elif keep < len(self.path):
            self.current_id = self.path[keep].node_id
        # keep == len(path): этот шаг уже текущий — узел не меняем.

        self.path = self.path[:keep]
        self.depth = keep
        self.is_completed = False
        self.draft = []

    def restart(self) -> None:
        """Полный сброс пути, ответов и финального состояния."""
        self.current_id = self.tree.root_id
        self.path = []
        self.depth = 0
        self.draft = []
        self.is_completed = False
        self.final_title = "Inspection result"

    # ------------------------------------------------------------------
    # Навигация и представление
    # ------------------------------------------------------------------

    def breadcrumbs(self) -> List[Breadcrumb]:
        """
        Хлебные крошки: Photo → Robot → A42T E2 → …

        index — сколько шагов останется, если нажать; последний элемент
        выделен как текущий.
        """
        crumbs = [
            Breadcrumb(index=0, label="Photo", active=not self.path, is_root=True)
        ]

        for position, step in enumerate(self.path):
            crumbs.append(
                Breadcrumb(
                    index=position,
                    label=step.label,
                    active=position == len(self.path) - 1 and not self.is_completed,
                )
            )

        return crumbs

    def breadcrumb_text(self, limit: int = 6) -> str:
        """Текстовая строка пути для подписи. Длинный путь укорачиваем."""
        crumbs = [crumb.label for crumb in self.breadcrumbs() if crumb.label]

        if len(crumbs) > limit:
            crumbs = ["…"] + crumbs[-(limit - 1):]

        return " → ".join(crumbs)

    def breadcrumb_trail(self) -> List[Dict[str, Any]]:
        """Сериализованные крошки (для произвольного UI)."""
        return [crumb.to_dict() for crumb in self.breadcrumbs()]

    def summary(self) -> List[Dict[str, str]]:
        """
        Итог: пары «поле — значение» по всему выбранному пути.

        Имя поля берётся из node.summary_label (наглядное «Category»,
        «Component»…). Если оно не задано — используем заголовок вопроса,
        чтобы итог всё равно читался.
        """
        labels: Dict[str, str] = {}

        for step in self.path:
            node = self.tree.node(step.node_id)

            if node is None:
                continue

            labels[step.node_id] = node.summary_label or node.title

        rows: List[Dict[str, str]] = []

        for step in self.path:
            rows.append({
                "field": labels.get(step.node_id, step.key),
                "key": step.key,
                "value": _format_value(step.value),
            })

        return rows

    def summary_title(self) -> str:
        if self.current_id is None:
            return self.final_title

        node = self.current_node()

        if node is not None and node.type == NodeType.FINAL:
            return node.summary_title or node.title

        return self.final_title

    def final_action(self) -> str:
        node = self.current_node()

        if node is not None and node.type == NodeType.FINAL:
            return node.final_action

        return ""

    def progress(self) -> Tuple[int, int]:
        """(пройдено шагов, глубина от корня до текущего узла)."""
        return len(self.path), len(self.path) + (0 if self.is_completed else 1)

    def result(self) -> Dict[str, Any]:
        """
        Готовый к сохранению результат — универсальный и динамический.

        Его же можно отдать на backend: структура не зависит от числа
        уровней конкретного дерева.
        """
        return {
            "tree_root": self.tree.root_id,
            "answers": self.answers(),
            "path": [step.to_dict() for step in self.path],
            "image": self.uploaded_image(),
            "final_action": self.final_action(),
        }


def _format_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)

    return str(value)


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
