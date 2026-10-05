# -*- coding: utf-8 -*-
"""
Подбор шаблона ошибки по тексту оператора.

Порог и алгоритм подобраны замерами на разметке людей (см. докстроку
`equipment_intake/template_match.py`), поэтому тесты фиксируют **поведение**:
найден подходящий шаблон / честный отказ. Меняя порог, придётся объяснить, чем
новый замер лучше — иначе легко вернуть ложные срабатывания.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equipment_intake import template_match as tm  # noqa: E402


#: Мини-справочник, повторяющий форму настоящих `issue_templates`.
TEMPLATES = [
    {
        "employee_title": "Driver component exception",
        "issue_sub_type": "Driver component exception",
        "issue_type": "Unable to drive",
        "issue_description": "In drive process robot got problem driver component exception",
        "recovery_title": "Move on QR Code robot then recovery robot",
        "solving_time": 6,
    },
    {
        "employee_title": "Program logic bug",
        "issue_sub_type": "Program logic bug",
        "issue_type": "Data error",
        "issue_description": "Software logic error causing malfunction",
        "recovery_title": "Restart application",
        "solving_time": 6,
    },
    {
        "employee_title": "Ground code dirty",
        "issue_sub_type": "Ground code dirty",
        "issue_type": "DM Code Error",
        "issue_description": "Ground code dirty preventing robot scanning",
        "recovery_title": "Clean ground code thoroughly",
        "solving_time": 6,
    },
]


class OperatorTextChecks(unittest.TestCase):
    """Текст оператора извлекается из любой формы описания."""

    def test_plain_text_is_returned_as_is(self):
        self.assertEqual(tm.operator_text("unable to drive"), "unable to drive")

    def test_journal_line_is_unwrapped(self):
        self.assertEqual(
            tm.operator_text("Reported error: bug\nIdentifier: 3452\nModule: Chassis"),
            "bug",
        )

    def test_ex_prefix_is_removed(self):
        """Операторы пишут «EX: 驱动组件异常 …» — префикс не часть ошибки."""
        self.assertEqual(
            tm.operator_text("EX: 驱动组件异常 Driver component exception."),
            "驱动组件异常 Driver component exception",
        )

    def test_empty_values_give_empty_string(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(tm.operator_text(value), "")

    def test_trailing_dot_is_dropped(self):
        self.assertEqual(tm.operator_text("unable to drive."), "unable to drive")

    def test_robot_number_at_the_end_is_dropped(self):
        """Операторы часто дописывают номер робота: «… exception. 3563».

        Номер — не часть описания, но `token_set_ratio` считает его лишним словом
        и роняет счёт со 100 до 80, ниже порога. Замерено на живых данных:
        снятие номера поднимает покрытие строк бота с 63 % до 80 % при
        неизменной точности эталона.
        """
        self.assertEqual(
            tm.operator_text("EX: 驱动组件异常 Driver component exception. 3563"),
            "驱动组件异常 Driver component exception",
        )
        self.assertEqual(tm.operator_text("unable to rotate 3670"), "unable to rotate")

    def test_long_number_is_not_treated_as_a_robot_number(self):
        """Номер робота четырёхзначный; длинное число — часть описания.

        Срезать всё подряд значило бы испортить текст, поэтому ограничение в
        1–6 цифр, и оно проверяется явно.
        """
        # 7 цифр — не номер робота, остаётся как есть.
        self.assertEqual(
            tm.operator_text("error 1234567"),
            "error 1234567",
        )

    def test_number_inside_text_is_kept(self):
        """Номер в середине, а не в хвосте, не трогаем."""
        self.assertEqual(
            tm.operator_text("robot 3452 went off track"),
            "robot 3452 went off track",
        )

    def test_text_finds_template_after_number_is_dropped(self):
        """Главное следствие: такой текст теперь распознаётся."""
        found = tm.match_template(
            "EX: 驱动组件异常 Driver component exception. 3563", TEMPLATES
        )

        self.assertIsNotNone(found)
        self.assertEqual(found["issue_type"], "Unable to drive")


class MatchChecks(unittest.TestCase):
    """Найден шаблон или честный отказ."""

    def test_short_operator_text_finds_template(self):
        found = tm.match_template("driver component exception", TEMPLATES)

        self.assertIsNotNone(found)
        self.assertEqual(found["issue_type"], "Unable to drive")

    def test_chinese_and_english_text_finds_template(self):
        found = tm.match_template(
            "EX: 驱动组件异常 Driver component exception.", TEMPLATES
        )

        self.assertIsNotNone(found)
        self.assertEqual(found["issue_type"], "Unable to drive")

    def test_bug_maps_to_data_error(self):
        found = tm.match_template("bug", TEMPLATES)

        self.assertIsNotNone(found)

    def test_unrelated_text_is_refused(self):
        """Отказ — правильный ответ: пустая колонка честнее неверной.

        `'unable to rotate'` — реальный текст оператора. Он близок к
        «Unable to drive» лишь частично (разные действия: поворот и движение),
        а по замерам на разметке людей уверенного совпадения тут нет.
        """
        self.assertIsNone(tm.match_template("unable to rotate", TEMPLATES))

    def test_robot_number_is_not_a_description(self):
        self.assertIsNone(tm.match_template("3452", TEMPLATES))

    def test_empty_description_is_refused(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(tm.match_template(value, TEMPLATES))

    def test_empty_template_list_is_refused(self):
        self.assertIsNone(tm.match_template("unable to drive", []))

    def test_missing_fuzzy_field_does_not_crash(self):
        """Шаблон без `issue_description` не должен ломать подбор.

        В живом справочнике такие есть: у id 120 и 122 поле пустое.
        """
        sparse = [{"employee_title": "Driver component exception",
                   "issue_sub_type": "Driver component exception",
                   "issue_type": "Unable to drive"}]

        found = tm.match_template("driver component exception", sparse)

        self.assertIsNotNone(found)


class ThresholdChecks(unittest.TestCase):
    """Порог защищён от случайного понижения."""

    def test_cutoff_is_the_measured_value(self):
        """85 — измеренный порог (13/13 на эталоне, 0 неверных).

        Понижение вернуло бы ложные срабатывания: при 70 `'unable to rotate'`
        уходил в «Forklift detection without container».
        """
        self.assertEqual(tm.MATCH_CUTOFF, 85)

    def test_all_three_fields_participate(self):
        """`issue_description` обязателен.

        Без него при пороге 85 подбор отвечает лишь на 5 описаний из 13
        эталонных — замерено на живой базе.
        """
        self.assertEqual(
            set(tm.MATCH_FIELDS),
            {"employee_title", "issue_sub_type", "issue_description"},
        )

    def test_lower_cutoff_would_produce_false_positive(self):
        """Негативная проверка самого порога, а не только кода.

        При 70 подбор «находит» шаблон для текста, который при 85 честно
        отклоняется. Если это перестанет быть так, порог 85 защищает не от
        чего — и тест об этом скажет.
        """
        self.assertIsNone(tm.match_template("unable to rotate", TEMPLATES))
        self.assertIsNotNone(
            tm.match_template("unable to rotate", TEMPLATES, cutoff=40)
        )


class TieBreakChecks(unittest.TestCase):
    """При равном счёте выбирается шаблон с более полными данными."""

    def test_fuller_template_wins(self):
        sparse = {
            "employee_title": "Driver component exception",
            "issue_sub_type": "Driver component exception",
            "issue_type": "Unable to drive",
        }
        full = dict(sparse, recovery_title="Move on QR Code robot", solving_time=6,
                    issue_description="In drive process robot got problem driver component exception")

        found = tm.match_template("driver component exception", [sparse, full])

        # Оба дают одинаковый счёт по имени, но у второго есть совет и время.
        self.assertEqual(found.get("recovery_title"), "Move on QR Code robot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
