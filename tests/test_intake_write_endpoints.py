# -*- coding: utf-8 -*-
"""
Тесты ручек записи дерева приёма (`/intake/node/position`, `/intake/option`).

Это **первые ручки бота, которые меняют поведение приёма**, поэтому проверяется
не только «работает», но и «не даёт сделать опасное»:

1. **Секрет обязателен и общий с чтением.** Пустой токен закрывает ручки (404),
   чужой — 403. Иначе забытая переменная окружения позволила бы менять дерево
   кому угодно, и операторы пошли бы не туда.
2. **Встроенный вариант нельзя удалить.** Он живёт в коде: удаление строки
   вернуло бы кнопку к исходному виду, то есть выглядело бы как «удаление не
   работает». Правильное действие — скрыть.
3. **Неизвестный узел отвергается.** Иначе появилась бы строка-сирота, которую
   веб не покажет, а база накопит.
4. **Перетаскивание не затирает другие поля узла** — проверяется на уровне
   `storage`, потому что это дефект, который в `update_node` уже был.

🔴 **База не трогается.** Все записи в `storage` подменены: тест, который ходит
в живую базу, при повторном прогоне создал бы мусорные кнопки в рабочем дереве.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from equipment_intake import storage, tree_config, tree_view  # noqa: E402

TOKEN = "test-intake-token"


@pytest.fixture
def client(monkeypatch):
    """Клиент Flask с включённым секретом и подменёнными записями."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:ABC")
    import telegram_bot

    monkeypatch.setattr(telegram_bot, "INTAKE_VIEW_TOKEN", TOKEN, raising=False)
    telegram_bot.app.config["TESTING"] = True

    return telegram_bot.app.test_client()


@pytest.fixture
def auth():
    return {"X-Intake-View-Token": TOKEN}


def _payload_with_node(node_id: str):
    """Дерево, где у узла есть один вариант — для проверок удаления."""
    return tree_view.build_tree_payload(
        base=tree_config.DEFAULT_TREE,
        option_rows=[
            {
                "node_id": node_id,
                "option_id": "builtin_one",
                "label": "Builtin one",
                "next_node": "ask_cause",
                "is_builtin": True,
                "hidden": False,
                "sort_order": 10,
            }
        ],
    )


# ============================================================
# ДОСТУП
# ============================================================

def test_write_endpoints_require_the_token(client, auth):
    """Без секрета — отказ; с секретом — запрос доходит до логики."""
    for path, body in (
        ("/intake/node/position", {"node_id": "root", "x": 1, "y": 2}),
        ("/intake/option", {"action": "hide", "node_id": "root", "option_id": "robot"}),
    ):
        assert client.post(path, json=body).status_code == 403, path
        assert (
            client.post(path, json=body, headers={"X-Intake-View-Token": "wrong"}).status_code
            == 403
        ), path


def test_empty_token_disables_write_endpoints(client, monkeypatch):
    """
    Пустой токен — 404, а не «открыто всем».

    🔴 Это главная проверка файла. Ручки меняют дерево приёма: если забытая
    переменная окружения открывала бы их, любой, кто знает адрес, мог бы
    перенаправить операторов в другую ветку.
    """
    import telegram_bot

    monkeypatch.setattr(telegram_bot, "INTAKE_VIEW_TOKEN", "", raising=False)

    for path, body in (
        ("/intake/node/position", {"node_id": "root", "x": 1, "y": 2}),
        ("/intake/option", {"action": "hide", "node_id": "root", "option_id": "robot"}),
    ):
        assert client.post(path, json=body).status_code == 404, path


# ============================================================
# ВАЛИДАЦИЯ
# ============================================================

def test_position_requires_node_id(client, auth):
    assert client.post("/intake/node/position", json={"x": 1, "y": 2}, headers=auth).status_code == 400
    assert (
        client.post(
            "/intake/node/position", json={"node_id": "  ", "x": 1, "y": 2}, headers=auth
        ).status_code
        == 400
    )


def test_unknown_node_is_rejected(client, auth):
    """Опечатка в id не должна создавать строку-сироту."""
    response = client.post(
        "/intake/node/position",
        json={"node_id": "no_such_node", "x": 1, "y": 2},
        headers=auth,
    )

    assert response.status_code == 400
    assert "unknown node" in response.get_json()["error"]


def test_option_rejects_unknown_node_and_action(client, auth):
    assert (
        client.post(
            "/intake/option",
            json={"action": "hide", "node_id": "nope", "option_id": "x"},
            headers=auth,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/intake/option", json={"action": "zzz", "node_id": "root"}, headers=auth
        ).status_code
        == 400
    )


def test_option_requires_ids(client, auth):
    # Без node_id — отказ.
    assert (
        client.post("/intake/option", json={"action": "hide"}, headers=auth).status_code
        == 400
    )
    # Без option_id для действий над вариантом — отказ.
    assert (
        client.post(
            "/intake/option", json={"action": "hide", "node_id": "root"}, headers=auth
        ).status_code
        == 400
    )


