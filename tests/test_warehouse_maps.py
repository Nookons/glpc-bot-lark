"""Сторож: карты складов бота не отстают от списка складов.

Зачем отдельный файл. Склады задаются в двух местах, и они уже расходились:

* `warehouses.WAREHOUSES` — что бот **обслуживает** (топики, команды, отчёты);
* `journal.WAREHOUSE_IDS` / `journal.KNOWN_ZONES` — как это читается из
  `exceptions` (`warehouse` — числовой id, `zone` — код зоны).

29.10.2026 в списке появился `GLP-C1`, а в карту чтения его не добавили. Бот
писал записи C1 (динамический `sendToDataBase.warehouse_id`), но
`get_shift_exceptions` отвечал «неизвестный склад», и **ни один сменный отчёт по
GLP-C1 не ушёл** — при этом в логах это выглядело как «нет данных смены».
Заметить можно было только по отсутствию маркеров `*-glp-c1.json`.

Тест статический (без сети и базы): сверяет карты с конфигурацией бота. Значения
id проверены по живой базе 07.11.2026.
"""

from __future__ import annotations

import unittest

from warehouses import WAREHOUSES
from journal import KNOWN_ZONES, WAREHOUSE_IDS, allowed_zone, warehouse_id
from equipment_intake import report_writer


class WarehouseMapsTest(unittest.TestCase):
    def test_every_served_warehouse_can_be_resolved_to_id(self) -> None:
        """Каждый склад бота обязан иметь id для чтения `exceptions`."""
        missing = [
            title for title in WAREHOUSES.values()
            if WAREHOUSE_IDS.get(title) is None
        ]

        self.assertEqual(
            missing, [],
            "склад есть в WAREHOUSES, но не в journal.WAREHOUSE_IDS: "
            "чтения по нему откажут «неизвестный склад»",
        )

    def test_known_ids_match_reference(self) -> None:
        """id складов — из справочника `warehouses` (проверено 07.11.2026)."""
        self.assertEqual(WAREHOUSE_IDS["GLP-C"], 1)
        self.assertEqual(WAREHOUSE_IDS["SMALL-P3"], 3)
        # 🔴 29.10.2026: GLP-C разделён на две очереди, у C1 свой id.
        self.assertEqual(WAREHOUSE_IDS["GLP-C1"], 7)

    def test_every_served_warehouse_has_zone_policy(self) -> None:
        """У каждого склада бота есть политика зон (пустая — тоже политика)."""
        for title in WAREHOUSES.values():
            self.assertIn(title, KNOWN_ZONES, f"нет политики зон для {title}")

    def test_c1_zone_is_known(self) -> None:
        """Зона C1 допустима для GLP-C1 — иначе записи теряют зону."""
        self.assertEqual(allowed_zone("GLP-C1", "C1"), "C1")
        # Чужая зона по-прежнему отвергается: догадка запрещена.
        self.assertIsNone(allowed_zone("GLP-C1", "C2"))
        self.assertEqual(warehouse_id("GLP-C1"), 7)

    def test_report_writer_reads_c1_zone_from_device(self) -> None:
        """`device_zone` обязан смотреть справочник и для GLP-C1."""
        self.assertIn("GLP-C1", report_writer.WAREHOUSES_WITH_ZONES)

        saved = report_writer.robot_row
        calls = []

        def fake_robot_row(number, warehouse=""):
            calls.append((str(number), warehouse))
            return {"id": 40388, "legacy_id": 20299, "sub_warehouse": "C1"}

        report_writer.robot_row = fake_robot_row
        try:
            self.assertEqual(report_writer.device_zone("GLP-C1", "2503"), "C1")
        finally:
            report_writer.robot_row = saved

        self.assertEqual(calls, [("2503", "GLP-C1")])


if __name__ == "__main__":
    unittest.main()
