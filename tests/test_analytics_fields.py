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

    Аудит 15.10.2026 нашёл здесь **второй дефект, глубже первого**: источником
    был журнал `change_status_robots`, и он давал роботов, которых в офлайне
    **уже нет**, пропуская тех, кто стоит. Сверка показала **ни одного общего**
    робота между журналом и живой таблицей:

    | Источник | Офлайн |
    |---|---|
    | журнал (последняя запись 09.10) | 115, 3742, 3694, 3587, 3773, 3628 |
    | `equipment` (живой статус) | 2627, 5138, 7449, 3511 |

    И журнал **путал склады**: `#115` (простой 179 дней) числился за GLP-C, а он
    `SMALL-P3` — зона `D`. Проверяется поэтому не только «есть ли робот в
    списке», но и правильный источник с правильным складом.
    """

    def _offline(self, rows):
        with mock.patch.object(shift_report, "rest_get", return_value=rows):
            return shift_report.still_offline("GLP-C")

    def test_offline_robot_is_reported(self):
        """Робот со статусом `offline` попадает в список."""
        robots = self._offline([
            {"equipment_code": "3742", "status": "offline",
             "updated_at": "2026-10-07T10:00:00+00:00",
             "problem_note": "Abnormal walking"},
        ])

        self.assertEqual(len(robots), 1)
        self.assertEqual(robots[0]["robot"], "3742")
        self.assertGreater(robots[0]["hours"], 24 * 6)

    def test_query_asks_for_offline_and_the_right_warehouse(self):
        """
        🔴 **Запрос идёт в `equipment` по НАЗВАНИЮ склада.**

        Две ловушки сразу: `equipment.warehouse` хранит **название** (сравнение с
        числовым id вернуло бы пусто — «все на линии» вместо правды), а статус
        нужен канонический `offline` (не локализованный `离线 | Offline`).
        """
        captured = {}

        def fake_get(table, params=None, optional=False):
            captured["table"] = table
            captured["params"] = params or {}
            return []

        with mock.patch.object(shift_report, "rest_get", side_effect=fake_get):
            shift_report.still_offline("SMALL-P3")

        self.assertEqual(captured["table"], "equipment")
        self.assertEqual(captured["params"].get("status"), "eq.offline")
        self.assertEqual(captured["params"].get("warehouse"), "eq.SMALL-P3")
        self.assertEqual(captured["params"].get("category"), "eq.robot")

    def test_oldest_is_first(self):
        """Самый долгий простой — первым: это то, что надо чинить."""
        robots = self._offline([
            {"equipment_code": "1", "status": "offline",
             "updated_at": "2026-10-13T10:00:00+00:00"},
            {"equipment_code": "2", "status": "offline",
             "updated_at": "2026-04-18T10:00:00+00:00"},
        ])

        self.assertEqual([item["robot"] for item in robots], ["2", "1"])

    def test_read_failure_is_not_a_zero(self):
        """Сбой чтения — `None`, а не пустой список.

        Различие важное: пустой список печатает «все на линии», а сбой обязан
        молчать, иначе отчёт соврёт о спокойствии.
        """
        with mock.patch.object(shift_report, "rest_get", return_value=None):
            self.assertIsNone(shift_report.still_offline("GLP-C"))


class WarehouseReportChecks(unittest.TestCase):
    """
    🔴 **Каждый склад отчитывается своим отчётом и своим хуком.**

    Аудит 14.10.2026: ошибки уходили в Lark по складам (`LARK_HOOK_ERROR_SP3`),
    а **сменный рапорт всегда считался по GLP-C** — `send_shift_report` звал
    `shift_metrics(date, shift)` без склада. За смену 14.10 ночь это **7 записей
    SMALL-P3**, которых группа не видела.

    Отдельная опасность — маркер «уже отправлено»: одно имя файла на смену
    означало бы, что после отчёта GLP-C отчёт SMALL-P3 отброшен как дубль.
    """

    def test_marker_is_different_per_warehouse(self):
        """У каждой смены каждого склада — свой маркер."""
        glp = shift_report.report_marker_name("2026-10-14", "night", "GLP-C")
        sp3 = shift_report.report_marker_name("2026-10-14", "night", "SMALL-P3")

        self.assertNotEqual(glp, sp3, "маркер общий на два склада — второй отчёт потеряется")
        self.assertIn("glp-c", glp)
        self.assertIn("small-p3", sp3)

    def test_marker_without_warehouse_stays_the_old_name(self):
        """Имя без склада не изменилось — старые маркеры читаются."""
        self.assertEqual(
            shift_report.report_marker_name("2026-10-14", "night"),
            "2026-10-14-night.json",
        )

    def test_report_goes_over_every_warehouse(self):
        """`send_shift_reports` обходит все склады, и сбой одного не мешает другому."""
        calls = []

        def fake_send(date, shift, force=False, warehouse=None):
            calls.append(warehouse)

            if warehouse == "GLP-C":
                raise RuntimeError("склад недоступен")

            return {"ok": True}

        with mock.patch.object(
            shift_report, "send_shift_report", side_effect=fake_send
        ):
            results = shift_report.send_shift_reports("2026-10-14", "night")

        # Оба склада вызваны, несмотря на сбой первого.
        self.assertIn("GLP-C", calls)
        self.assertIn("SMALL-P3", calls)
        # Сбой одного виден в результате, а не проглочен.
        self.assertEqual(results["GLP-C"].get("reason"), "error")
        self.assertTrue(results["SMALL-P3"].get("ok"))

    def test_sent_ok_requires_every_warehouse(self):
        """Смена закрыта, только если по КАЖДОМУ складу отчёт ушёл."""
        good = {"GLP-C": {"code": 0}, "SMALL-P3": {"code": 0}}
        mixed = {"GLP-C": {"code": 0}, "SMALL-P3": {"skipped": True, "reason": "db-error"}}

        self.assertTrue(shift_report._report_sent_ok(good))
        self.assertFalse(shift_report._report_sent_ok(mixed))


class MaintenanceMeaningChecks(unittest.TestCase):
    """
    🔴 **«Обслуживание» — это НЕЗАКРЫТЫЕ ошибки, а не частые.**

    Дефект 14.10.2026: порог считал все записи робота за смену. У робота #3808
    их было 16, из них **14 (87 %)** закрыты за ≤5 минут — он работал, а не
    требовал ремонта. Порог срабатывал почти всегда (**15 роботов** за смену), и
    из-за этого карточка **всегда была красной**: сигнал «нужен ремонт» перестал
    что-либо значить.
    """

    def _data(self, rows):
        with mock.patch(
            "sendToDataBase.get_shift_exceptions", return_value=rows
        ):
            import sendToDataBase

            return sendToDataBase.shift_report_data(
                "2026-10-14", "night", warehouse="GLP-C"
            )

    def test_closed_errors_do_not_ask_for_maintenance(self):
        """Пять закрытых ошибок — робот работает, а не «требует обслуживания»."""
        data = self._data([
            {"error_robot": "3808", "raw_fault": "X", "solving_time": 4,
             "recovery_title": "Restart"}
            for _ in range(5)
        ])

        self.assertEqual(data["maintenance"], [])

    def test_unresolved_errors_ask_for_maintenance(self):
        """Три незакрытые — вот это повод для ремонта."""
        data = self._data([
            {"error_robot": "3808", "raw_fault": "X", "solving_time": None,
             "recovery_title": None}
            for _ in range(3)
        ])

        self.assertEqual(data["maintenance"], [("3808", 3)])

    def test_zero_means_unknown_not_solved(self):
        """`solving_time = 0` — «неизвестно», а не «решено мгновенно».

        Поэтому такая ошибка считается незакрытой (см. `_solving_time`).
        """
        data = self._data([
            {"error_robot": "1", "raw_fault": "X", "solving_time": 0,
             "recovery_title": None}
            for _ in range(3)
        ])

        self.assertEqual(data["maintenance"], [("1", 3)])


class ReportColorChecks(unittest.TestCase):
    """
    🔴 **Цвет карточки различает смены, а не горит красным всегда.**

    Проверено на живых данных 14.10.2026: **4 смены подряд были `red`**. Причин
    было две: порог обслуживания ловил «частые» ошибки (см. выше), а
    `REPORT_WARN_TOTAL = 5` не различал ничего при 100–200 записях за смену.

    Новая шкала — по **доле незакрытых**: 200 мелких ошибок, закрытых за минуты,
    это нормальная работа, а 20 ошибок, из которых 15 висят, — тревога.
    """

    def _metrics(self, **over):
        base = {
            "total": 100, "robots": {}, "employees": {}, "downtime_minutes": 0,
            "downtime": {"available": False}, "maintenance": [], "types": {},
            "previous": {"date": None, "shift": None, "total": None}, "delta": None,
            "warehouse": "GLP-C", "zones": {}, "unassigned": 0,
            "no_recovery": 0, "no_solving_time": 0,
        }
        base.update(over)

        return base

    def test_empty_shift_is_green(self):
        self.assertEqual(shift_report.report_color(self._metrics(total=0)), "green")

    def test_busy_but_closed_shift_is_green(self):
        """200 ошибок, из которых 2 не закрыты — работа идёт нормально."""
        color = shift_report.report_color(
            self._metrics(total=200, no_recovery=2)
        )

        self.assertEqual(color, "green")

    def test_many_unresolved_is_orange(self):
        """Больше четверти смены без «что сделано» — предупреждение."""
        color = shift_report.report_color(
            self._metrics(total=100, no_recovery=40)
        )

        self.assertEqual(color, "orange")

    def test_maintenance_is_red(self):
        """Робот копит незакрытое — это ремонт."""
        color = shift_report.report_color(
            self._metrics(total=100, no_recovery=2, maintenance=[("3808", 3)])
        )

        self.assertEqual(color, "red")


class RepairTimeLineChecks(unittest.TestCase):
    """
    🔴 **Нет цифры — назови причину, а не выдумывай спокойствие.**

    Аудит 15.10.2026: строка «MTTR: no robot came back online this shift»
    **вводила в заблуждение** — читается как «роботов не было в ремонте», а
    означает «измерить нечем»: журнал `change_status_robots` не наполняется с
    **09.10.2026** (проверено: последняя запись 09.10 21:25, всего 175).

    Проверено, что **измеренного** источника нет:

    * `equipment` хранит только текущий статус и время его изменения — момента
      перехода в офлайн нет, начало ремонта неизвестно;
    * `exceptions.end_time` = `start_time` + справочное время заготовки
      (сходится в 99.99 % строк) — это **оценка**, а не факт.

    Поэтому проверяется, что отчёт называет причину **с датой**, а не молчит и
    не обещает ложного «всё спокойно».
    """

    def _metrics(self, downtime):
        return {
            "total": 10, "robots": {}, "employees": {}, "downtime_minutes": 0,
            "downtime": downtime, "maintenance": [], "types": {},
            "previous": {"date": None, "shift": None, "total": None}, "delta": None,
            "warehouse": "GLP-C", "zones": {}, "unassigned": 0,
            "no_recovery": 0, "no_solving_time": 0,
        }

    def test_no_measured_data_names_the_reason(self):
        """Пустой журнал — говорим «не измеримо» и называем дату последней записи."""
        with mock.patch.object(
            shift_report, "_journal_last_entry", return_value="09.10.2026"
        ):
            line = shift_report.downtime_line(
                self._metrics({"available": True, "count": 0})
            )

        self.assertIn("not measurable", line)
        self.assertIn("09.10.2026", line)
        # 🔴 Прежняя формулировка запрещена: она читается как «всё спокойно».
        self.assertNotIn("no robot came back online", line)

    def test_unreadable_journal_does_not_invent_a_date(self):
        """Сбой чтения журнала — без выдуманной даты."""
        with mock.patch.object(shift_report, "_journal_last_entry", return_value=""):
            line = shift_report.downtime_line(
                self._metrics({"available": True, "count": 0})
            )

        self.assertIn("not measurable", line)
        self.assertIn("empty", line)

    def test_measured_repairs_still_show_mttr(self):
        """Когда данные ЕСТЬ, считается как раньше — MTTR и самый долгий."""
        line = shift_report.downtime_line(
            self._metrics({
                "available": True, "count": 2, "mttr_seconds": 3600,
                "longest": {"robot": "3422", "seconds": 7200},
            })
        )

        self.assertIn("MTTR 1h 00m", line)
        self.assertIn("#3422", line)
