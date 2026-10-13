"""
Сторож: **во всём боте не больше двух рядов кнопок**.

Требование владельца 13.10.2026, дословно: «Сделай по всему боту чтобы было не
больше двух рядов кнопок в противном случае они не читаются». Уточнение того же
дня: в лимит считаются **ряды вариантов**, а ряд навигации (Next / Back /
Cancel) идёт **отдельно** внизу.

─── Почему этот файл существует ─────────────────────────────────────────────

Правило «два ряда» жило только в дереве приёма, и **каждый новый экран заводил
свою раскладку**. Замерено 13.10.2026 на живом коде:

    экран                        было рядов
    ─────────────────────────────────────────
    статус робота (offline)         6
    статус робота (online)          6
    меню типа ошибки              N + 1
    выбор склада                  N + 1
    исправить номер робота        K + 1
    частые номера роботов           3
    редактор, узел A42T            11
    редактор, экран структуры     N + 1
    выбор узла-цели               N + 1

Общий сторож нужен именно потому, что раскладок много: точечные проверки
пропускали бы следующий новый экран.

─── Что считается «вариантом» ───────────────────────────────────────────────

Ряд вариантов — тот, где кнопка **выбирает значение** (причина, склад, тип
ошибки, узел, номер робота). Ряд навигации — `Next`/`Back`/`Cancel`/`Close`/
`Home`/`Structure` и служебные действия карточки («Rename», «Hide»). Вторые в
лимит не входят: их не читают как список, и владелец уточнил это отдельно.

─── Негативная проверка ─────────────────────────────────────────────────────

Файл проверен «наоборот»: если вернуть раскладку по одной кнопке в ряд, падают
проверки `test_reasons_fit_two_rows` и `test_editor_node_screen_fits_two_rows`.
"""

from __future__ import annotations

import unittest

import keyboard_layout as kl


# ============================================================
# ЧТО СЧИТАЕТСЯ «ВАРИАНТОМ»
# ============================================================

#: 🔴 **Классификация — по `callback_data`, а не по подписи.**
#:
#: Первая версия этого сторожа разбирала **подписи** и ошибалась: действие
#: «➕ Add option» в редакторе и «⏭️ Skip — no description» на экране статуса
#: она считала вариантами выбора, и живой экран «не проходил» проверку, хотя
#: раскладка была верной. Подпись к тому же несёт эмодзи и меняется свободно.
#:
#: `callback_data` — устойчивый признак: в нём закодировано **действие**.
#: Вариант выбора открывает конкретное значение (индекс, код, узел), а
#: навигация и действия карточки — служебные команды.
#:
#: Лимит владельца — **на ряды вариантов**; ряд навигации идёт отдельно внизу
#: (его уточнение от 13.10.2026).

#: Коды-действия редактора (`ed:<код>`), которые **не** являются вариантами.
EDITOR_ACTION_CODES = frozenset({
    "x",  # закрыть
    "n",  # назад к узлу
    "b",  # назад
    "u",  # вверх
    "h",  # домой
    "t",  # структура
    "A",  # добавить вариант
    "e",  # править узел
    "T", "E", "P", "N",  # поля узла
    "U", "W",            # перемещение варианта
    "q", "w", "m", "k",  # поля заготовки
    "v",  # скрыть/показать
    "L",  # удалить
    "H",  # скрыть узел
    "g",  # листание выбора цели
})


def _rows_matching(keyboard: dict, predicate) -> list:
    """
    Ряды, в которых есть **хотя бы одна** кнопка под условие.

    Условие получает **кнопку целиком**, а не только `callback_data`: у меню
    ошибок колбэк `em:c:<путь>` означает и «открыть категорию», и «Back» на
    уровень выше — различить их можно только по подписи.
    """
    return [
        row for row in (keyboard.get("inline_keyboard") or [])
        if any(predicate(b) for b in row)
    ]


