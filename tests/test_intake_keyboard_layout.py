"""Сторож на раскладку кнопок выбора — требование владельца 12.10.2026.

Дословно: «Ошибки на модулях должны идти **в два ряда по 6 за раз** и должна
быть **кнопка далее на два ряда внизу** если их больше».

Расшифровка, которую и проверяет этот файл:

  * 6 вариантов  → **один** ряд из шести (ровно 6, не растягиваем);
  * 8 вариантов  → **два** ряда: 6 + 2;
  * 12 вариантов → два ряда по 6, кнопки **Next нет** (всё влезло);
  * 13 вариантов → два ряда по 6 плюс кнопка **Next** отдельным рядом внизу;
  * на любой странице видна **Cancel**;
  * листание **не сбрасывает** сессию: путь и текущий шаг остаются на месте.

Сторож сознательно **негативно проверен**: если раскладку сломать (свести к
одному столбцу), проверки на «6 в ряд» падают. Это и есть смысл файла — он
обязан ловить регресс, а не подтверждать текущее состояние.
"""

from __future__ import annotations

from typing import Any, Dict, List

from equipment_intake.engine import Session
from equipment_intake.keyboards import (
    CB_CANCEL,
    CB_PAGE,
    OPTIONS_PER_PAGE,
    OPTIONS_PER_ROW,
    node_keyboard,
    option_page,
    set_option_page,
    total_option_pages,
)
from equipment_intake.types import DecisionTree, Node, NodeType, Option


def _tree(count: int) -> DecisionTree:
    """Дерево с одним узлом-выбором на `count` вариантов."""
    options = tuple(
        Option(id=f"o{index}", label=f"Option {index + 1}")
        for index in range(count)
    )

    node = Node(
        id="root",
        title="Which option?",
        type=NodeType.CHOICE,
        options=options,
        next_node="finish",
    )
    finish = Node(id="finish", title="Done", type=NodeType.FINAL)

    return DecisionTree(root_id="root", nodes={"root": node, "finish": finish})


def _session(count: int) -> Session:
    return Session(tree=_tree(count))


def _rows(session: Session) -> List[List[Dict[str, str]]]:
    return node_keyboard(session)["inline_keyboard"]


def _option_rows(rows: List[List[Dict[str, str]]]) -> List[List[Dict[str, str]]]:
    """Ряды вариантов — без служебных рядов (Cancel, Next/Back, Done)."""
    service = ("Cancel", "Next", "Back", "Done")

    return [
        row for row in rows
        if not any(any(word in button["text"] for word in service) for button in row)
    ]


def _texts(rows: List[List[Dict[str, str]]]) -> List[str]:
    return [button["text"] for row in rows for button in row]


class TestGridTwoRowsOfSix:
    def test_six_options_one_row_of_six(self) -> None:
        """6 вариантов — **ровно один** ряд из шести кнопок."""
        session = _session(6)
        option_rows = _option_rows(_rows(session))

        assert len(option_rows) == 1, "шесть вариантов обязаны уместиться в один ряд"
        assert len(option_rows[0]) == OPTIONS_PER_ROW == 6

    def test_eight_options_two_rows_six_and_two(self) -> None:
        """8 вариантов — два ряда: **6 + 2**, как в требовании."""
        session = _session(8)
        option_rows = _option_rows(_rows(session))

        assert len(option_rows) == 2
        assert [len(row) for row in option_rows] == [6, 2]

    def test_twelve_options_two_rows_of_six(self) -> None:
        """12 вариантов — два полных ряда по шесть, страница целиком."""
        session = _session(12)
        option_rows = _option_rows(_rows(session))

        assert [len(row) for row in option_rows] == [6, 6]

    def test_rows_never_exceed_six(self) -> None:
        """Ни в одном ряду не больше шести кнопок — это и есть «по 6»."""
        for count in range(1, 14):
            rows = _rows(_session(count))

            assert all(len(row) <= OPTIONS_PER_ROW for row in rows), count


