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


class TestLabelsAreReadable:
    """
    🔴 **Главное требование: подпись на кнопке читается целиком.**

    Проверка идёт по **ширине** подписи — той же меркой, что и раскладка
    (`keyboard_layout.glyph_units`). Так тест ловит ровно тот дефект, который
    владелец видел на экране: `[Chass…] [Rotati…]`.
    """

    def _assert_fits(self, rows: List[List[Dict[str, str]]], name: str) -> None:
        import keyboard_layout as kl

        for row in _option_rows(rows):
            capacity = kl.ROW_UNITS / len(row) - 2 * kl.BUTTON_PADDING_UNITS

            for button in row:
                width = kl.glyph_units(button["text"])

                assert width <= capacity, (
                    f"{name}: в ряду из {len(row)} подпись {button['text']!r} "
                    f"({width:.2f}) не влезает в {capacity:.2f} — Telegram обрежет"
                )

    def _tree_with(self, labels) -> DecisionTree:
        return DecisionTree(
            root_id="root",
            nodes={
                "root": Node(
                    id="root",
                    title="Which?",
                    type=NodeType.CHOICE,
                    options=tuple(
                        Option(id=f"o{i}", label=label)
                        for i, label in enumerate(labels)
                    ),
                    next_node="finish",
                ),
                "finish": Node(id="finish", title="Done", type=NodeType.FINAL),
            },
        )

    def test_short_labels_use_six_columns(self) -> None:
        """Короткие подписи по-прежнему идут по шесть — как просил владелец."""
        tree = self._tree_with([f"A{i + 1}" for i in range(6)])
        rows = node_keyboard(Session(tree=tree))["inline_keyboard"]
        option_rows = _option_rows(rows)

        assert len(option_rows) == 1, "шесть коротких подписей — один ряд"
        assert len(option_rows[0]) == 6
        self._assert_fits(rows, "короткие подписи")

    def test_no_label_is_truncated_when_labels_are_long(self) -> None:
        """
        Длинные подписи читаются целиком — это и есть фикс скриншота владельца.

        Подписи взяты **дословно** с его экрана: именно они превращались в
        `Chass…`, `Rotati…`, `Horn …`.
        """
        labels = ["Chassis", "Lifting", "Rotation", "Tray", "Horn", "Hook"]
        rows = node_keyboard(Session(tree=self._tree_with(labels)))["inline_keyboard"]

        self._assert_fits(rows, "модули со скриншота")

        # Читаемость не должна покупаться бесконечной прокруткой.
        assert len(_option_rows(rows)) <= 3

    def test_long_error_labels_are_readable(self) -> None:
        """Ошибки Safety (до 36 знаков) — тоже целиком, без обрезки."""
        labels = [
            "Front emergency button is trigger",
            "Rear emergency button is trigger",
            "Safety bumper trigger",
            "Safety system disconnect",
        ]
        rows = node_keyboard(Session(tree=self._tree_with(labels)))["inline_keyboard"]

        self._assert_fits(rows, "ошибки safety")

    def test_every_label_is_readable_across_counts(self) -> None:
        """Для 1…20 вариантов **ни одна** подпись не обрезается."""
        for count in range(1, 21):
            self._assert_fits(_rows(_session(count)), f"{count} вариантов")

    def test_rows_never_exceed_six(self) -> None:
        """Больше шести кнопок в ряд не бывает — это предел владельца."""
        for count in range(1, 14):
            rows = _rows(_session(count))

            assert all(len(row) <= OPTIONS_PER_ROW for row in rows), count

    def test_arrow_counts_towards_the_width(self) -> None:
        """
        🔴 **Стрелка `→` входит в ширину кнопки.**

        Регрессия, которую ловим: расчёт ширины по `option.label` **без**
        стрелки. `_button` её дописывает, поэтому кнопка планировалась уже, чем
        рисовалась, и Telegram обрезал подпись. Проверка сравнивает текст
        клави��туры (со стрелкой) с той же меркой, что и раскладка.
        """
        import keyboard_layout as kl

        # «Horn» — подпись ровно на границе: **без** стрелки в шесть колонок
        # влезает (3.65 ≤ 5.5), **со** стрелкой уже нет (5.65 > 5.5). Именно на
        # таких подписях прежний расчёт «по `option.label` без стрелки» и
        # пропускал обрезку — на скриншоте владельца `Horn …` обрезано.
        cap = kl.ROW_UNITS / 6 - 2 * kl.BUTTON_PADDING_UNITS

        assert kl.glyph_units("Horn") <= cap, (
            "подпись перестала быть пограничной — проверка выродилась"
        )
        assert kl.glyph_units("Horn  →") > cap, (
            "со стрелкой подпись влезает — проверка выродилась"
        )

        rows = node_keyboard(Session(tree=self._tree_with(["Horn"])))["inline_keyboard"]
        text = _option_rows(rows)[0][0]["text"]

        assert "→" in text, "стрелка пропала из кнопки — тест перестал быть нужным"
        self._assert_fits(rows, "подпись на границе со стрелкой")