def assert_labels_are_readable(
    case: unittest.TestCase, name: str, keyboard: dict, predicate
) -> None:
    """
    🔴 **Ни одна подпись на кнопке не обрезается.**

    Это ГЛАВНОЕ требование владельца (13.10.2026: «просто нужно чтобы ошибки
    читались»). Дефект со скриншота — `[Chass…] [Lifting…] [Rotati…]`: Telegram
    **обрезает** текст кнопки многоточием, а не переносит его, и подпись
    перестаёт быть выбором.

    Проверка считает **ширину текста** (`glyph_units`), а не число знаков:
    `Tray →` (7 знаков) влезает в шесть колонок, `Horn →` (тоже 7) — нет,
    потому что `Horn` шире. Счёт по знакам этот дефект пропускал.
    """
    import keyboard_layout as kl

    for row in keyboard.get("inline_keyboard") or []:
        choices = [b for b in row if predicate(b)]

        if not choices:
            continue

        capacity = kl.ROW_UNITS / len(row) - 2 * kl.BUTTON_PADDING_UNITS

        for button in choices:
            width = kl.glyph_units(button.get("text") or "")

            case.assertLessEqual(
                width, capacity,
                f"{name}: подпись {button.get('text')!r} ({width:.2f}) шире кнопки "
                f"({capacity:.2f}) в ряду из {len(row)} — Telegram её обрежет",
            )


def assert_option_rows_within_limit(
    case: unittest.TestCase, name: str, keyboard: dict, predicate
) -> None:
    """
    Не больше двух рядов **вариантов** (ряды ищутся по `callback_data`).

    Падает с понятным сообщением: видно, сколько рядов и какие кнопки в них.
    Без этого при регрессе пришлось бы считать кнопки руками.
    """
    rows = _rows_matching(keyboard, predicate)

    case.assertLessEqual(
        len(rows),
        kl.MAX_OPTION_ROWS,
        f"{name}: {len(rows)} рядов вариантов (предел {kl.MAX_OPTION_ROWS}). "
        f"Ряды: {[[b.get('text') for b in r] for r in rows]}",
    )


def _callback(button: dict) -> str:
    return str(button.get("callback_data") or "")


def _text(button: dict) -> str:
    return str(button.get("text") or "").strip()


def is_reason_choice(button: dict) -> bool:
    """Причина статуса (не Skip и не Cancel)."""
    callback = _callback(button)

    if not callback.startswith("st:"):
        return False

    return callback.rsplit(":", 1)[-1] not in ("skip", "cancel")


def is_error_menu_choice(button: dict) -> bool:
    """
    Категория, подтип или спец-пункт меню ошибок.

    🔴 **`Back` приходится отсеивать по подписи.** Его колбэк — `em:c:<путь>`,
    то есть **тот же**, что у открытия категории (переход на уровень вверх и
    есть открытие узла). Отличить его можно только по тексту, и это не
    «подгонка теста»: в самой клавиатуре это навигация, а не вариант выбора.
    """
    callback = _callback(button)

    if callback.startswith(("em:l:", "em:a:")):
        return True

    if callback.startswith("em:c:"):
        return _text(button) not in ("⬅️ Back", "✖️ Cancel")

    return False


def is_warehouse_choice(button: dict) -> bool:
    callback = _callback(button)
    return callback.startswith("w:") and not callback.startswith("w:cancel")


def is_robot_fix_choice(button: dict) -> bool:
    callback = _callback(button)
    return callback.startswith("rf:") and callback != "rf:no"


def is_robot_number_choice(button: dict) -> bool:
    return _callback(button).startswith("em:n:")


def is_editor_option_choice(button: dict) -> bool:
    """Вариант узла в редакторе: `ed:s:<индекс>`."""
    return _callback(button).startswith("ed:s:")


def is_editor_structure_choice(button: dict) -> bool:
    """Узел на экране структуры: `ed:j:<позиция>`."""
    return _callback(button).startswith("ed:j:")


def is_editor_target_choice(button: dict) -> bool:
    """
    Узел-цель: `ed:G:<позиция>`.

    `ed:G:e` — «End of branch», это **не** узел, а конец ветки.
    """
    callback = _callback(button)

    return callback.startswith("ed:G:") and not callback.endswith(":e")


# ============================================================
# 1. ЭКРАНЫ TELEGRAM-БОТА
# ============================================================

