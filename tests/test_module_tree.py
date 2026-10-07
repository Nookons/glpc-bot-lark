"""Модули робота разделены по моделям, клавиатура — в две колонки.

**Владелец 07.10.2026:** «нужно как-то сделать модуля отдельно на роботов,
потому что например на K50H мы имеем только 3 модуля это lift, chassis and safety,
больше нету и нету смысла показывать все в таком случае. Нужно сделать кнопки в
два столбца на модулях, так будет лучше, так как модулей много».

**Что было.** Один узел `robot_module` с четырьмя вариантами показывал их **всем
моделям**: у K50H предлагали Rotation и Tray, которых у него нет. Кладовщик либо
выбирал чужой модуль, либо писал «не знаю» — оба варианта портят статистику.

**Почему отдельные узлы, а не `only_for`.** Механизм `only_for` в дереве есть и
работает, но валидатор запрещает **одинаковые id вариантов внутри узла**, а
`lifting` нужен и K50H, и A42T. Отдельные узлы снимают ограничение честно.
"""

from __future__ import annotations

from typing import Any

from equipment_intake.keyboards import node_keyboard
from equipment_intake.tree_config import DEFAULT_TREE


class _Session:
    """Минимальная сессия для сборки клавиатуры: дерево и текущий узел."""

    def __init__(self, node_id: str) -> None:
        self._node_id = node_id
        self.draft: set[str] = set()

    def current_node(self) -> Any:
        return DEFAULT_TREE.node(self._node_id)

    def visible_options(self, node: Any = None) -> list[Any]:
        return list((node or self.current_node()).options)


def _labels(node_id: str) -> list[str]:
    return [option.label for option in DEFAULT_TREE.node(node_id).options]


class TestModulesPerModel:
    def test_k50h_has_exactly_three_modules(self) -> None:
        """K50H: подъём, шасси и безопасность — по словам владельца.

        Rotation и Tray у этой модели отсутствуют, и предлагать их нельзя.
        """
        labels = _labels("robot_module_k50h")

        assert "Lifting" in labels
        assert "Chassis" in labels
        assert "Safety" in labels
        assert "Rotation" not in labels, "у K50H нет поворота — вариант лишний"
        assert "Tray" not in labels, "у K50H нет полки — вариант лишний"

    def test_a42t_has_four_modules(self) -> None:
        labels = _labels("robot_module_a42t")

        for expected in ("Lifting", "Rotation", "Tray", "Chassis"):
            assert expected in labels

    def test_both_models_offer_not_a_module(self) -> None:
        """«Not a module» есть у обеих моделей.

        Ошибка бывает не в модуле: сбой программы, грязный код пола, посторонний
        предмет. Без этого варианта человек выбирал чужой модуль наугад.
        """
        assert "Not a module" in _labels("robot_module_k50h")
        assert "Not a module" in _labels("robot_module_a42t")

    def test_model_leads_to_its_own_module_list(self) -> None:
        """Модель ведёт в **свой** список модулей, а не в общий."""
        node = DEFAULT_TREE.node("robot_type")
        targets = {option.id: option.next_node for option in node.options}

        assert targets["k50h"] == "robot_module_k50h"
        assert targets["a42t"] == "robot_module_a42t"
        assert targets["a42t_c2"] == "robot_module_a42t"


class TestKeyboardColumns:
    def test_four_or_more_options_use_two_columns(self) -> None:
        """С четырёх вариантов кнопки идут по две в ряд.

        Владелец: «кнопки в два столбца на модулях, так будет лучше, так как
        модулей много». Одна колонка растягивала экран — у robot_module пять
        вариантов, и на телефоне приходилось листать.
        """
        rows = node_keyboard(_Session("robot_module_a42t"))["inline_keyboard"]

        assert len(rows[0]) == 2, "первый ряд должен содержать две кнопки"
        assert all(len(row) <= 2 for row in rows), "в ряду не больше двух кнопок"

    def test_three_options_stay_single_column(self) -> None:
        """Три и меньше — одна колонка: такой список читается одним взглядом."""
        rows = node_keyboard(_Session("robot_type"))["inline_keyboard"]

        assert all(len(row) == 1 for row in rows)

    def test_two_columns_keep_every_option(self) -> None:
        """Раскладка меняет вид, но **не теряет** варианты."""
        rows = node_keyboard(_Session("robot_module_a42t"))["inline_keyboard"]
        # Последняя строка — , она есть на любом шаге и вариантом не
        # является. Считаю только строки вариантов.
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]
        shown = sum(len(row) for row in option_rows)

        assert shown == len(_labels("robot_module_a42t"))


class TestCauseQuestion:
    def test_cause_is_asked_after_the_description(self) -> None:
        """Причина спрашивается **после** описания проблемы.

        Владелец: «добавить причину ошибки как ещё один вопрос». Описание
        отвечает «что не так», причина — «почему»; это разные вещи.
        """
        nodes = DEFAULT_TREE.to_dict()["nodes"]
        description = nodes["ask_description"]

        assert description["next_node"] == "ask_cause", (
            "после описания должен идти вопрос о причине"
        )

    def test_cause_has_options_and_leads_to_summary(self) -> None:
        options = DEFAULT_TREE.node("ask_cause").options

        assert len(options) >= 4, "список причин пуст или слишком короткий"
        assert all(option.next_node == "summary" for option in options)
        # «Other» обязателен: закрытый список без него заставляет выбирать
        # неточное и портит статистику.
        assert any(option.id == "other" for option in options)

    def test_cause_is_recorded_in_answers(self) -> None:
        """Ответ о причине попадает в результат с ключом `cause`."""
        assert DEFAULT_TREE.node("ask_cause").answer_key() == "cause"