class TestNextButtonOnOverflow:
    def test_thirteen_options_show_next(self) -> None:
        """13 вариантов — есть кнопка **Next** отдельным рядом внизу."""
        session = _session(13)
        rows = _rows(session)

        next_buttons = [
            button for row in rows for button in row
            if "Next" in button["text"]
        ]

        assert len(next_buttons) == 1, "13 вариантов обязаны дать кнопку далее"
        assert next_buttons[0]["callback_data"] == f"dt:{CB_PAGE}:1"
        # «далее» — **внизу**: ниже ряды вариантов, а сама кнопка стоит
        # отдельным рядом под сеткой (после неё идёт только Cancel).
        next_index = next(
            index for index, row in enumerate(rows) if next_buttons[0] in row
        )
        option_rows_below = _option_rows(rows[next_index + 1:])

        assert not option_rows_below, "под «далее» не должно быть рядов вариантов"

    def test_twelve_options_have_no_next(self) -> None:
        """12 вариантов — «далее» **нет**: всё влезло на одну страницу."""
        rows = _rows(_session(12))

        assert not any("Next" in text for text in _texts(rows))

    def test_second_page_shows_the_rest_and_back(self) -> None:
        """Вторая страница догружает остаток и даёт вернуться («назад»)."""
        session = _session(13)
        set_option_page(session, 1)
        rows = _rows(session)

        labels = _texts(rows)
        option_rows = _option_rows(rows)

        assert any("Back" in text for text in labels), "со второй страницы нужен возврат"
        assert not any("Next" in text for text in labels), "страница последняя"
        assert sum(len(row) for row in option_rows) == 1

    def test_pager_never_mixes_with_options(self) -> None:
        """Next/Back идут **отдельным рядом**, а не в ряду вариантов."""
        session = _session(13)

        for page in (0, 1):
            set_option_page(session, page)

            for row in _rows(session):
                texts = [button["text"] for button in row]

                assert not (
                    any("Next" in text or "Back" in text for text in texts)
                    and len(row) > 1
                ), (page, texts)


class TestCancelAlwaysVisible:
    def test_cancel_on_every_page(self) -> None:
        """Cancel обязана быть видна на **любой** странице."""
        session = _session(25)

        for page in range(total_option_pages(25)):
            set_option_page(session, page)
            codes = [
                button["callback_data"]
                for row in _rows(session)
                for button in row
            ]

            assert f"dt:{CB_CANCEL}" in codes, f"Cancel потерялась на странице {page}"


class TestPagingKeepsTheSession:
    def test_paging_does_not_touch_path_or_step(self) -> None:
        """Листание меняет только страницу: путь, шаг и ответы не трогаются."""
        session = _session(13)

        node = session.current_node()
        assert node is not None

        before_path = list(session.path)
        before_depth = session.depth
        before_current = session.current_id
        before_completed = session.is_completed

        set_option_page(session, 1)

        assert session.path == before_path, "листание потеряло выбранный путь"
        assert session.depth == before_depth
        assert session.current_id == before_current
        assert session.is_completed == before_completed
        assert option_page(session, node) == 1

    def test_paging_is_per_node(self) -> None:
        """Страница помнится **для узла**: у другого узла список с нуля."""
        session = _session(13)
        node = session.current_node()
        assert node is not None

        set_option_page(session, 1)

        assert session.data["option_pages"][node.id] == 1

    def test_page_is_clamped_to_available(self) -> None:
        """Страница за пределами списка не показывает пустой экран."""
        session = _session(13)
        node = session.current_node()
        assert node is not None

        set_option_page(session, 99)

        assert option_page(session, node) == total_option_pages(13) - 1

    def test_flow_paging_callback_redraws_the_same_session(self) -> None:
        """Колбэк `dt:p:1` из флоу листает **ту же** сессию, а не новую.

        Это сторож на главный риск: перелистывание не должно сбрасывать
        диалог. Проверяется через настоящий `flow.handle_callback` с
        подменённой отрисовкой.
        """
        from unittest import mock

        from equipment_intake import flow as intake_flow

        session = _session(13)
        session.data["menu_message_id"] = 5
        session.data["user_id"] = 1

        intake_flow.reset_state()
        intake_flow.put_session(-100, 1, session)

        captured: Dict[str, Any] = {}

        def _fake_render(current, chat_id, message_id, edit=True):
            captured["session"] = current
            captured["rows"] = current and node_keyboard(current)["inline_keyboard"]

        try:
            with mock.patch.object(intake_flow, "render", _fake_render), \
                 mock.patch("telegram_api.answer_callback_query"):
                handled = intake_flow.handle_callback(
                    -100,
                    {"id": 1},
                    ["dt", CB_PAGE, "1"],
                    5,
                    None,
                )

            assert handled is True
            assert captured["session"] is session, "создана новая сессия вместо листания"
            assert intake_flow.get_session(-100, 1) is session
            assert session.depth == 0
            assert session.current_id == "root"
            # На второй странице виден остаток и возврат.
            texts = [
                button["text"]
                for row in captured["rows"]
                for button in row
            ]
            assert any("Back" in text for text in texts)
        finally:
            intake_flow.reset_state()


class TestPageArithmetic:
    def test_options_per_page_is_twelve(self) -> None:
        """Страница = 2 ряда × 6 кнопок = 12 вариантов."""
        assert OPTIONS_PER_PAGE == 12

    def test_total_pages(self) -> None:
        assert total_option_pages(1) == 1
        assert total_option_pages(12) == 1
        assert total_option_pages(13) == 2
        assert total_option_pages(25) == 3
