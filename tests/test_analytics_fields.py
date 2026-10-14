"""
🔴 **Топы и рапорты читают ОСМЫСЛЕННЫЕ поля.**

Аудит 14.10.2026 нашёл класс дефектов, который не ловился ничем: команда
формально работает, запрос валиден, ошибок в логе нет — а читателю показывают
**не то поле**. Тесты здесь стерегут именно это, потому что `pytest` зелёный и
на сломанном коде: старая реализация тоже «работала».

Три подтверждённых случая (все на живых данных):

| Что показывали | Что лежало в поле | Масштаб |
|---|---|---|
| «设备Equipment — 119 (93 %)» как **ошибку** смены | категория оборудования | 14 277 строк из 24 674 (58 %) по GLP-C |
| «robot», «Chassis» в топе **ошибок** | категория и имя модуля | 1 132 строки из 1 444 (78 %) за неделю |
| «#None» в топе **роботов** | `str(None)` от пустого номера | 74 записи из 198 (37 %) на смене 04.10 ночь |

Поэтому проверки ниже формулируются **через смысл**: «в топе ошибок не может
быть категории», «запись без робота не становится роботом по имени None».
"""
from __future__ import annotations

import os
import sys
import unittest
# 🔴 **`unittest.mock` импортируется явно.** В Python 3.9 он не подгружается
# вместе с `unittest` сам, и обращение `unittest.mock.patch` падает с
# `AttributeError` — то есть тест не проверяет ничего, а падает на импорте.
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics  # noqa: E402
import journal  # noqa: E402
import shift_report  # noqa: E402


class MeaningfulIssueChecks(unittest.TestCase):
    """Правило выбора типа ошибки — одно на все команды и отчёты."""

    def test_category_is_not_an_issue(self):
        """Категория оборудования не может быть типом ошибки."""
        row = {
            "equipment_category": "robot",
            "raw_fault": "robot",
            "raw_template_title": "Chassis",
            "reported_error": "Unable to rotate",
        }

        # Все «поля ошибок» содержат категорию и модуль — остаётся `reported_error`.
        self.assertEqual(journal.meaningful_issue(row), "Unable to rotate")

    def test_localized_category_is_skipped(self):
        """`设备Equipment` — локализованная категория, а не ошибка.

        На живой базе это 14 277 строк из 24 674 (58 %), и на длинном периоде
        она занимала **первое место** в топе ошибок.
        """
        row = {"issue_type": "设备Equipment", "raw_fault": "Unable to drive"}

        self.assertEqual(journal.meaningful_issue(row), "Unable to drive")

    def test_module_name_is_skipped(self):
        """Имя модуля отвечает на «где», а не на «что»."""
        row = {"raw_template_title": "Lifting", "raw_fault": "Problem Cannot located"}

        self.assertEqual(journal.meaningful_issue(row), "Problem Cannot located")

    def test_glued_chinese_prefix_is_trimmed(self):
        """Склейка `行走异常Unable to drive` — это одна ошибка, а не две."""
        row = {"raw_fault": "行走异常Unable to drive"}

        self.assertEqual(journal.meaningful_issue(row), "Unable to drive")

    def test_fault_wins_over_template_title(self):
        """
        🔴 **`raw_fault` важнее `raw_template_title`** — проверено на данных.

        Это неочевидно. В `raw_template_title` лежит **название заготовки**, а в
        `raw_fault` — сама ошибка. У 262 строк GLP-C в названии стоит
        `Unknown reason` («причина неизвестна»), тогда как рядом записана
        конкретная ошибка `Problem Cannot located`; в другом случае название —
        «Driver component exception», а подтип — «Speed error».

        Показать название вместо ошибки значит скрыть то, что известно.

        **Это же подтверждает старый код:** он читал `issue_type`, а запасным
        путём держал `first_column` — то есть `raw_fault` (см. `legacy_row`).
        Значит `raw_fault` и был основным полем.
        """
        row = {
            "raw_template_title": "Driver component exception",
            "raw_fault": "Speed error",
        }

        self.assertEqual(journal.meaningful_issue(row), "Speed error")

    def test_unknown_reason_does_not_hide_the_real_fault(self):
        """«Unknown reason» — название заготовки, а не ответ «неизвестно»."""
        row = {
            "raw_template_title": "Unknown reason",
            "raw_fault": "Problem Cannot located",
        }

        self.assertEqual(journal.meaningful_issue(row), "Problem Cannot located")

    def test_nothing_meaningful_gives_unknown(self):
        """Когда осмысленного поля нет — `unknown`, а не категория.

        Подставить категорию значило бы выдать её за ошибку; `unknown` виден в
        отчёте и показывает, что заготовка не заполнена.
        """
        self.assertEqual(journal.meaningful_issue({"issue_type": "设备Equipment"}), "unknown")
        self.assertEqual(journal.meaningful_issue({}), "unknown")