def test_add_requires_a_label(client, auth):
    """
    Безымянную кнопку создать нельзя.

    В дереве появился бы пункт, который оператор не сможет прочитать, — а он
    выбирает по тексту, что случилось.
    """
    response = client.post(
        "/intake/option",
        json={"action": "add", "node_id": "robot_errors_chassis", "label": "   "},
        headers=auth,
    )

    assert response.status_code == 400
    assert "label" in response.get_json()["error"]


# ============================================================
# ЗАПИСЬ (storage подменён — база не трогается)
# ============================================================

def test_position_is_saved_for_a_known_node(client, auth, monkeypatch):
    """Координаты доходят до `storage` ровно как переданы."""
    seen = {}

    def fake_update(node_id, x, y, **kwargs):
        seen.update({"node_id": node_id, "x": x, "y": y})
        return True

    monkeypatch.setattr(storage, "update_node_position", fake_update)

    response = client.post(
        "/intake/node/position", json={"node_id": "root", "x": 12.5, "y": -8}, headers=auth
    )

    assert response.status_code == 200
    assert seen == {"node_id": "root", "x": 12.5, "y": -8}


def test_position_failure_names_the_reason(client, auth, monkeypatch):
    """
    Отказ называет причину: «узел неизвестен» и «миграция не применена» —
    разные действия для человека.

    Первое лечится исправлением id, второе — применением `0114`. Один код
    ответа на оба случая заставлял бы гадать.
    """
    monkeypatch.setattr(storage, "update_node_position", lambda *a, **k: False)
    monkeypatch.setattr(storage, "position_supported", lambda: False)

    response = client.post(
        "/intake/node/position", json={"node_id": "root", "x": 1, "y": 2}, headers=auth
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "positions unsupported"
    assert response.get_json()["position_supported"] is False


def test_add_option_links_the_template(client, auth, monkeypatch):
    """
    Добавление кнопки со связью: сначала создание, затем связь.

    Двумя шагами намеренно: `add_option` пишет только колонки v1, чтобы
    работать и до миграции `0109`. Поэтому неудачная связь не отменяет
    создания — кнопку всё равно видно, и связь поставят позже.
    """
    linked = {}

    monkeypatch.setattr(storage, "add_option", lambda *a, **k: "new_error")
    monkeypatch.setattr(
        storage,
        "link_option_to_template",
        lambda node_id, option_id, template_id, **k: linked.update(
            {"node_id": node_id, "option_id": option_id, "template_id": template_id}
        )
        or True,
    )

    response = client.post(
        "/intake/option",
        json={
            "action": "add",
            "node_id": "robot_errors_chassis",
            "label": "New error",
            "template_id": 152,
        },
        headers=auth,
    )

    assert response.status_code == 200
    assert response.get_json()["option_id"] == "new_error"
    assert linked == {
        "node_id": "robot_errors_chassis",
        "option_id": "new_error",
        "template_id": 152,
    }


def test_add_without_template_does_not_link(client, auth, monkeypatch):
    """Кнопка без заготовки создаётся: связь можно поставить позже."""
    called = {"link": False}

    monkeypatch.setattr(storage, "add_option", lambda *a, **k: "no_tpl")
    monkeypatch.setattr(
        storage, "link_option_to_template", lambda *a, **k: called.update({"link": True})
    )

    response = client.post(
        "/intake/option",
        json={"action": "add", "node_id": "robot_errors_chassis", "label": "No template"},
        headers=auth,
    )

    assert response.status_code == 200
    assert called["link"] is False


def test_builtin_option_can_now_be_deleted(client, auth, monkeypatch):
    """
    🔴 Встроенную кнопку **можно** удалить — требование владельца 13.10.2026.

    Дословно: «нужно чтобы полностью мог менять удалять кнопки все и
    переподвязывать их». Раньше ручка отвечала «builtin option cannot be
    deleted», и это было ограничением, а не защитой: определение кнопки лежит в
    коде бота, но пометка `deleted` (`0115`) убирает её из дерева так же, как
    удаление.

    Проверяем, что запрета больше нет и вызов **доходит** до `storage`: причину
    отказа называет он, и она одна — «последняя кнопка узла».
    """
    called = {}

    monkeypatch.setattr(
        storage,
        "delete_option",
        lambda node_id, option_id, **k: called.update(
            {"node": node_id, "opt": option_id}
        )
        or True,
    )

    response = client.post(
        "/intake/option",
        json={
            "action": "delete",
            "node_id": "robot_errors_chassis",
            "option_id": "chassis_went_off_road",
        },
        headers=auth,
    )

    assert response.status_code == 200, response.text
    assert called == {"node": "robot_errors_chassis", "opt": "chassis_went_off_road"}


def test_last_visible_button_cannot_be_deleted(client, auth, monkeypatch):
    """
    🔴 Последнюю видимую кнопку узла удалить нельзя — это был бы тупик.

    Узел-выбор без вариантов показывает оператору только «Cancel»: ошибку
    записать нечем. То же ограничение проверяет валидатор дерева в боте
    (`types.py::validate` отвергает `choice` без вариантов), поэтому отказ обязан
    приходить от `storage`, а не быть выдумкой ручки.
    """
    monkeypatch.setattr(
        storage,
        "delete_option",
        lambda node_id, option_id, **k: False,
    )

    response = client.post(
        "/intake/option",
        json={
            "action": "delete",
            "node_id": "robot_errors_chassis",
            "option_id": "chassis_went_off_road",
        },
        headers=auth,
    )

    assert response.status_code == 400
    assert "last visible button" in response.get_json()["error"]
    # Подсказка говорит, что делать: сначала добавить другую кнопку.
    assert "add another button" in response.get_json()["hint"]


def test_restore_brings_a_deleted_button_back(client, auth, monkeypatch):
    """
    Удаление встроенной кнопки обратимо: `restore` возвращает её.

    Существует потому, что удаление встроенной — **пометка**, а не потеря.
    Человек, удаливший кнопку по ошибке, должен смочь её вернуть.
    """
    called = {}
    monkeypatch.setattr(
        storage,
        "restore_option",
        lambda node_id, option_id, **k: called.update(
            {"node": node_id, "opt": option_id}
        )
        or True,
    )

    response = client.post(
        "/intake/option",
        json={
            "action": "restore",
            "node_id": "robot_errors_chassis",
            "option_id": "chassis_went_off_road",
        },
        headers=auth,
    )

    assert response.status_code == 200, response.text
    assert called == {"node": "robot_errors_chassis", "opt": "chassis_went_off_road"}


def test_added_option_can_be_deleted(client, auth, monkeypatch):
    """Добавленный вариант удаляется — он живёт только в базе."""
    monkeypatch.setattr(storage, "load_option_rows", lambda: [])
    monkeypatch.setattr(
        storage,
        "effective_options",
        lambda base, node_id, rows: [
            {
                "id": "added_one",
                "label": "Added",
                "next_node": "ask_cause",
                "description": "",
                "icon": "",
                "only_for": [],
                "is_builtin": False,
                "hidden": False,
                "kind": "added",
                "template_id": None,
                "index": 0,
            }
        ],
    )

    called = {}
    monkeypatch.setattr(
        storage,
        "delete_option",
        lambda node_id, option_id, **k: called.update({"node": node_id, "opt": option_id})
        or True,
    )

    response = client.post(
        "/intake/option",
        json={"action": "delete", "node_id": "robot_errors_chassis", "option_id": "added_one"},
        headers=auth,
    )

    assert response.status_code == 200
    assert called == {"node": "robot_errors_chassis", "opt": "added_one"}


def test_hide_and_move_and_link_reach_storage(client, auth, monkeypatch):
    """Три действия над вариантом доходят до своих функций."""
    seen = {}

    monkeypatch.setattr(
        storage,
        "set_option_hidden",
        lambda node_id, option_id, hidden, **k: seen.update(
            {"hide": (node_id, option_id, hidden)}
        )
        or True,
    )
    monkeypatch.setattr(
        storage,
        "move_option",
        lambda node_id, option_id, direction, **k: seen.update(
            {"move": (node_id, option_id, direction)}
        )
        or True,
    )
    monkeypatch.setattr(
        storage,
        "link_option_to_template",
        lambda node_id, option_id, template_id, **k: seen.update(
            {"link": (node_id, option_id, template_id)}
        )
        or True,
    )

    base = {"node_id": "robot_errors_chassis", "option_id": "x"}

    assert client.post("/intake/option", json={**base, "action": "hide", "hidden": True}, headers=auth).status_code == 200
    assert client.post("/intake/option", json={**base, "action": "move", "direction": "up"}, headers=auth).status_code == 200
    assert client.post("/intake/option", json={**base, "action": "link", "template_id": 7}, headers=auth).status_code == 200

    assert seen["hide"] == ("robot_errors_chassis", "x", True)
    assert seen["move"] == ("robot_errors_chassis", "x", "up")
    assert seen["link"] == ("robot_errors_chassis", "x", 7)


def test_storage_failure_is_503_not_silent_success(client, auth, monkeypatch):
    """
    Сбой записи — 503, а не «ok».

    Иначе веб показал бы «сохранено» на неудавшейся записи, и человек ушёл бы с
    мыслью, что дерево изменено.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(storage, "update_node_position", boom)

    response = client.post(
        "/intake/node/position", json={"node_id": "root", "x": 1, "y": 2}, headers=auth
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "save failed"