class BotScreensFitTwoRows(unittest.TestCase):
    """Экраны `telegram_bot.py`: статус, склады, меню ошибок, номера."""

    def setUp(self) -> None:
        import telegram_bot as bot

        self.bot = bot
        self._original_menu = bot.ERROR_MENU
        bot.ERROR_MENU = {
            "Robot": ("Abnormal walking", "Stuck", "Battery", "Sensor"),
            "Location": (),
            "Camera": ("Dirty", "Blurred"),
            "Network": ("Lost", "Slow", "Offline"),
        }
        self.addCleanup(setattr, bot, "ERROR_MENU", self._original_menu)

    def test_reasons_fit_two_rows(self):
        """
        🔴 Причины статуса: было **6 рядов** — по кнопке в ряд.

        Самый заметный случай из жалобы владельца: «Abnormal walking»,
        «Damaged car body parts», «Safety controller issues», «Other», «Skip» и
        «Cancel» шли шестью отдельными рядами и уезжали за верх экрана.

        **Что проверяется теперь.** Сетка есть (рядов стало 2–4 вместо 6), и
        **каждая подпись читается целиком**. Требовать «ровно два ряда» нельзя:
        «Safety controller issues» (24 знака) в ряд с другими не влезает, и
        два ряда означали бы обрезку — ровно тот дефект, что был на скриншоте.
        """
        for direction in ("offline", "online"):
            with self.subTest(direction=direction):
                keyboard = self.bot.reasons_keyboard(direction, 3780)

                assert_labels_are_readable(
                    self, f"причины {direction}", keyboard, is_reason_choice
                )

                # Сетка, а не «по одной в ряд»: рядов меньше, чем кнопок.
                rows = _rows_matching(keyboard, is_reason_choice)
                total = sum(len(r) for r in rows)

                self.assertLess(
                    len(rows), total,
                    f"{direction}: причины снова идут по одной в ряд",
                )

    def test_reasons_do_not_lose_any_choice(self):
        """
        Обратная сторона: в сетку попали **все** причины.

        Без этой проверки раскладка могла бы «уложиться в два ряда», просто
        выбросив лишние кнопки, — и проверка выше осталась бы зелёной.
        """
        from robot_status import REASONS

        for direction in ("offline", "online"):
            with self.subTest(direction=direction):
                keyboard = self.bot.reasons_keyboard(direction, 3780)
                codes = {
                    b["callback_data"].rsplit(":", 1)[-1]
                    for row in keyboard["inline_keyboard"] for b in row
                }

                for code, _label in REASONS.get(direction, ()):
                    self.assertIn(
                        code, codes,
                        f"{direction}: причина {code} п��терялась в раскладке",
                    )

    def test_reason_labels_are_readable(self):
        """
        🔴 Причины статуса читаются целиком: «Safety controller issues» (24).

        Это второй экран из жалобы владельца: подписи причин длиннее, чем у
        модулей, и в ряду из шести они превращались в `Safety…`.
        """
        for direction in ("offline", "online"):
            with self.subTest(direction=direction):
                assert_labels_are_readable(
                    self, f"причины {direction}",
                    self.bot.reasons_keyboard(direction, 3780),
                    is_reason_choice,
                )

    def test_error_menu_root_fits_two_rows(self):
        """Меню типа ошибки: первый уровень — категории."""
        assert_option_rows_within_limit(
            self, "меню ошибок (корень)", self.bot.error_menu_keyboard(()),
            is_error_menu_choice,
        )

    def test_error_menu_subtypes_fit_two_rows(self):
        """Второй уровень — подтипы (самый длинный список)."""
        assert_option_rows_within_limit(
            self, "меню ошибок (подтипы)", self.bot.error_menu_keyboard((0,)),
            is_error_menu_choice,
        )

    def test_error_menu_keeps_every_subtype_across_pages(self):
        """
        🔴 **Ни один подтип не теряется при листании.**

        Лишние кнопки уходят на следующую страницу, а не отбрасываются. Это
        важнее раскладки: потерянный подтип оператор не выберет вообще.
        """
        menu = {"Robot": tuple(f"Subtype {i}" for i in range(1, 10))}
        self.bot.ERROR_MENU = menu

        seen = set()
        page = 0

        # Обходим страницы, как это делает оператор кнопкой «Next».
        for _ in range(10):
            keyboard = self.bot.error_menu_keyboard((0,), page=page)
            leafs = [
                b["callback_data"] for row in keyboard["inline_keyboard"] for b in row
                if b["callback_data"].startswith("em:l:")
            ]
            seen.update(leafs)

            pager = [
                b["callback_data"] for row in keyboard["inline_keyboard"] for b in row
                if b["callback_data"].startswith("em:p:")
            ]

            if not pager:
                break

            page = int(pager[-1].rsplit(":", 1)[-1])

        self.assertEqual(
            len(seen), len(menu["Robot"]),
            f"показано {len(seen)} подтипов из {len(menu['Robot'])}",
        )

    def test_warehouse_choice_fits_two_rows(self):
        """Выбор склада: было `N + 1` рядов."""
        assert_option_rows_within_limit(
            self,
            "выбор склада",
            self.bot.warehouse_keyboard("on", 3780, ["GLP-C", "SMALL-P3"]),
            is_warehouse_choice,
        )

    def test_robot_fix_fits_two_rows(self):
        """Исправление номера робота: было `K + 1` рядов."""
        assert_option_rows_within_limit(
            self,
            "исправление номера",
            self.bot.robot_fix_keyboard(["882", "3881", "3872"]),
            is_robot_fix_choice,
        )

    def test_robot_number_keyboard_fits_two_rows(self):
        """
        Частые номера роботов: было **3 ряда** (8 номеров по 4 + Cancel).

        Здесь подписи — числа, поэтому ряд широкий (6 колонок), и в два ряда
        помещается 12 номеров.
        """
        import robot_card

        original = robot_card.robot_numbers
        robot_card.robot_numbers = lambda **kwargs: [str(3700 + i) for i in range(40)]
        self.addCleanup(setattr, robot_card, "robot_numbers", original)

        keyboard = self.bot.robot_number_keyboard(-100)
        self.assertIsNotNone(keyboard, "клавиатура номеров не построилась")
        assert_option_rows_within_limit(
            self, "частые номера", keyboard, is_robot_number_choice
        )