class TopReportChecks(unittest.TestCase):
    """Топ роботов не выдумывает робота из пустого номера."""

    def _report(self, rows):
        """Считает топ по строкам, минуя чтение базы."""
        with mock.patch.object(analytics, "fetch_exceptions", return_value=rows):
            return analytics.top_report("week", warehouse="GLP-C")

    def test_missing_robot_is_not_a_robot(self):
        """`str(None)` давал строку `"None"` и она попадала в топ как робот.

        На живой смене таких записей 74 из 198 (37 %), и «None» возглавлял
        список — то есть главное (у 37 % смены не указан робот) было скрыто.
        """
        report = self._report([
            {"error_robot": None, "raw_fault": "Unable to drive"},
            {"error_robot": None, "raw_fault": "Unable to drive"},
            {"error_robot": "3780", "raw_fault": "Unable to drive"},
        ])

        robots = dict(report["robots"])

        self.assertNotIn("None", robots, "пустой номер снова стал роботом")
        self.assertEqual(robots.get("3780"), 1)
        # И при этом потеря не скрыта — она названа числом.
        self.assertEqual(report["unassigned"], 2)

    def test_unassigned_is_shown_in_the_text(self):
        """Сумма по роботам должна сходиться с итогом — строка это объясняет."""
        report = self._report([
            {"error_robot": None, "raw_fault": "X"},
            {"error_robot": "3780", "raw_fault": "X"},
        ])

        text = analytics.format_top_report(report)

        self.assertIn("no robot in the record", text)

    def test_issue_top_has_no_categories(self):
        """В топе ошибок не может быть категории оборудования или модуля."""
        report = self._report([
            {"issue_type": "设备Equipment", "raw_fault": "Unable to drive"},
            {"equipment_category": "robot", "raw_template_title": "Chassis",
             "reported_error": "Unable to rotate"},
        ])

        issues = [issue for issue, _ in report["issues"]]

        self.assertIn("Unable to drive", issues)
        self.assertIn("Unable to rotate", issues)
        for banned in ("设备Equipment", "robot", "Chassis"):
            self.assertNotIn(banned, issues, f"категория/модуль {banned!r} в топе ошибок")


class ShiftReportChecks(unittest.TestCase):
    """Сменный отчёт: метрики читаются из тех полей и названы честно."""

    def _data(self, rows):
        """
        Считает метрики по строкам, минуя чтение базы.

        🔴 **Патчится именно то имя, которое вызовет код.** `shift_report`
        импортирует `shift_report_data` **напрямую** (не через модуль), поэтому
        подмена `sendToDataBase.get_shift_exceptions` не влияет на вызов — а
        патч при этом **утекает** в соседние тесты: их `mock.patch` снимает
        подмену с объекта, которого вызов не касался, и настоящая функция
        остаётся подменённой навсегда.

        Именно это и случилось: чужой тест
        `test_confirm_message_reports_success` падал **только в полном прогоне**
        и проходил в одиночку. Патчим имя там, где его прочитает вызывающий код
        (`sendToDataBase.get_shift_exceptions`), а сам вызов делаем внутри
        контекста.
        """
        with mock.patch(
            "sendToDataBase.get_shift_exceptions", return_value=rows
        ), mock.patch.object(
            shift_report, "shift_report_data",
            side_effect=lambda *a, **kw: __import__("sendToDataBase").shift_report_data(
                "2026-10-14", "night", warehouse="GLP-C"
            ),
        ):
            return shift_report.shift_report_data("2026-10-14", "night", warehouse="GLP-C")

    def test_shift_top_uses_meaningful_issue(self):
        """Топ смены — тоже ошибки, а не категории."""
        data = self._data([
            {"issue_type": "设备Equipment", "raw_fault": "Unable to drive",
             "error_robot": "3780", "solving_time": 4, "recovery_title": "Restart"},
            {"equipment_category": "robot", "raw_template_title": "Chassis",
             "reported_error": "Collision", "error_robot": "3780",
             "solving_time": 4, "recovery_title": "Restart"},
        ])

        types = dict(data["types"])

        self.assertEqual(types.get("Unable to drive"), 1)
        self.assertEqual(types.get("Collision"), 1)
        self.assertNotIn("设备Equipment", types)
        self.assertNotIn("robot", types)

    def test_unassigned_and_gaps_are_counted(self):
        """Отчёт обязан показать незаполненное, а не молчать о нём.

        Проверено на живой смене 14.10 day: 56 записей из 121 (46 %) были без
        «что сделано». Отчёт об этом не сообщал, и незакрытая половина смены
        выглядела как обычные строки.
        """
        data = self._data([
            {"error_robot": None, "raw_fault": "X", "solving_time": None,
             "recovery_title": None},
            {"error_robot": "3780", "raw_fault": "X", "solving_time": 4,
             "recovery_title": "Restart"},
        ])

        self.assertEqual(data["unassigned"], 1)
        self.assertEqual(data["no_recovery"], 1)
        self.assertEqual(data["no_solving_time"], 1)

    def test_zero_solving_time_is_not_a_measurement(self):
        """`0` минут — это «неизвестно», а не «решили мгновенно».

        Иначе сумма «Booked repair time» считала бы нули измерениями и
        занижала картину.
        """
        data = self._data([
            {"error_robot": "1", "raw_fault": "X", "solving_time": 0,
             "recovery_title": "R"},
        ])

        self.assertEqual(data["downtime_minutes"], 0)
        self.assertEqual(data["no_solving_time"], 1)

    def test_zones_are_counted(self):
        """Зона есть в выборке, но в отчёте её не было вовсе."""
        data = self._data([
            {"error_robot": "1", "raw_fault": "X", "issue_warehouse": "E",
             "solving_time": 4, "recovery_title": "R"},
            {"error_robot": "2", "raw_fault": "X", "issue_warehouse": "D",
             "solving_time": 4, "recovery_title": "R"},
            {"error_robot": "3", "raw_fault": "X", "issue_warehouse": "E",
             "solving_time": 4, "recovery_title": "R"},
        ])

        self.assertEqual(data["zones"], {"E": 2, "D": 1})

    def test_repair_time_is_not_called_downtime(self):
        """⚠️ Подпись менялась осознанно — стерегу, чтобы не вернули обратно.

        Это сумма справочного `solving_time`, а не время простоя: по смене
        14.10 day 340 минут = 50×4 + 14×6 + 7×2. Настоящий простой считает
        `downtime_line` (пары Offline→Online).
        """
        metrics = {
            "total": 1, "robots": {"1": 1}, "employees": {"A": 1},
            "downtime_minutes": 4, "downtime": {"available": False},
            "maintenance": [], "types": {"X": 1},
            "previous": {"date": None, "shift": None, "total": None},
            "delta": None, "warehouse": "GLP-C", "zones": {"C2": 1},
            "unassigned": 0, "no_recovery": 0, "no_solving_time": 0,
        }

        text = shift_report.build_shift_summary("2026-10-14", "night", metrics)

        self.assertIn("Booked repair time", text)
        self.assertNotIn("Downtime 4m", text)


