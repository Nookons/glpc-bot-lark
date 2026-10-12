# -*- coding: utf-8 -*-
"""Связь ошибки дерева со справочником `issue_templates`.

**Что здесь проверяется.** Требование владельца 12.10.2026, дословно: «нужно
сделать **связь** как-то и при добавление новой ошибки делать **заготовку**
тоже чтобы потом были поля которые будут **подставлены автоматически при
копирование** и **на сервере в столбцы с описанием ошибки что было сделано и
тп»».

Три вещи по порядку:

1. **связь** — вариант дерева («Went off the road») однозначно указывает на
   запись справочника, и указывает **по id**, а не подбором по тексту;
2. **заготовка** — при добавлении новой ошибки строка справочника создаётся
   сразу и связывается с вариантом;
3. **подстановка** — при выборе ошибки поля шаблона уезжают в журнал
   (`issue_description`, `recovery_title`, `solving_time`, `template_id`).

**Почему точная связь, а не подбор.** Естественный ключ справочника не уникален:
`equipment_type` + `issue_sub_type` даёт **2** дубля (`Equipment` +
`Network disconnection` и `Equipment` + `Driver component exception`). Дубль
`Driver component exception` — тот самый, из-за которого оператор писал «driver
component exception», а в журнал попадало `Speed error`. Проверено на живой базе
12.10.2026.

**Ключевой негативный сторож.** `test_wrong_template_is_not_substituted`:
если связь сломается и шаблон будет подставлен не тот (или подставлен по тексту
вместо связи), тест обязан упасть.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"
os.environ["TELEGRAM_DRY_RUN"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")

from equipment_intake import error_templates as et  # noqa: E402
from equipment_intake import report_writer as rw  # noqa: E402
from equipment_intake import template_match as tm  # noqa: E402
from equipment_intake.tree_config import DEFAULT_TREE, MODULE_ERRORS  # noqa: E402

#: Заготовки справочника, **как они лежат в живой базе после миграции `0109`**.
#: `recovery_title` и `solving_time` пусты — так их создаёт заготовка, и это
#: проверяется отдельно (выдумывать время решения нельзя).
TEMPLATES = [
    {
        "id": 152,
        "employee_title": "Went off the road",
        "issue_type": "Chassis",
        "issue_sub_type": "Went off the road",
        "issue_description": "Went off the road",
        "recovery_title": None,
        "solving_time": None,
    },
    {
        "id": 155,
        "employee_title": "QR Code dirty",
        "issue_type": "QR Code",
        "issue_sub_type": "QR Code dirty",
        "issue_description": "QR Code dirty",
        "recovery_title": None,
        "solving_time": None,
    },
    {
        "id": 156,
        "employee_title": "Safety system disconnect",
        "issue_type": "Safety",
        "issue_sub_type": "Safety system disconnect",
        "issue_description": "Safety system disconnect",
        "recovery_title": "Restart the safety module",
        "solving_time": 6,
    },
    # Дубль по `issue_sub_type`: тот самый, из-за которого подбор по тексту
    # выбирал не тот шаблон. Нужен, чтобы сторож ниже падал при возврате подбора.
    {
        "id": 10,
        "employee_title": "Speed error",
        "issue_type": "Unable to drive",
        "issue_sub_type": "Driver component exception",
        "issue_description": "Speed error",
        "recovery_title": "Reboot the robot",
        "solving_time": 4,
    },
]


def answer_step(node_id, option_id, label):
    """Шаг пути в том виде, в каком его отдаёт `engine`."""
    return {"node_id": node_id, "option_id": option_id, "label": label,
            "key": "module_error", "value": label, "next_node": None}


class SeedCatalogueChecks(unittest.TestCase):
    """Карта заготовок совпадает с контрактом дерева одиннадцать-в-один.

    Расхождение в букве разорвало бы связь **молча**: оператор выбрал бы ошибку,
    а в журнал не подставилось ничего. Поэтому сверяется не «примерно», а каждая
    из 11 опций слоя `robot_errors_*`.
    """

    def test_tree_exposes_eleven_error_options(self):
        found = []

        for node in DEFAULT_TREE.nodes.values():
            if et.is_error_node(node.id):
                found.extend(option.id for option in node.options)

        self.assertEqual(len(found), 11, f"опций слоя ошибок: {found}")
        self.assertEqual(len(set(found)), 11, f"дубли id опций: {found}")

    def test_every_tree_error_option_has_a_seed(self):
        """Каждая опция узла ошибок находит свою заготовку — по id и по тексту."""
        seen = 0

        for node in DEFAULT_TREE.nodes.values():
            if not et.is_error_node(node.id):
                continue

            for option in node.options:
                with self.subTest(option=option.id):
                    by_id = et.seed_for_option(option.id, None, node.id)
                    by_label = et.seed_for_option(None, option.label, node.id)

                    self.assertIsNotNone(by_id, f"нет заготовки по id {option.id!r}")
                    self.assertIsNotNone(by_label, f"нет заготовки по тексту {option.label!r}")
                    self.assertIs(by_id, by_label, "id и текст ведут к разным заготовкам")
                    self.assertEqual(by_id["label"], option.label)
                    seen += 1

        self.assertEqual(seen, 11)

    def test_seed_labels_match_the_owner_scheme(self):
        """Тексты заготовок — дословно со схемы владельца.

        Включая его написание `trigger` (не `triggered`) и регистр `HORN`/`Hook`:
        тексты служат ключом связи, и «исправлять» их нельзя.
        """
        labels = {spec["label"] for spec in et.template_specs()}

        self.assertIn("Can't extend/expand the HORN", labels)
        self.assertIn("Can't extend/expand the Hook", labels)
        self.assertIn("Front emergency button is trigger", labels)
        self.assertIn("Rear emergency button is trigger", labels)
        self.assertNotIn("Front emergency button is triggered", labels)

    def test_seeds_match_the_tree_contract(self):
        """Карта кода не расходится с `MODULE_ERRORS` дерева.

        Две копии одного списка — это ровно тот класс дефекта, который в
        эко-системе дважды терял данные. Здесь копии сверяются автоматически.
        """
        from_tree = {
            option_id
            for options in MODULE_ERRORS.values()
            for option_id, _label in options
        }
        from_map = {
            option_id
            for spec in et.template_specs()
            for option_id in spec["option_ids"]
        }

        self.assertTrue(
            from_tree.issubset(from_map),
            f"дерево знает ошибки, которых нет в карте: {sorted(from_tree - from_map)}",
        )

    def test_cause_option_is_not_taken_for_a_module_error(self):
        """🔴 Вариант-ПРИЧИНА `went_off_the_road` не связывается с заготовкой.

        В живом дереве на `ask_cause` (причина) есть вариант с тем же `option_id`,
        что у ошибки шасси. Это **разные** понятия: причина отвечает «почему», а
        не «что сломалось». Без ограничения по узлу причина получала бы
        `issue_type = Chassis`, и в журнал уезжал бы тип, к делу не относящийся.

        **Негативная проверка:** убрать проверку `is_error_node` в
        `seed_for_option` — тест обязан упасть.
        """
        self.assertIsNone(et.seed_for_option("went_off_the_road", None, "ask_cause"))
        self.assertIsNone(et.seed_for_option(None, "Went off the road", "ask_cause"))
        # На узле ошибок тот же id связывается — иначе ограничение было бы
        # простым запретом, а не разделением понятий.
        self.assertIsNotNone(
            et.seed_for_option("went_off_the_road", None, "robot_errors_chassis")
        )

    def test_module_names_come_from_the_node(self):
        """`robot_errors_qr_code` → `QR Code`; чужой узел модуля не даёт."""
        self.assertEqual(et.module_from_node("robot_errors_qr_code"), "QR Code")
        self.assertEqual(et.module_from_node("robot_errors_safety"), "Safety")
        self.assertEqual(et.module_from_node("ask_cause"), "")
        self.assertEqual(et.module_from_node(None), "")

    def test_stub_row_does_not_invent_recovery_or_time(self):
        """🔴 Заготовка не выдумывает «что было сделано» и время решения.

        В справочнике `solving_time` 2–8 минут, и выдуманное число испортило бы
        метрику «сколько решается ошибка». Заполняет их человек.

        **Негативная проверка:** подставить в `stub_row` любое число вместо
        `None` — тест обязан упасть.
        """
        row = et.stub_row("Went off the road", module="Chassis", created_by=7)

        self.assertIsNone(row["solving_time"])
        self.assertIsNone(row["recovery_title"])
        self.assertEqual(row["equipment_type"], "Equipment")
        # Описание ошибки и подтип берутся из названия — владелец дал именно его.
        self.assertEqual(row["issue_description"], "Went off the road")
        self.assertEqual(row["issue_sub_type"], "Went off the road")
        self.assertEqual(row["issue_type"], "Chassis")
        self.assertEqual(row["created_by"], 7)


class ExactLinkChecks(unittest.TestCase):
    """Связь берётся по `option_id`, а не подбором по тексту."""

    def test_link_resolves_by_option_id(self):
        found = et.resolve(
            TEMPLATES, option_id="chassis_went_off_road", node_id="robot_errors_chassis"
        )

        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 152)

    def test_stored_link_wins_over_the_code_map(self):
        """Ссылка из базы сильнее карты кода: админ мог связать иначе.

        Иначе «выбрал другой шаблон в `/tree`» не работало бы, а владелец решил
        бы, что кнопка не сохраняет.
        """
        found = et.resolve(
            TEMPLATES,
            option_id="chassis_went_off_road",
            node_id="robot_errors_chassis",
            template_id=156,
        )

        self.assertEqual(found["id"], 156)

    def test_linked_template_walks_the_path_backwards(self):
        """Берётся **последняя** выбранная ошибка: путь идёт от общего к частному."""
        steps = [
            answer_step("robot_type", "a42t", "A42T"),
            answer_step("robot_module_a42t", "chassis", "Chassis"),
            answer_step("robot_errors_chassis", "chassis_went_off_road", "Went off the road"),
        ]

        found = et.linked_template(steps, TEMPLATES)

        self.assertEqual(found["id"], 152)

    def test_linked_template_uses_the_option_links_map(self):
        """Связь из базы отдаётся картой `option_id → template_id`."""
        steps = [
            answer_step("robot_errors_safety", "safety_system_disconnect", "Safety system disconnect"),
        ]

        found = et.linked_template(steps, TEMPLATES, {"safety_system_disconnect": 156})

        self.assertEqual(found["id"], 156)

    def test_unknown_option_gives_nothing(self):
        """Незнакомый вариант не «угадывается»: связи нет — и это честный ответ."""
        steps = [answer_step("robot_errors_chassis", "some_new_error", "Some new error")]

        self.assertIsNone(et.linked_template(steps, TEMPLATES))


class SubstitutionChecks(unittest.TestCase):
    """Поля шаблона подставляются в журнал при выборе ошибки."""

    def setUp(self):
        rw.reset_templates_cache()
        self.addCleanup(rw.reset_templates_cache)

    def _row(self, steps, description="Went off the road", links=None):
        """Строка журнала для пути с выбранной ошибкой.

        Связь читается писателем через `storage.option_template_links`, поэтому
        здесь она подменяется картой: сети нет, но путь ровно тот же, что в бою.
        """
        result = {
            "answers": {
                "object": "Robot",
                "device_type": "A42T",
                "module": "Chassis",
                "device_number": "3490",
                "description": description,
            },
            "path": steps,
            "warehouse": "GLP-C",
            "employee": "Smoke User",
            "user_id": 12345,
            "username": "smoke-user",
            "chat_id": -100123,
            "message_id": 777,
        }

        with mock.patch.object(rw, "canonical_type", return_value=None), \
             mock.patch.object(rw, "load_templates", return_value=TEMPLATES), \
             mock.patch(
                 "equipment_intake.storage.option_template_links",
                 return_value=dict(links or {}),
             ), \
             mock.patch("sendToDataBase.warehouse_id", return_value=1):
            return rw.build_row(result, "GLP-C")

    def test_fields_come_from_the_linked_template(self):
        """Описание, «что было сделано» и время решения — из шаблона.

        Это и есть требование владельца: «на сервере в столбцы с описанием ошибки
        что было сделано и тп».
        """
        steps = [
            answer_step("robot_errors_safety", "safety_system_disconnect", "Safety system disconnect"),
        ]

        row = self._row(steps, links={"safety_system_disconnect": 156})

        self.assertEqual(row["template_id"], 156)
        self.assertEqual(row["equipment_category"], "Safety")
        self.assertEqual(row["raw_recovery"], "Restart the safety module")
        self.assertEqual(row["raw_solving_time"], 6)
        self.assertEqual(row["raw_fault"], "Safety system disconnect")

    def test_template_ref_has_priority_over_a_better_text_match(self):
        """🔴 Точная связь сильнее **лучшего** совпадения по тексту.

        Проверка построена так, чтобы подбор по тексту дал **другой** ответ:
        описание оператора дословно повторяет `Speed error` (id 10), но путь
        указывает на заготовку «Went off the road» (id 152). Победить обязана
        связь.

        **Негативная проверка:** вернуть в `resolve_template` порядок «сначала
        подбор, потом связь» — тест обязан упасть, потому что в журнал уехал бы
        id 10 и `Unable to drive`.
        """
        steps = [
            answer_step("robot_errors_chassis", "chassis_went_off_road", "Went off the road"),
        ]

        row = self._row(steps, description="Speed error", links={"chassis_went_off_road": 152})

        self.assertEqual(row["template_id"], 152)
        self.assertEqual(row["equipment_category"], "Chassis")

    def test_fallback_text_match_is_kept_for_legacy_paths(self):
        """Запасной путь работает: путь без `option_id` — шаблон ищется по тексту.

        Старые и нетиповые записи связи не имеют, и их поведение обязано
        остаться прежним. Выбрасывать подбор вместе со связью значило бы чинить
        одно и ломать другое.
        """
        row = self._row([], description="driver component exception")

        # Подбор по тексту нашёл шаблон из дублей — и это ожидаемое поведение
        # запасного пути (порог 85, выбор по названию).
        self.assertEqual(row["template_id"], 10)
        self.assertEqual(row["equipment_category"], "Unable to drive")

    def test_unlinked_error_does_not_get_a_random_template(self):
        """Ошибка без связи и без совпадения по тексту не получает шаблон вовсе.

        Пустая ссылка честнее неверной: иначе отчёт «Went off the road» уехал бы
        в тип `Unable to drive`, и метрика ошибок стала бы неверной.
        """
        steps = [answer_step("robot_errors_chassis", "brand_new_error", "Brand new error")]

        row = self._row(steps, description="zzz unknown wording")

        self.assertIsNone(row["template_id"])
        self.assertEqual(row["raw_solving_time"], 0)
        self.assertIsNone(row["raw_recovery"])

    def test_empty_template_time_keeps_end_equal_to_start(self):
        """Пустое время решения — не ошибка: конец отчёта равен началу.

        Так же ведёт себя шаблон без времени, и заготовка создаётся именно
        пустой (время заполнит человек).
        """
        from datetime import datetime

        steps = [
            answer_step("robot_errors_safety", "safety_system_disconnect", "Safety system disconnect"),
        ]

        with mock.patch.object(rw, "build_rows", wraps=rw.build_rows):
            row = self._row(steps, links={"safety_system_disconnect": 155})

        self.assertEqual(row["end_time"], row["start_time"])
        # Связь на «QR Code dirty» (пустое время) и модуль из неё.
        self.assertEqual(row["template_id"], 155)
        self.assertEqual(row["equipment_category"], "QR Code")
        self.assertIsInstance(datetime.fromisoformat(row["start_time"]), datetime)

    def test_operator_text_is_never_replaced_by_the_template(self):
        """Связь подставляет поля шаблона, но **не** подменяет текст оператора.

        `reported_error` — единственное место, где видно, что написал человек;
        если бы шаблон его переписывал, разбор инцидентов был бы невозможен.
        """
        steps = [
            answer_step("robot_errors_safety", "safety_system_disconnect", "Safety system disconnect"),
        ]

        row = self._row(steps, description="robot stopped, red light on", links={"safety_system_disconnect": 156})

        self.assertEqual(row["reported_error"], "robot stopped, red light on")

    def test_issue_description_comes_from_the_template_on_an_exact_link(self):
        """🔴 При точной связи описание ошибки в журнале — **из шаблона**.

        Требование владельца, дословно: «на сервере в столбцы с описанием ошибки
        что было сделано и тп». Колонка `raw_description` — это и есть «问题描述
        Issue Description» в выгрузке смены (`tk-new`), и читатели предпочитают
        её справочнику, поэтому заполнить её обязан писатель.

        Проверка нашла дефект: до этой правки описание из шаблона **не
        подставлялось нигде** — читатель видел только текст оператора.

        **Негативная проверка:** убрать `linked_description` из `build_rows` —
        тест обязан упасть.
        """
        steps = [
            answer_step("robot_errors_qr_code", "qr_code_dirty", "QR Code dirty"),
        ]

        row = self._row(
            steps, description="dm code on the floor is unreadable",
            links={"qr_code_dirty": 155},
        )

        self.assertEqual(row["raw_description"], "QR Code dirty")
        # Текст оператора не потерян — он в своей колонке.
        self.assertEqual(row["reported_error"], "dm code on the floor is unreadable")

    def test_text_match_keeps_the_operator_text_in_the_description(self):
        """При подборе **по тексту** описание остаётся текстом оператора.

        Уверенности нет: подставить описание «похожего» шаблона значило бы выдать
        догадку за факт. Это и есть обратная совместимость — поведение запасного
        пути не изменилось.
        """
        row = self._row([], description="driver component exception")

        # Шаблон подобран (id 10), но описание — прежнее, от оператора.
        self.assertEqual(row["template_id"], 10)
        self.assertEqual(row["raw_description"], "driver component exception")


class ResolveTemplateChecks(unittest.TestCase):
    """`resolve_template` — единая точка выбора: связь, затем подбор."""

    def test_exact_link_is_used_first(self):
        steps = [answer_step("robot_errors_chassis", "chassis_went_off_road", "Went off the road")]

        found = tm.resolve_template("speed error", TEMPLATES, steps=steps)

        self.assertEqual(found["id"], 152)

    def test_text_match_is_the_fallback(self):
        found = tm.resolve_template("driver component exception", TEMPLATES, steps=[])

        self.assertEqual(found["id"], 10)

    def test_no_match_at_all_returns_none(self):
        found = tm.resolve_template("zzz nothing similar", TEMPLATES, steps=[])

        self.assertIsNone(found)

    def test_option_links_map_is_honoured(self):
        steps = [answer_step("robot_errors_chassis", "chassis_went_off_road", "Went off the road")]

        found = tm.resolve_template(
            "speed error", TEMPLATES, steps=steps,
            option_links={"chassis_went_off_road": 10},
        )

        self.assertEqual(found["id"], 10)


if __name__ == "__main__":
    unittest.main()