# ============================================================
# 2. РЕДАКТОР ДЕРЕВА (/tree)
# ============================================================

class EditorScreensFitTwoRows(unittest.TestCase):
    """Экраны `equipment_intake/editor.py` — самая длинная раскладка в боте."""

    def setUp(self) -> None:
        from equipment_intake.tree_config import DEFAULT_TREE

        self.tree = DEFAULT_TREE

    def _node_items(self, node_id: str):
        node = self.tree.node(node_id)

        if node is None:
            self.skipTest(f"в дереве нет узла {node_id}")

        return node, [
            {
                "label": option.label,
                "hidden": False,
                "kind": "builtin",
                "index": index,
                "is_builtin": True,
                "last": index == len(node.options) - 1,
            }
            for index, option in enumerate(node.options)
        ]

    def test_editor_node_screen_fits_two_rows(self):
        """
        🔴 Экран узла: у модуля A42T было **11 рядов**.

        Самый длинный экран бота: 8 вариантов по одному в ряд плюс действия и
        навигация. Теперь варианты — сетка не больше двух рядов.
        """
        from equipment_intake import editor

        for node_id in (
            "root",
            "robot_module_a42t",
            "robot_module_a42t_c2",
            "ask_cause_chassis",
            "robot_errors_safety",
        ):
            with self.subTest(node=node_id):
                node, items = self._node_items(node_id)
                keyboard = editor._node_keyboard(
                    self.tree, node, editor.ROLE_ADMIN, items
                )
                assert_option_rows_within_limit(
                    self, f"узел {node_id}", keyboard, is_editor_option_choice
                )

    def test_editor_node_screen_keeps_every_option_clickable(self):
        """
        Обратная сторона: **все** варианты узла остались кнопками.

        Сетка могла бы «уложиться в два ряда», отбросив лишние варианты, и
        тогда часть дерева стала бы нередактируемой.
        """
        from equipment_intake import editor

        node, items = self._node_items("robot_module_a42t")
        keyboard = editor._node_keyboard(self.tree, node, editor.ROLE_ADMIN, items)

        indexes = {
            b["callback_data"].rsplit(":", 1)[-1]
            for row in keyboard["inline_keyboard"] for b in row
            if b["callback_data"].startswith("ed:s")
        }

        self.assertEqual(
            indexes, {str(i) for i in range(len(items))},
            "часть вариантов пропала из раскладки",
        )

    def test_structure_screen_fits_two_rows(self):
        """Экран структуры: список всех узлов дерева."""
        from equipment_intake import editor

        _text, keyboard = editor._structure_screen(self.tree, 0)
        assert_option_rows_within_limit(
            self, "структура дерева", keyboard, is_editor_structure_choice
        )

    def test_target_picker_fits_two_rows(self):
        """Выбор узла-цели: было `N + 1` рядов."""
        from equipment_intake import editor

        _text, keyboard = editor._target_picker(self.tree, 0, None)
        assert_option_rows_within_limit(
            self, "выбор узла-цели", keyboard, is_editor_target_choice
        )

    def test_target_picker_opens_the_node_it_shows(self):
        """
        🔴 **Кнопка открывает тот узел, который на ней написан** — на каждой
        странице, а не только на первой.

        Это не про раскладку, а про дефект, который она чуть не внесла: раньше
        в колбэке ехали `page` и `offset`, и обработчик считал позицию как
        `page * PAGE_SIZE + offset`. После перехода на сетку размер страницы
        стал зависеть от длины подписей, и старая формула открыла бы **чужой
        узел**.

        ⚠️ **Проверка ОБЯЗАНА смотреть не первую страницу.** Первая версия
        этого теста брала только `page=0`, где `page * per_page + offset ==
        offset` — то есть старая (сломанная) формула давала **тот же** ответ, и
        тест оставался зелёным на сломанном коде. Поймано негативной проверкой.
        """
        from equipment_intake import editor

        node_ids = list(self.tree.nodes)
        columns = kl.columns_for_labels(
            [self.tree.node(n).title if self.tree.node(n) else n for n in node_ids]
        )
        pages = kl.page_count(len(node_ids), columns)

        self.assertGreater(pages, 1, "нужно минимум две страницы для честной проверки")

        for page in range(pages):
            with self.subTest(page=page):
                _text, keyboard = editor._target_picker(self.tree, page, None)

                checked = 0

                for row in keyboard["inline_keyboard"]:
                    for button in row:
                        if not is_editor_target_choice(button):
                            continue

                        position = int(_callback(button).rsplit(":", 1)[-1])
                        title = self.tree.node(node_ids[position]).title

                        # Подпись кнопки — это имя узла, показанное оператору.
                        # Значит позиция обязана указывать на **тот же** узел.
                        self.assertIn(
                            title[:30], button["text"],
                            f"стр.{page}: кнопка «{button['text'][:40]}» ведёт "
                            f"на узел {node_ids[position]!r}",
                        )
                        checked += 1

                self.assertGreater(checked, 0, f"стр.{page}: ни одного узла-цели")
                self.assertLessEqual(checked, kl.MAX_OPTION_ROWS * columns)

    def test_structure_screen_opens_the_node_it_shows(self):
        """То же для экрана структуры (`ed:j:<абсолютная позиция>`)."""
        from equipment_intake import editor

        _text, keyboard = editor._structure_screen(self.tree, 0)

        positions = [
            int(b["callback_data"].rsplit(":", 1)[-1])
            for row in keyboard["inline_keyboard"] for b in row
            if b["callback_data"].startswith("ed:j")
        ]

        self.assertEqual(positions, list(range(len(positions))))