if __name__ == "__main__":
    unittest.main()


class StillOfflineChecks(unittest.TestCase):
    """
    🔴 **Отчёт обязан сказать, сколько роботов стоит ПРЯМО СЕЙЧАС.**

    Аудит 14.10.2026: в офлайне было **7 роботов**, самый старый — с 18.04.2026
    (**179 суток**), ещё три — с 07–09.10. Сменный рапорт об этом молчал
    полностью, а MTTR в тех же сменах печатал «no robot came back online»:
    читатель видел «всё спокойно», хотя часть парка стоит неделями.

    Проверяется смысл: незакрытый интервал обязан попасть в отчёт, а закрытый —
    нет (иначе список врал бы «стоит» про робота, который уже на линии).
    """

    def _offline(self, rows):
        with mock.patch.object(shift_report, "rest_get", return_value=rows):
            return shift_report.still_offline("GLP-C")

    def test_open_interval_is_reported(self):
        """Робот ушёл в офлайн и не вернулся — он «стоит сейчас»."""
        robots = self._offline([
            {"robot_number": "3742", "new_status": "离线 | Offline",
             "created_at": "2026-10-07T10:00:00+00:00", "type_problem": "Abnormal walking"},
        ])

        self.assertEqual(len(robots), 1)
        self.assertEqual(robots[0]["robot"], "3742")
        self.assertGreater(robots[0]["hours"], 24 * 6)

    def test_closed_interval_is_not_reported(self):
        """Робот вернулся на линию — в списке «стоит сейчас» его быть не должно."""
        robots = self._offline([
            {"robot_number": "3422", "new_status": "离线 | Offline",
             "created_at": "2026-10-08T08:00:00+00:00", "type_problem": None},
            {"robot_number": "3422", "new_status": "在线 | Online",
             "created_at": "2026-10-08T16:25:00+00:00", "type_problem": None},
        ])

        self.assertEqual(robots, [])

    def test_oldest_is_first(self):
        """Самый долгий простой — первым: это то, что надо чинить."""
        # Статусы — **точные строки из справочника** (`robot_status`), а не
        # сокращения: проверка идёт сравнением с константой, и «Offline» её не
        # прошло бы (на этом мой первый вариант теста и упал).
        import robot_status

        robots = self._offline([
            {"robot_number": "1", "new_status": robot_status.OFFLINE,
             "created_at": "2026-10-13T10:00:00+00:00", "type_problem": None},
            {"robot_number": "2", "new_status": robot_status.OFFLINE,
             "created_at": "2026-04-18T10:00:00+00:00", "type_problem": None},
        ])

        self.assertEqual([item["robot"] for item in robots], ["2", "1"])

    def test_read_failure_is_not_a_zero(self):
        """Сбой чтения — `None`, а не пустой список.

        Различие важное: пустой список печатает «все на линии», а сбой обязан
        молчать, иначе отчёт соврёт о спокойствии.
        """
        self.assertIsNone(self._offline(None))
