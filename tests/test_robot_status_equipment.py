# -*- coding: utf-8 -*-
"""Команды статуса ищут робота в каноническом реестре `equipment`.

🔴 **Почему** (18.11.2026). Владелец: «почему когда я пишу /offline 9999 пишет,
что такого робота нету». Робот 9999 есть в `equipment` (GLP-C, online), но в
легаси-зеркале `robots_maintenance_list` его нет: там у GLP-C 52 строки из 2 191,
остальные заведены вручную. Команды смотрели только в легаси.

Проверки без сети: подменяем REST-хелперы.
"""

from __future__ import annotations

import robot_status as rs

EQUIPMENT = {
    "id": 62956,
    "equipment_code": "9999",
    "warehouse": "GLP-C",
    "category": "robot",
    "status": "online",
    "equipment_type_id": 7,
    "problem_type": None,
    "problem_note": None,
    "legacy_id": None,
    "legacy_table": "manual",
}


def test_find_robot_prefers_equipment(monkeypatch) -> None:
    calls = []

    def fake_get(table, params=None, **kwargs):
        calls.append((table, params))
        return [dict(EQUIPMENT)] if table == "equipment" else None

    monkeypatch.setattr(rs, "rest_get", fake_get)
    monkeypatch.setattr(rs, "_type_name", lambda type_id: "RT_KUBOT_C2")

    robot = rs.find_robot("9999", "GLP-C")

    assert robot is not None
    assert robot["id"] == 62956
    assert robot["_source"] == "equipment"
    assert robot["robot_number"] == "9999"
    assert robot["status"] == rs.ONLINE  # подпись, как ждут старые экраны
    assert robot["robot_type"] == "RT_KUBOT_C2"
    assert calls[0][0] == "equipment"


def test_find_robot_falls_back_to_legacy(monkeypatch) -> None:
    def fake_get(table, params=None, **kwargs):
        if table == "equipment":
            return []
        return [{"id": 3542, "robot_number": 3783, "warehouse": "GLP-C",
                 "status": rs.ONLINE}]

    monkeypatch.setattr(rs, "rest_get", fake_get)

    robot = rs.find_robot("3783", "GLP-C")

    assert robot["id"] == 3542
    assert robot["_source"] == "legacy"


def test_find_robot_by_id_resolves_equipment_and_old_legacy_ids(monkeypatch) -> None:
    def fake_get(table, params=None, **kwargs):
        if table != "equipment":
            return None
        if params and params.get("id") == "eq.62956":
            return [dict(EQUIPMENT)]
        if params and params.get("legacy_id") == "eq.3542":
            return [dict(EQUIPMENT, id=777)]
        return []

    monkeypatch.setattr(rs, "rest_get", fake_get)
    monkeypatch.setattr(rs, "_type_name", lambda type_id: "RT_KUBOT_C2")

    assert rs.find_robot_by_id(62956)["id"] == 62956
    # Старая кнопка (до 18.11) несла id легаси-строки.
    assert rs.find_robot_by_id(3542)["id"] == 777


def test_change_status_writes_equipment_with_canonical_status(monkeypatch) -> None:
    patches = []
    posts = []

    monkeypatch.setattr(
        rs, "rest_patch",
        lambda table, params, payload: (patches.append((table, params, payload)), [{}])[1],
    )
    monkeypatch.setattr(
        rs, "rest_post",
        lambda table, payload: (posts.append((table, payload)), [{}])[1],
    )
    monkeypatch.setattr(
        rs, "find_equipment",
        lambda robot: {"id": 62956, "equipment_code": "9999", "warehouse": "GLP-C"},
    )
    monkeypatch.setattr(rs, "open_repair", lambda *a, **k: 4242)

    robot = {
        "id": 62956,
        "robot_number": "9999",
        "status": rs.ONLINE,
        "warehouse": "GLP-C",
        "_source": "equipment",
        "_canonical_status": "online",
    }

    result = rs.change_robot_status(
        robot, "offline", "Other", "сломался", {"card_id": 1, "user_name": "T"}
    )

    assert result is not None
    assert patches and patches[0][0] == "equipment"
    assert patches[0][2]["status"] == "maintenance"
    assert patches[0][2]["problem_type"] == "Other"
    assert result["new_status"] == rs.MAINTENANCE
    # Журнал: robot_id — каноническая единица.
    history = next(item for item in posts if item[0] == rs.STATUS_HISTORY_TABLE)
    assert history[1]["robot_id"] == 62956


def test_change_status_keeps_legacy_table_for_legacy_rows(monkeypatch) -> None:
    patches = []

    monkeypatch.setattr(
        rs, "rest_patch",
        lambda table, params, payload: (patches.append((table, params, payload)), [{}])[1],
    )
    monkeypatch.setattr(rs, "rest_post", lambda *a, **k: [{}])
    monkeypatch.setattr(
        rs, "find_equipment",
        lambda robot: {"id": 777, "equipment_code": "3783", "warehouse": "GLP-C"},
    )
    monkeypatch.setattr(rs, "open_repair", lambda *a, **k: None)

    robot = {
        "id": 3542,
        "robot_number": 3783,
        "status": rs.ONLINE,
        "warehouse": "GLP-C",
        "_source": "legacy",
    }

    rs.change_robot_status(robot, "offline", "Other", "note", {"card_id": 1})

    assert patches[0][0] == rs.ROBOTS_TABLE
    assert patches[0][2]["status"] == rs.MAINTENANCE