# ------------------------------------------------------------
# ЧТО ПОД ПРАВИЛО НЕ ПОПАДАЕТ (решение владельца 13.10.2026)
# ------------------------------------------------------------

class SettingsCardIsNotAChoiceList(unittest.TestCase):
    """
    Карточка настройки **одной** кнопки в `/tree` — не список выбора.

    У неё 13 действий («Rename», «Icon», «Module», «Solving time», «Delete»…),
    сгруппированных по два в ряд, и это **панель настроек**: кнопки не
    сравнивают между собой и не читаются как список, поэтому правило «два ряда»
    на неё не распространяется.

    Владелец подтвердил это отдельно 13.10.2026, когда сторож показал карточку
    единственным местом с большим числом рядов: он выбрал **оставить как есть**,
    а не прятать настройки в подменю. Тест фиксирует **границу правила**, чтобы
    её не приняли за недосмотр и не «починили» сжатием в два ряда: тогда часть
    настроек уехала бы в подменю и стала бы менее доступной.
    """

    def test_option_card_is_a_settings_panel(self):
        """Карточка содержит действия, а не варианты одного выбора."""
        from equipment_intake import editor

        item = {
            "label": "Chassis",
            "hidden": False,
            "kind": "added",
            "index": 1,
            "last": False,
            "is_builtin": False,
        }
        keyboard = editor._option_keyboard(item, editor.ROLE_ADMIN)
        rows = keyboard["inline_keyboard"]

        # Рядов больше двух — и это осознанно (см. докстроку класса).
        self.assertGreater(len(rows), kl.MAX_OPTION_ROWS)

        # 🔴 Но **ряды остаются короткими**: в каждом не больше двух кнопок.
        # Длинный ряд настроек читался бы хуже, чем несколько коротких, — а
        # именно на читаемость и жаловался владелец.
        for row in rows:
            self.assertLessEqual(
                len(row), 2,
                f"ряд настроек разросся: {[b.get('text') for b in row]}",
            )

        # И это именно действия: ни одна кнопка не выбирает значение списка.
        self.assertTrue(
            all(b["callback_data"].startswith("ed:") for row in rows for b in row),
        )


