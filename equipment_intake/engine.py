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

    def answer_kind(self) -> str:
        """
        Вид номера на текущем шаге — по **ветке**, а не по узлу.

        Узел `ask_number` один на всё дерево, а формат вводимого значения
        зависит от выбранной ветки (робот / станция / рабочая станция / код
        места / код полки). Без этого подсказка была общей, и на ветке QR
        оператор вводил номер робота (16 таких строк на 12.10.2026).
        """
        from .tree_config import number_kind_for

        return number_kind_for(self.path, self.current_node())

    def text_example(self) -> str:
        """Пример ввода для текущего текстового шага (по ветке)."""
        from .tree_config import number_example

        node = self.current_node()

        if node is not None and node.type == NodeType.INPUT:
            return number_example(self.answer_kind())

        return ""

    def is_number_step(self) -> bool:
        """Вводится ли на этом шаге номер устройства, а не текст описания.

        Номер проверяется по формату ветки; `ask_description` — свободный
        текст, и проверять его нельзя.
        """
        return is_number_node(self.current_node())

    def text_error(self, raw: str) -> Optional[str]:
        """Понятная ошибка формата для текущего шага, или `None`.

        Проверка стоит **на вводе**, а не при записи: описание ошибки в поле
        номера (`'Driver component exception. 3528'`, `'Unable to drive'`) —
        это мусор, и человек должен переписать его на месте, не теряя сессию.
        """
        node = self.current_node()

        if not is_number_node(node):
            return None

        from .validation import validate_format

        return validate_format(self.answer_kind(), raw)

    def number_kind_for_step(self, step) -> str:
        """Вид номера для **уже пройденного** шага пути.

        Нужен на подтверждении: текущий узел — финальный, а проверить надо
        именно тот шаг, где вводился номер (у зарядки их два — станция и
        робот).
        """
        from .tree_config import number_kind_for

        for index, item in enumerate(self.path):
            if item is step:
                return number_kind_for(
                    self.path[: index + 1], self.tree.node(step.node_id)
                )

        return number_kind_for(self.path, None)

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
        """Куда идти дальше: у варианта приоритет, затем у узла.

        **Разные шаги для разных веток.** У узла номера следующая цель одна, но
        на зарядной ветке после станции надо спросить робота. `next_node_by_kind`
        задаёт такую цель по виду номера (`answer_kind`), и она **сильнее**
        `next_node` узла: иначе зарядная ветка молча шла бы сразу к описанию.
        """
        if option is not None and option.next_node:
            return option.next_node

        if node.next_node_by_kind:
            kind = self.answer_kind_for(node)

            for branch_kind, target in node.next_node_by_kind:
                if branch_kind == kind:
                    return target

        return node.next_node

    def answer_kind_for(self, node: Optional[Node]) -> str:
        """Вид вводимого номера для узла с учётом уже выбранного пути."""
        from .tree_config import number_kind_for

        return number_kind_for(self.path, node)

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
            raise EngineError(f"Next node {target!r} not found")

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
            raise EngineError("The tree is already complete")

        if node.type == NodeType.MULTI:
            self.toggle(option_id)
            return

        if node.type not in (NodeType.CHOICE, NodeType.YESNO):
            raise EngineError(
                f"Node {node.id!r} does not support option selection"
            )

        option = node.option(option_id)

        if option is None:
            raise EngineError(f"Unknown option {option_id!r}")

        # Скрытый для этого пути вариант — как если бы его не было.
        if option not in self.visible_options(node):
            raise EngineError(
                "This option is not available for the selected model"
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
            raise EngineError("The current step does not support multiple selection")

        if node.option(option_id) is None:
            raise EngineError(f"Unknown option {option_id!r}")

        if option_id in self.draft:
            self.draft.remove(option_id)
        else:
            self.draft.append(option_id)

    def done_multi(self) -> None:
        """Завершить Multi Choice: зафиксировать выбранное и идти дальше."""
        node = self.current_node()

        if node is None or node.type != NodeType.MULTI:
            raise EngineError("The current step is not a multiple selection")

        if not self.draft:
            raise EngineError("No option selected")

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
            raise EngineError("The current step does not accept text input")

        text = (raw or "").strip()

        if not text:
            raise EngineError("Empty input")

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
            raise EngineError("The current step does not accept text input")

        if node.type == NodeType.NUMBER:
            value = self._parse_number(node, text)
        else:
            value = text

        # **Формат номера проверяется ДО перехода.**
        #
        # Владелец 12.10.2026: «сейчас очень много некорректных данных идёт
        # через бота». В поле номера попадало описание ошибки (`'Unable to
        # drive'`, `'Driver component exception. 3528'`) — 103 невалидных
        # номера из 2340 в его выгрузке за октябрь. Это не «мягкий» случай:
        # сообщение об ошибке не является номером, и человек должен переписать
        # его на месте. Сессия и фото сохраняются — терять уже введённое нельзя.
        error = self.text_error(text)

        if error:
            raise EngineError(error)

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
            raise EngineError("Enter a number")

        try:
            value = float(match.group(0))
        except ValueError:
            raise EngineError("Enter a number")

        if node.min_value is not None and value < node.min_value:
            raise EngineError(f"Minimum: {_num(node.min_value)}")

        if node.max_value is not None and value > node.max_value:
            raise EngineError(f"Maximum: {_num(node.max_value)}")

        return int(value) if value.is_integer() else value

    def back(self) -> bool:
        """Шаг назад по дереву. False — уже в корне.

        **С финального экрана Back не уводит из ветки.** Раньше он возвращал к
        последнему вопросу, **снимая** последний шаг пути. На финальном экране
        (после `ask_cause`) это давало ловушку: кнопка «Edit selection» звала
        `back()`, путь терял ответ о причине, а `Confirm` после этого отказывал
        («Finish «What caused it?» first»). Теперь с финала возврат лишь
        показывает итог заново: путь остаётся полным, и подтвердить можно сразу.
        """
        if self.is_completed and self.current_id is None:
            # Путь не трогаем: сессия завершена, и `Confirm` обязан сработать.
            return bool(self.path)

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


#: Ключи узлов, где вводится **номер устройства**, а не произвольный текст.
#: `error_robot` — номер робота на зарядной ветке (`ask_charging_robot`).
NUMBER_KEYS = ("device_number", "error_robot")


def is_number_node(node: Optional[Node]) -> bool:
    """Вводится ли на узле номер устройства (а не описание проблемы)."""
    return (
        node is not None
        and node.type in TEXT_NODE_TYPES
        and (node.key in NUMBER_KEYS or bool(getattr(node, "answer_kind", "")))
    )