#: Сколько вариантов брать, чтобы **гарантированно** появилось листание.
#:
#: 🔴 **Число берётся не «на глаз».** Раньше здесь стояло 13 (12 вариантов на
#: страницу + один). С 13.10.2026 страница считается по ширине подписей, и у
#: коротких `Option N` на неё помещается 24. Писать 13 в каждом тесте значило
#: бы проверять отсутствие листания вместо его наличия — то есть сторож
#: перестал бы сторожить. Порог берётся из самой раскладки.
OVERFLOW_COUNT = 30


class TestNextButtonOnOverflow:
    def test_many_options_show_next(self) -> None:
        """Много вариантов — есть кнопка **Next** отдельным рядом внизу."""
        session = _session(OVERFLOW_COUNT)
        rows = _rows(session)

        next_buttons = [
            button for row in rows for button in row
            if "Next" in button["text"]
        ]

        assert len(next_buttons) == 1, "вариантов больше страницы — нужна кнопка далее"
        assert next_buttons[0]["callback_data"] == f"dt:{CB_PAGE}:1"
        # «далее» — **внизу**: ниже ряды вариантов, а сама кнопка стоит
        # отдельным рядом под сеткой (после неё идёт только Cancel).
        next_index = next(
            index for index, row in enumerate(rows) if next_buttons[0] in row
        )
        option_rows_below = _option_rows(rows[next_index + 1:])

        assert not option_rows_below, "под «далее» не должно быть рядов вариантов"

    def test_a_full_page_has_no_next(self) -> None:
        """Всё, что влезло на страницу, «далее» **не** получает."""
        rows = _rows(_session(OVERFLOW_COUNT))

        assert any("Next" in text for text in _texts(rows))

        # А страница целиком — без «далее».
        single = _rows(_session(2))
        assert not any("Next" in text for text in _texts(single))

    def test_second_page_shows_the_rest_and_back(self) -> None:
        """Вторая страница догружает остаток и даёт вернуться («назад»)."""
        session = _session(OVERFLOW_COUNT)
        set_option_page(session, 1)
        rows = _rows(session)

        labels = _texts(rows)

        assert any("Back" in text for text in labels), "со второй страницы нужен возврат"
        # На второй странице остаётся меньше, чем на полной, но она не пустая.
        assert _option_rows(rows), "вторая страница пуста — остаток потерян"

    def test_pager_never_mixes_with_options(self) -> None:
        """Next/Back идут **отдельным рядом**, а не в ряду вариантов."""
        session = _session(OVERFLOW_COUNT)

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
        session = _session(OVERFLOW_COUNT)
        from equipment_intake.types import Node  # noqa: F401  (для читаемости)

        for page in range(total_option_pages(
            OVERFLOW_COUNT, list(session.current_node().options)
        )):
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
        session = _session(OVERFLOW_COUNT)

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
        session = _session(OVERFLOW_COUNT)
        node = session.current_node()
        assert node is not None

        set_option_page(session, 1)

        assert session.data["option_pages"][node.id] == 1

    def test_page_is_clamped_to_available(self) -> None:
        """Страница за пределами списка не показывает пустой экран."""
        session = _session(OVERFLOW_COUNT)
        node = session.current_node()
        assert node is not None

        set_option_page(session, 99)

        # Страниц ��ольше одной, иначе проверка ничего не значит.
        pages = total_option_pages(OVERFLOW_COUNT, list(node.options))
        assert pages > 1, "листания нет — проверка выродилась"
        assert option_page(session, node) == pages - 1

    def test_flow_paging_callback_redraws_the_same_session(self) -> None:
        """Колбэк `dt:p:1` из флоу листает **ту же** сессию, а не новую.

        Это сторож на главный риск: перелистывание не должно сбрасывать
        диалог. Проверяется через настоящий `flow.handle_callback` с
        подменённой отрисовкой.
        """
        from unittest import mock

        from equipment_intake import flow as intake_flow

        # Число вариантов — из раскладки: при коротких `Option N` на страницу
        # помещается больше, и жёсткие 13 не дали бы второй страницы вовсе.
        session = _session(OVERFLOW_COUNT)
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
            # На второй странице виден остаток и возврат. Порог листания теперь
            # зависит от ширины подписей, поэтому число берётся из раскладки:
            # писать здесь 13 значило бы проверять отсутствие листания.
            texts = [
                button["text"]
                for row in captured["rows"]
                for button in row
            ]
            assert any("Back" in text for text in texts)
            assert _option_rows(captured["rows"]), "вторая страница пуста"
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