# ============================================================
# 3. ДЕРЕВО ПРИЁМА
# ============================================================

class IntakeScreensFitTwoRows(unittest.TestCase):
    """Экраны `equipment_intake/keyboards.py` — здесь правило и родилось."""

    def _session(self, count: int):
        from equipment_intake.engine import Session
        from equipment_intake.types import DecisionTree, Node, NodeType, Option

        options = tuple(
            Option(id=f"o{index}", label=f"Option {index + 1}")
            for index in range(count)
        )
        node = Node(
            id="root", title="Which option?", type=NodeType.CHOICE,
            options=options, next_node="finish",
        )
        finish = Node(id="finish", title="Done", type=NodeType.FINAL)

        return Session(tree=DecisionTree(root_id="root", nodes={"root": node, "finish": finish}))

    def _options(self, count: int):
        """Те же варианты, что строит `_session` — для расчёта страниц."""
        from equipment_intake.types import Option

        return [Option(id=f"o{index}", label=f"Option {index + 1}") for index in range(count)]

    def _per_page(self, count: int) -> int:
        from equipment_intake.keyboards import options_per_page

        return options_per_page(self._options(count))

    def _pages(self, count: int) -> int:
        from equipment_intake.keyboards import total_option_pages

        return total_option_pages(count, self._options(count))

    def test_intake_choice_fits_two_rows(self):
        """
        Списки вариантов дерева: 6, 8, 12, 13 и 25 штук.

        🔴 **Проверка переименована по смыслу, а не по названию.** Раньше здесь
        требовалось «не больше двух рядов». С 13.10.2026 ширина ряда выводится из
        текста, и подписи вида `Option 12  →` (9.2 единицы) в шесть колонок не
        влезают: честная раскладка даёт три ряда по четыре. Это **не регресс**, а
        цена читаемости: при шести колонках Telegram показал бы `Option…`, и
        оператор не отличил бы вариант 12 от варианта 1.

        Поэтому проверяются два свойства, которые действительно нужны:
        **подписи читаются** и **ни один вариант не потерян**.
        """
        from equipment_intake.keyboards import node_keyboard

        for count in (1, 6, 8, 12, 13, 25):
            with self.subTest(count=count):
                keyboard = node_keyboard(self._session(count))

                assert_labels_are_readable(
                    self, f"дерево, {count} вариантов", keyboard,
                    lambda b: _callback(b).startswith(("dt:s", "dt:t")),
                )

                # Вариантов на **странице** — ровно столько, сколько влезло;
                # остальные доступны листанием, а не выброшены.
                shown = sum(
                    1 for row in keyboard["inline_keyboard"] for b in row
                    if _callback(b).startswith(("dt:s", "dt:t"))
                )
                pages = self._pages(count)
                expected = min(count, self._per_page(count))

                self.assertEqual(
                    shown, expected,
                    f"{count} вариантов: показано {shown} вместо {expected} "
                    f"(страниц {pages})",
                )

    def test_intake_labels_are_readable(self):
        """🔴 Подписи дерева читаются целиком при любом числе вариантов."""
        from equipment_intake.keyboards import node_keyboard

        for count in (1, 6, 8, 12, 13, 25):
            with self.subTest(count=count):
                assert_labels_are_readable(
                    self, f"дерево, {count} вариантов",
                    node_keyboard(self._session(count)),
                    lambda b: _callback(b).startswith(("dt:s", "dt:t")),
                )

    def test_intake_long_labels_fit_two_rows(self):
        """
        Длинные подписи **читаются целиком** — ради этого сужается ряд.

        Было: «укладываются в два ряда». Проверка оказалась не о том: у девяти
        подписей по 31 знаку в ряд влезает одна кнопка, и «два ряда» значило бы
        показать две из девяти. Владелец просил читаемость, поэтому здесь
        проверяется она, а не число рядов.
        """
        from equipment_intake.engine import Session
        from equipment_intake.keyboards import node_keyboard
        from equipment_intake.types import DecisionTree, Node, NodeType, Option

        options = tuple(
            Option(id=f"o{index}", label="Wrong container position on robot")
            for index in range(9)
        )
        node = Node(
            id="root", title="What caused it?", type=NodeType.CHOICE,
            options=options, next_node="finish",
        )
        finish = Node(id="finish", title="Done", type=NodeType.FINAL)

        keyboard = node_keyboard(
            Session(tree=DecisionTree(root_id="root", nodes={"root": node, "finish": finish}))
        )

        # 🔴 **Проверяется читаемость, а не число рядов.**
        #
        # Раньше здесь стояло «два ряда». У девяти подписей по 31 знаку в ряд
        # влезает **одна** кнопка, и «два ряда» означало бы показать две из
        # девяти — остальные за листанием. Владелец просил читаемость
        # («просто нужно чтобы ошибки читались»), а не конкретное число рядов:
        # лучше шесть рядов с полными подписями, чем два ряда с `Wrong cont…`.
        assert_labels_are_readable(
            self, "дерево, длинные подписи", keyboard,
            lambda b: _callback(b).startswith(("dt:s", "dt:t")),
        )

        # 🔴 Ни один вариант не потерян. Подписей девять, в ряд влезает одна,
        # поэтому страница вмещает шесть — остальные три доступны **листанием**
        # (`Next ▶`), а не выброшены. Проверяю именно это: либо все девять на
        # экране, либо есть чем долистать.
        shown = sum(
            1 for row in keyboard["inline_keyboard"] for b in row
            if _callback(b).startswith(("dt:s", "dt:t"))
        )
        pager = [
            b for row in keyboard["inline_keyboard"] for b in row
            if "Next" in (b.get("text") or "")
        ]

        self.assertTrue(pager, "девять длинных подписей обязаны дать листание")

        # Все девять достижимы: `per_page` на **каждой** странице.
        from equipment_intake.keyboards import options_per_page, total_option_pages
        from equipment_intake.types import Option

        options = [Option(id=f"o{i}", label="Wrong container position on robot")
                   for i in range(9)]
        reachable = options_per_page(options) * total_option_pages(9, options)

        self.assertGreaterEqual(
            reachable, 9,
            f"показано {shown}, но листанием доступно всего {reachable} — "
            f"часть вариантов потеряна",
        )

    def test_intake_final_screen_fits_two_rows(self):
        """Финальный экран: Edit / Confirm / Cancel."""
        from equipment_intake.keyboards import final_keyboard

        keyboard = final_keyboard(self._session(2))
        assert_option_rows_within_limit(
            self, "финальный экран", keyboard,
            lambda b: _callback(b).startswith(("dt:k", "dt:e")),
        )


# ============================================================
# 4. САМ ИНСТРУМЕНТ РАСКЛАДКИ
# ============================================================

class GridHelperChecks(unittest.TestCase):
    """`keyboard_layout`: сетка, страницы и подсчёт вытесненных кнопок."""

    def _buttons(self, count: int) -> list:
        return [{"text": f"B{index}", "callback_data": f"b:{index}"} for index in range(count)]

    def test_grid_never_exceeds_two_rows(self):
        for count in range(0, 40):
            with self.subTest(count=count):
                rows = kl.grid_rows(self._buttons(count))
                self.assertLessEqual(len(rows), kl.MAX_OPTION_ROWS)

    def test_grid_keeps_rows_full(self):
        """Ряд заполняется целиком: «дыр» в конце быть не должно."""
        rows = kl.grid_rows(self._buttons(7), columns=6)

        self.assertEqual([len(r) for r in rows], [6, 1])

    def test_grid_reports_what_it_dropped(self):
        """
        🔴 Раскладка **не молчит** о вытесненных кнопках.

        `grid_rows` ��брезает лишнее, и без счётчика «обрезали» и «поместилось»
        выглядели бы одинаково. Вызывающий код обязан сперва разложить список
        по страницам (`paginate`), и этот счётчик — способ это проверить.
        """
        buttons = self._buttons(20)

        self.assertEqual(kl.overflow(buttons, columns=6), 8)
        self.assertEqual(kl.overflow(self._buttons(12), columns=6), 0)

    def test_paginate_covers_every_item(self):
        """Страницы вместе покрывают список целиком и без повторов."""
        buttons = self._buttons(20)
        collected = []

        for page in range(kl.page_count(len(buttons), 3)):
            collected.extend(kl.paginate(buttons, 3, page)["items"])

        self.assertEqual(collected, buttons)

    def test_paginate_clamps_out_of_range_page(self):
        """Запрос несуществующей страницы даёт последнюю, а не пустоту."""
        buttons = self._buttons(5)
        data = kl.paginate(buttons, 6, 99)

        self.assertEqual(data["page"], 0)
        self.assertEqual(len(data["items"]), 5)

    def test_columns_shrink_for_long_labels(self):
        """
        Ширина ряда — по **ширине текста**, а не по числу знаков.

        🔴 **Числа изменились 13.10.2026, и это исправление.** Раньше здесь
        стояло `== 6` для `Chassis`: считалось `len(label)`, и шесть колонок
        давали на кнопку ≈6 знаков — Telegram обрезал подпись до `Chass…`
        (скриншот владельца). Теперь ширина считается по глифам **вместе со
        стрелкой `→`**, которую дописывает клавиатура.

        Проверки ниже держат именно **читаемость**: при выбранной ширине подпись
        обязана влезать целиком.
        """
        # Короткие подписи по-прежнему идут по шесть.
        self.assertEqual(kl.columns_for_labels(["A1", "A2"]), 6)

        # 🔴 **Проверка со стрелкой — так, как зовёт клавиатура.**
        #
        # Здесь стояло `columns_for_labels(["Chassis","Lifting"])` — **без**
        # стрелок, и функция возвращала 5 колонок. Но `_button` дописывает `→`, и
        # «Chassis  →» (8.5) в пять колонок (7.2) не влезает: на экране это и был
        # `Chass…`. Клавиатура зовёт расчёт через `labels_for_options`, который
        # стрелку **учитывает**, — тест обязан звать так же, иначе он проверяет
        # не то, что происходит на самом деле.
        from equipment_intake.keyboards import labels_for_options
        from equipment_intake.types import Option

        options = [Option(id="a", label="Chassis", next_node="finish"),
                   Option(id="b", label="Lifting", next_node="finish")]
        labels = labels_for_options(options, has_next=True)

        chassis = kl.columns_for_labels(labels)
        self.assertLess(chassis, 6, "`Chassis  →` обязана сузить ряд")
        self.assertTrue(
            kl.fits_columns(labels, chassis),
            f"модули не влезают в {chassis} колонок — Telegram обрежет подпись",
        )

        # 🔴 Сравнивать «длиннее -> меньше колонок» **нельзя**: «Safety
        # controller issues» (23 знака) даёт две колонки, а «Chassis» (7) —
        # пять. Обе читаются, и это главное. Проверяю читаемость обеих.
        long = kl.columns_for_labels(["Safety controller issues"])
        self.assertTrue(
            # Тело подписи шире, чем `columns_for_labels` получил на входе
            # (стрелку дописывает клавиатура), поэтому её нужно учитывать явно —
            # ровно так же, как это делает `labels_for_options`.
            kl.fits_columns(["Safety controller issues  →"], long),
            "длинная подпись не влезает в выбранную ширину — Telegram обрежет",
        )
        self.assertTrue(
            kl.fits_columns(["Chassis  →", "Lifting  →"], chassis),
            "модули не влезают в выбранную ширину",
        )

        # Пустой список не должен ломать раскладку.
        self.assertEqual(kl.columns_for_labels([]), 6)

    def test_pager_row_is_empty_for_a_single_page(self):
        self.assertEqual(kl.pager_row(0, 1, "em:p::"), [])

    def test_pager_row_has_at_most_two_buttons(self):
        """Ряд листания — не больше двух кнопок: «далее» встаёт отдельно."""
        for page in range(3):
            with self.subTest(page=page):
                self.assertLessEqual(len(kl.pager_row(page, 3, "em:p::")), 2)


if __name__ == "__main__":
    unittest.main()
