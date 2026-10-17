"""
Осмотр оборудования из бота (`/inspection`): офлайн-проверки.

Ни сети, ни боевой базы: `SUPABASE_URL` указывает в никуда **до** импорта
модулей, а обращения к API и Telegram подменены. Забытая заглушка падает
громко, а не создаёт осмотр на оборудовании владельца.

**Что проверяется и почему именно это.**

| Проверка | Какой дефект она ловит |
| --- | --- |
| неверный номер | осмотр стартует на несуществующем устройстве |
| номер не той категории (робот как станция) | требование владельца «если станция, то только по станциям» |
| номер с чужого склада | номера повторяются между складами (206 случаев) |
| «не в работе» | человек тратит смену на осмотр списанного |
| отмена | диалог нельзя завершить, состояние остаётся |
| истечение TTL | диалог живёт вечно и мешает новому |
| проход по пунктам | фото не сохраняется / пункт не закрывается |
| кнопка Problem | «не так» записывается как «норма» |
| фото-пример и фото оператора удаляются | чат зарастает снимками |
| публичная ссылка | итог некуда отправить «для всех» |
| `/inspection` не в чужих топиках | бот отвечает там, где его нет |

Запуск:

    python3 -m pytest tests/test_inspection.py -q
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Изоляция — ДО импорта модулей бота: они читают окружение на импорте.
os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")
os.environ["TELEGRAM_TOPIC_ID"] = ""
os.environ["TELEGRAM_TOPIC_NAME"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""
for _key in ("GLPC", "SP3"):
    os.environ[f"TELEGRAM_TOPIC_ERROR_{_key}"] = ""
for _key in ("STATUS", "STATS", "SERVICE"):
    os.environ[f"TELEGRAM_TOPIC_{_key}"] = ""
os.environ["TELEGRAM_WRONG_TOPIC_HINT"] = ""

import pytest  # noqa: E402

import inspection as insp  # noqa: E402
import inspection_api  # noqa: E402
import telegram_api as tg  # noqa: E402
import telegram_bot as bot  # noqa: E402
from equipment_intake import validation  # noqa: E402


#: 🔴 **Сторож сети: ни один тест этого файла не имеет права выйти наружу.**
#
# **Почему он появился.** Первая версия тестов подменяла
# `inspection_api.list_templates` — но `inspection.py` импортирует функцию **по
# имени** (`from inspection_api import list_templates`), поэтому подмена не
# действовала, и 18 тестов **дошли до живого API владельца**. Запись не
# произошла только потому, что токен оказался отозван (401): то есть на месте
# дефекта изоляции был «повезло».
#
# Сторож стоит **ниже** уровня `requests`, поэтому его не обойдёт ни один
# незаглушённый путь: ни API осмотров, ни Supabase, ни Telegram. Любой такой
# вызов становится падением теста с объяснением, а не записью в живую базу.
def _install_network_guard(monkeypatch) -> None:
    import requests

    def guard(name):
        def wrapper(url, *args, **kwargs):
            raise AssertionError(
                f"Тест обратился наружу через {name}: {url}\n"
                "Вызов не заглушён. В живом прогоне он создал бы осмотр на "
                "оборудовании владельца — заглушите его."
            )

        return wrapper

    for name in ("request", "get", "post", "put", "patch", "delete"):
        if hasattr(requests, name):
            monkeypatch.setattr(requests, name, guard(f"requests.{name}"))


CHAT_ID = -500
USER_ID = 100
AUTH_ID = "df39b86e-57e2-46ba-97eb-a7e13c032bbd"

#: Тип устройства — «робот» во всех сценариях, если не сказано иное.
ROBOT = {
    "id": 40337,
    "equipment_code": "3780",
    "category": "robot",
    "warehouse": "GLP-C",
    "sub_warehouse": "C2",
    "status": "online",
    "equipment_type_id": 7,
}

#: Тот же номер, но зарядная станция — ровно тот случай, ради которого
#: категория обязательна (проверено на живой базе: в SMALL-P3 номер 13 — и
#: робот, и станция).
STATION_SAME_NUMBER = {
    "id": 999,
    "equipment_code": "3780",
    "category": "charging",
    "warehouse": "GLP-C",
    "sub_warehouse": "C2",
    "status": "online",
    "equipment_type_id": None,
}

TEMPLATES = [
    {
        "id": 1,
        "category": "robot",
        "name": "Robot daily inspection",
        "items": [
            {
                "id": 11,
                "position": 1,
                "code": "R-01",
                "title": "Chassis and wheels",
                "purpose": "A crack in the chassis grows into a breakdown.",
                "hint": "Look at the wheels",
                "requires_photo": True,
                "is_critical": True,
                "example_photo_url": "https://example.test/example-1.jpg",
            },
            {
                "id": 12,
                "position": 2,
                "code": "R-02",
                "title": "Emergency stop button",
                "purpose": "A dead button won't stop the robot.",
                "requires_photo": False,
                "is_critical": False,
                "example_photo_url": None,
            },
        ],
    },
]


# ============================================================
# ДВОЙНИКИ
# ============================================================

class Recorder:
    """Собирает то, что бот отправил и что ушло в API."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.photos: list[dict] = []
        self.deleted: list[tuple] = []
        self.answered: list[dict] = []
        self.api: list[dict] = []
        self.sequence = 0

    def next_id(self) -> int:
        self.sequence += 1
        return self.sequence


@pytest.fixture()
def rec(monkeypatch) -> Recorder:
    """Подменяет Telegram и API. Возвращает запись вызовов."""
    recorder = Recorder()

    def fake_send_message(chat_id, text, reply_to_message_id=None,
                          disable_notification=False, message_thread_id=None,
                          reply_markup=None):
        message_id = recorder.next_id()
        recorder.sent.append({
            "chat_id": chat_id,
            "text": text,
            "thread_id": message_thread_id,
            "reply_markup": reply_markup,
            "message_id": message_id,
        })
        return {"message_id": message_id}

    def fake_send_photo_url(chat_id, url, caption=None, message_thread_id=None,
                            reply_markup=None, timeout=60):
        message_id = recorder.next_id()
        recorder.photos.append({
            "chat_id": chat_id,
            "url": url,
            "caption": caption,
            "thread_id": message_thread_id,
            "reply_markup": reply_markup,
            "message_id": message_id,
        })
        return {"message_id": message_id}

    def fake_delete_message(chat_id, message_id):
        recorder.deleted.append((chat_id, message_id))
        return True

    def fake_answer(callback_query_id, text=None):
        recorder.answered.append({"id": callback_query_id, "text": text})
        return True

    def fake_send_chat_action(*args, **kwargs):
        return True

    monkeypatch.setattr(tg, "send_message", fake_send_message)
    monkeypatch.setattr(tg, "send_photo_url", fake_send_photo_url)
    monkeypatch.setattr(tg, "send_photo", fake_send_photo_url)
    monkeypatch.setattr(tg, "delete_message", fake_delete_message)
    monkeypatch.setattr(tg, "answer_callback_query", fake_answer)
    monkeypatch.setattr(tg, "send_chat_action", fake_send_chat_action)
    monkeypatch.setattr(tg, "edit_message_text", lambda *a, **k: {"message_id": 1})

    # Осмотр разрешён: адрес API и секрет заданы.
    monkeypatch.setattr(insp, "api_configured", lambda: True)

    # Сотрудник привязан и у него есть `auth_id`.
    monkeypatch.setattr(
        "inspection._bot",
        lambda: bot,
    )
    import telegram_store

    def fake_context(telegram_id, strict=False):
        return "Felipe Dominguez", {
            "user_name": "Felipe Dominguez",
            "card_id": 60143682,
            "auth_id": AUTH_ID,
            "warehouse": "GLP-C",
        }

    monkeypatch.setattr(telegram_store, "get_employee_context", fake_context)

    # По умолчанию устройство находится, шаблоны есть.
    monkeypatch.setattr(
        validation, "find_equipment",
        lambda kind, warehouse, code: dict(ROBOT),
    )
    monkeypatch.setattr(validation, "find_elsewhere", lambda *a, **k: [])
    monkeypatch.setattr(
        insp, "list_templates", lambda auth_id, category: list(TEMPLATES)
    )

    # 🔴 **Последний барьер: наружу из теста выйти нельзя.**
    #
    # Подмены выше целятся в имена, импортированные в `inspection` **по
    # значению** (`from inspection_api import list_templates`). Пока это было не
    # так, подмена не действовала, и тесты дошли до живого API владельца —
    # запись спас только 401 отозванного токена. Сторож устроен ниже уровня
    # `requests`, поэтому такую ошибку он ловит сразу и громко.
    _install_network_guard(monkeypatch)

    insp.reset_state()

    yield recorder

    insp.reset_state()


def _start(rec, monkeypatch, category="robot"):
    """Доводит диалог до выбора шаблона: тип, номер."""
    insp.start(CHAT_ID, {"id": USER_ID}, None)

    insp.handle_callback(
        CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, category], 1, "cb1"
    )
    insp.handle_text(CHAT_ID, {"id": USER_ID}, "3780", 2)


def _session():
    return insp.get_session(CHAT_ID, USER_ID)


def _created_inspection(items=None):
    """Ответ API на создание осмотра.

    🔴 **`example_photo_url` и `purpose` здесь не случайны.** API **копирует** их
    из шаблона в осмотр при создании (`InspectionService.create_inspection`), и
    бот обязан читать их **из осмотра**, а не из шаблона: правка шаблона не
    должна переписывать уже начатый осмотр. Именно поэтому образец стоит в
    ответе на создание, а не подставляется тестом при показе пункта.
    """
    return {
        "id": 77,
        "equipment_id": 40337,
        "warehouse": "GLP-C",
        "template_id": 1,
        "status": "draft",
        "verdict": None,
        "items": items or [
            {
                "id": 101, "position": 1, "code": "R-01",
                "title": "Chassis and wheels", "requires_photo": True,
                "is_critical": True, "verdict": None, "comment": None,
                "purpose": "A crack in the chassis grows into a breakdown.",
                "example_photo_url": "https://example.test/example-1.jpg",
            },
            {
                "id": 102, "position": 2, "code": "R-02",
                "title": "Emergency stop button", "requires_photo": False,
                "is_critical": False, "verdict": None, "comment": None,
                "purpose": "A dead button won't stop the robot.",
                "example_photo_url": None,
            },
        ],
    }


def _choose_template(rec, monkeypatch, items=None):
    """Выбирает шаблон; возвращает созданный «осмотр»."""
    created = _created_inspection(items)
    calls = []

    def fake_create(auth_id, equipment_id, template_id, comment=None):
        calls.append({
            "auth_id": auth_id,
            "equipment_id": equipment_id,
            "template_id": template_id,
        })
        return created

    monkeypatch.setattr(insp, "create_inspection", fake_create)
    insp.handle_callback(
        CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TEMPLATE, "1"], 3, "cb2"
    )
    return calls


# ============================================================
# ШАГ 1-2: ТИП И НОМЕР
# ============================================================

class TestStart:
    def test_command_offers_type_buttons(self, rec, monkeypatch) -> None:
        """После команды бот спрашивает ТИП — категория задаёт поиск."""
        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert rec.sent, "бот ничего не ответил на /inspection"
        prompt = rec.sent[0]
        assert "What do you want to inspect" in prompt["text"]

        flat = [
            button["callback_data"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert "ins:t:robot" in flat
        assert "ins:t:charging" in flat
        assert "ins:t:workstation" in flat
        assert "ins:x" in flat, "нет кнопки отмены"

    def test_unregistered_is_refused(self, rec, monkeypatch) -> None:
        """Без привязки осмотр не начинается: неизвестно, на кого писать."""
        import telegram_store

        monkeypatch.setattr(
            telegram_store, "get_employee_context", lambda *a, **k: (None, None)
        )
        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session() is None
        assert "not registered" in rec.sent[0]["text"]

    def test_employee_without_auth_id_is_refused(self, rec, monkeypatch) -> None:
        """Без `auth_id` осмотр записался бы без автора — отказываем."""
        import telegram_store

        monkeypatch.setattr(
            telegram_store, "get_employee_context",
            lambda *a, **k: ("Felipe", {"user_name": "Felipe", "card_id": 1}),
        )
        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session() is None
        assert "auth_id" in rec.sent[0]["text"]

    def test_not_configured_is_explained(self, rec, monkeypatch) -> None:
        """Не настроенный API — понятный отказ, а не молчание."""
        monkeypatch.setattr(insp, "api_configured", lambda: False)
        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert "not configured" in rec.sent[0]["text"]
        assert _session() is None


class TestNumber:
    def test_wrong_format_is_asked_again(self, rec, monkeypatch) -> None:
        """Мусор в поле номера — переспрашиваем, а не ищем."""
        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, "robot"], 1, "cb1"
        )

        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Unable to drive", 2)

        assert "digits only" in rec.sent[-1]["text"]
        assert _session().step == insp.STEP_NUMBER
        assert not _session().equipment, "на мусоре устройство не выбрано"

    def test_unknown_number_does_not_start(self, rec, monkeypatch) -> None:
        """Номера нет — осмотр НЕ начинается (требование владельца)."""
        monkeypatch.setattr(validation, "find_equipment", lambda *a, **k: None)

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, "robot"], 1, "cb1"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "99999", 2)

        assert "not in the system" in rec.sent[-1]["text"]
        assert _session().step == insp.STEP_NUMBER
        assert not _session().inspection

    def test_robot_number_is_not_found_as_station(self, rec, monkeypatch) -> None:
        """🔴 Главная проверка: робот не осматривается как станция.

        Номер `3780` есть, но он **робот**. Оператор выбрал «Charging station» —
        искать надо только среди станций, поэтому осмотр не начинается, а
        человеку показывают, где номер всё-таки есть.
        """
        calls = []

        def fake_find(kind, warehouse, code):
            calls.append((kind, warehouse, code))
            # Станции с таким номером нет; робот — есть.
            return None

        monkeypatch.setattr(validation, "find_equipment", fake_find)
        monkeypatch.setattr(
            validation, "find_elsewhere",
            lambda *a, **k: [("GLP-C", "robot")],
        )

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, "charging"], 1, "cb1"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "3780", 2)

        # Поиск шёл строго по категории станций.
        assert calls and calls[0][0] == validation.KIND_STATION, calls
        # Категория НЕ подменилась молча.
        assert _session().category == "charging"
        assert not _session().equipment
        assert not _session().inspection

        text = rec.sent[-1]["text"]
        assert "not in the system" in text
        assert "robot" in text, "не сказано, что номер есть как робот"
        assert "pick the right type" in text

    def test_number_from_another_warehouse_is_not_taken(
        self, rec, monkeypatch
    ) -> None:
        """🔴 Номер ищется на складе топика: чужой склад не подставляется.

        Номера повторяются между складами (206 случаев), поэтому поиск без
        склада вернул бы чужое устройство — и осмотр ушёл бы не туда.
        """
        seen = []

        def fake_find(kind, warehouse, code):
            seen.append(warehouse)
            # На складе топика (GLP-C) такого номера нет.
            return None

        monkeypatch.setattr(validation, "find_equipment", fake_find)
        monkeypatch.setattr(
            validation, "find_elsewhere",
            lambda *a, **k: [("SMALL-P3", "robot")],
        )

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, "robot"], 1, "cb1"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "13", 2)

        assert seen and seen[0] == "GLP-C", seen
        assert "SMALL-P3" in rec.sent[-1]["text"]
        assert not _session().inspection

    def test_not_in_work_is_refused(self, rec, monkeypatch) -> None:
        """🔴 «Не в работе» не осматривают — и человеку говорят, что делать.

        Единица выведена из работы (ремонт/списание). Молча разрешить значило бы
        отправить человека осматривать то, что стоит в ремонте. История перехода
        в этот статус в базе не ведётся, поэтому фильтр — по текущему.
        """
        broken = dict(ROBOT, status="not_in_work")
        monkeypatch.setattr(
            validation, "find_equipment", lambda *a, **k: broken
        )

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, "robot"], 1, "cb1"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "3780", 2)

        assert "NOT IN WORK" in rec.sent[-1]["text"]
        assert "/online" in rec.sent[-1]["text"], "не сказано, как вернуть в работу"
        assert _session().step == insp.STEP_NUMBER
        assert not _session().inspection

    def test_offline_is_allowed_with_warning(self, rec, monkeypatch) -> None:
        """`offline`/`maintenance` — предупреждение, а не отказ.

        Такое оборудование как раз чинят, и осмотреть его нужно: отказ здесь
        мешал бы осматривать станцию после ремонта.
        """
        monkeypatch.setattr(
            validation, "find_equipment",
            lambda *a, **k: dict(ROBOT, status="offline"),
        )

        _start(rec, monkeypatch)

        assert _session().step == insp.STEP_TEMPLATE
        assert "offline" in rec.sent[-1]["text"]


# ============================================================
# ШАГ 3: ШАБЛОНЫ
# ============================================================

class TestTemplates:
    def test_templates_are_filtered_by_category(self, rec, monkeypatch) -> None:
        """Шаблоны запрашиваются строго по категории найденного устройства."""
        requested = []
        monkeypatch.setattr(
            insp, "list_templates",
            lambda auth_id, category: requested.append(category) or list(TEMPLATES),
        )

        _start(rec, monkeypatch, category="charging")

        assert requested == ["charging"], requested

    def test_robot_gets_no_charging_checklist(self, rec, monkeypatch) -> None:
        """Роботу не предлагают «Charging station inspection»."""
        monkeypatch.setattr(
            insp, "list_templates",
            lambda auth_id, category: (
                list(TEMPLATES) if category == "robot" else []
            ),
        )

        _start(rec, monkeypatch, category="charging")

        # Для станции шаблонов нет — диалог не продолжается.
        assert _session().step == insp.STEP_NUMBER
        assert "No inspection checklist" in rec.sent[-1]["text"]

    def test_template_keyboard_is_offered(self, rec, monkeypatch) -> None:
        """Найденное устройство + кнопки шаблонов."""
        _start(rec, monkeypatch)

        assert _session().step == insp.STEP_TEMPLATE
        prompt = rec.sent[-1]
        assert "Found: 3780" in prompt["text"]

        flat = [
            b["callback_data"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        assert "ins:k:1" in flat


# ============================================================
# ШАГ 4: ПУНКТЫ
# ============================================================

class TestItems:
    def test_first_item_shows_what_and_why(self, rec, monkeypatch) -> None:
        """Показывается что проверяем, зачем и фото-пример."""
        _start(rec, monkeypatch)
        calls = _choose_template(rec, monkeypatch)

        # Осмотр привязан к устройству по КЛЮЧУ, а не по номеру-строке.
        assert calls and calls[0]["equipment_id"] == ROBOT["id"]
        assert calls[0]["auth_id"] == AUTH_ID, "осмотр не от имени работника"

        assert rec.photos, "фото-пример не отправлен"
        example = rec.photos[-1]
        assert example["url"] == "https://example.test/example-1.jpg"
        assert "Chassis and wheels" in example["caption"]
        assert "Why:" in example["caption"]
        assert "Send a photo" in example["caption"]

    def test_photo_saves_point_and_advances(self, rec, monkeypatch) -> None:
        """Фото = «норма»: пункт закрывается `ok`, диалог идёт дальше."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        photo_calls = []
        verdict_calls = []

        monkeypatch.setattr(
            insp, "upload_item_photo",
            lambda auth_id, inspection_id, item_id, content, filename="photo.jpg",
                   content_type="image/jpeg": (
                photo_calls.append((inspection_id, item_id, content)), {}
            )[1],
        )
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda auth_id, inspection_id, item_id, verdict, comment=None: (
                verdict_calls.append((item_id, verdict, comment)), {}
            )[1],
        )

        path = _write_photo(monkeypatch)
        insp.handle_photo(
            CHAT_ID, {"id": USER_ID},
            {"photo": [{"file_id": "f", "file_unique_id": "u"}]},
            20, path,
        )

        assert photo_calls and photo_calls[0][1] == 101, photo_calls
        assert verdict_calls == [(101, "ok", None)], verdict_calls
        assert _session().item_index == 1, "не перешли к следующему пункту"

    def test_example_and_user_photo_are_deleted(self, rec, monkeypatch) -> None:
        """🔴 Фото-пример и фото оператора убираются из чата."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        example_message_id = _session().data["example_message_id"]
        rec.deleted.clear()

        monkeypatch.setattr(
            insp, "upload_item_photo", lambda *a, **k: {}
        )
        monkeypatch.setattr(
            insp, "set_item_verdict", lambda *a, **k: {}
        )

        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 4242, path)

        assert (CHAT_ID, 4242) in rec.deleted, "фото оператора осталось"
        assert (CHAT_ID, example_message_id) in rec.deleted, "фото-пример осталось"

    def test_last_point_finishes_inspection(self, rec, monkeypatch) -> None:
        """После последнего пункта осмотр закрывается и показывается итог."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        monkeypatch.setattr(
            insp, "set_item_verdict", lambda *a, **k: {}
        )
        monkeypatch.setattr(
            insp, "complete_inspection",
            lambda auth_id, inspection_id, comment=None: {
                "id": 77, "status": "done", "verdict": "ok",
                "template_id": 1,
                "items": [
                    {"id": 101, "title": "Chassis and wheels",
                     "verdict": "ok", "comment": None},
                    {"id": 102, "title": "Emergency stop button",
                     "verdict": "ok", "comment": None},
                ],
            },
        )
        monkeypatch.setattr(
            insp, "create_share",
            lambda auth_id, inspection_id: {"public_token": "Tok123"},
        )

        path = _write_photo(monkeypatch)
        # Пункт 1 — фото, пункт 2 — фото.
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 31, path)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 32, path)

        # **После закрытия диалог снимается** — кнопок на итоговом экране нет,
        # и держать состояние было бы нечем. Поэтому сессии уже нет.
        assert _session() is None

        texts = "\n".join(item["text"] for item in rec.sent)
        assert "Inspection passed" in texts
        assert "Checked: 2 of 2" in texts

    def test_public_link_is_sent(self, rec, monkeypatch) -> None:
        """Общая ссылка «для всех на веб-версию» приходит в чат."""
        _start(rec, monkeypatch)
        _choose_template(
            rec, monkeypatch,
            items=[{
                "id": 101, "position": 1, "code": "R-01", "title": "Only point",
                "requires_photo": False, "is_critical": False, "verdict": None,
            }],
        )

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})
        monkeypatch.setattr(
            insp, "complete_inspection",
            lambda *a, **k: {
                "id": 77, "status": "done", "verdict": "ok", "template_id": 1,
                "items": [{"id": 101, "title": "Only point",
                           "verdict": "ok", "comment": None}],
            },
        )
        monkeypatch.setattr(
            insp, "create_share",
            lambda auth_id, inspection_id: {"public_token": "Tok123"},
        )

        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 41, path)

        texts = "\n".join(item["text"] for item in rec.sent)
        assert "for everyone on the web" in texts
        assert "/inspection/share/Tok123" in texts


class TestProblemButton:
    def test_problem_asks_for_a_comment(self, rec, monkeypatch) -> None:
        """Кнопка Problem просит комментарий, а не ставит вердикт сразу."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )

        assert _session().step == insp.STEP_PROBLEM
        assert "Describe what is wrong" in rec.sent[-1]["text"]

    def test_critical_problem_is_failed_with_comment(self, rec, monkeypatch) -> None:
        """Критичный пункт → `failed`, комментарий сохраняется.

        Требование владельца: кнопка «Problem» — если что-то не так. Провал
        требует объяснения, иначе тому, кто будет чинить, нечего дел��ть.
        """
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        verdict_calls = []
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda auth_id, inspection_id, item_id, verdict, comment=None: (
                verdict_calls.append((item_id, verdict, comment)), {}
            )[1],
        )

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Horn is cracked", 51)

        assert verdict_calls == [(101, "failed", "Horn is cracked")], verdict_calls
        assert _session().item_index == 1

    def test_non_critical_problem_is_warning(self, rec, monkeypatch) -> None:
        """Некритичный пункт → `warning`: осмотр не «падает» из-за мелочи."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        monkeypatch.setattr(
            insp, "upload_item_photo", lambda *a, **k: {}
        )
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})

        # Проходим пункт 1 фото, затем Problem на пункте 2.
        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 60, path)

        verdict_calls = []
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda auth_id, inspection_id, item_id, verdict, comment=None: (
                verdict_calls.append((item_id, verdict, comment)), {}
            )[1],
        )
        monkeypatch.setattr(
            insp, "complete_inspection",
            lambda auth_id, inspection_id, comment=None: {
                "id": 77, "status": "done", "verdict": "warning", "template_id": 1,
                "items": [
                    {"id": 101, "title": "Chassis and wheels",
                     "verdict": "ok", "comment": None},
                    {"id": 102, "title": "Emergency stop button",
                     "verdict": "warning", "comment": "Dust on the button"},
                ],
            },
        )
        monkeypatch.setattr(
            insp, "create_share",
            lambda auth_id, inspection_id: {"public_token": "T"},
        )

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "1"], 61, "cb4"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Dust on the button", 62)

        assert verdict_calls == [(102, "warning", "Dust on the button")]

        # Итог осмотра показывает предупреждение, а не «всё хорошо».
        texts = "\n".join(item["text"] for item in rec.sent)
        assert "passed with warnings" in texts
        assert "Dust on the button" in texts

    def test_empty_comment_is_refused(self, rec, monkeypatch) -> None:
        """Пустой комментарий не закрывает пункт."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "   ", 51)

        assert _session().step == insp.STEP_PROBLEM
        assert "describe the problem" in rec.sent[-1]["text"].lower()

    def test_stale_problem_button_is_ignored(self, rec, monkeypatch) -> None:
        """Кнопка Problem на уже пройденном пункте не открывает комментарий."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        # Пункт 9 — такого индекса нет.
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "9"], 50, "cb3"
        )

        assert _session().step == insp.STEP_ITEM
        assert rec.answered[-1]["text"] == "This point is already done"


# ============================================================
# ОТМЕНА И TTL
# ============================================================

class TestCancelAndTtl:
    def test_cancel_button_drops_the_dialogue(self, rec, monkeypatch) -> None:
        """Кнопка Cancel завершает диалог."""
        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_CANCEL], 1, "cb1"
        )

        assert _session() is None
        assert rec.answered[-1]["text"] == "Cancelled"

    def test_cancel_keeps_the_draft(self, rec, monkeypatch) -> None:
        """`/cancel` не удаляет незакрытый осмотр — он остаётся черновиком."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        handled = insp.cancel(CHAT_ID, {"id": USER_ID})

        assert handled is True
        assert _session() is None
        assert "draft is kept" in rec.sent[-1]["text"]

    def test_cancel_without_dialogue_reports_nothing(self, rec, monkeypatch) -> None:
        """Без диалога `/cancel` не считает себя обработанным."""
        assert insp.cancel(CHAT_ID, {"id": USER_ID}) is False

    def test_expired_session_is_dropped(self, rec, monkeypatch) -> None:
        """Истёкший по TTL диалог не принимает ответ."""
        _start(rec, monkeypatch)

        key = insp._key(CHAT_ID, USER_ID)
        insp._SESSIONS[key]["expires"] = 0

        assert insp.get_session(CHAT_ID, USER_ID) is None
        assert insp.handle_text(CHAT_ID, {"id": USER_ID}, "3780", 2) is False

    def test_button_after_expiry_explains(self, rec, monkeypatch) -> None:
        """Кнопка после истечения — понятный текст, а не молчание."""
        _start(rec, monkeypatch)

        key = insp._key(CHAT_ID, USER_ID)
        insp._SESSIONS[key]["expires"] = 0

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TEMPLATE, "1"], 1, "cb1"
        )

        assert "expired" in rec.answered[-1]["text"]

    def test_sweep_removes_expired(self, rec, monkeypatch) -> None:
        """Просроченные сессии вычищаются, словарь не растёт."""
        _start(rec, monkeypatch)
        insp._SESSIONS[insp._key(CHAT_ID, USER_ID)]["expires"] = 0

        assert insp.sweep_sessions() == 1
        assert insp._SESSIONS == {}


# ============================================================
# ОШИБКИ API
# ============================================================

class TestApiErrors:
    def test_api_failure_is_explained(self, rec, monkeypatch) -> None:
        """Сбой API — понятный текст, а не «500» на экране."""
        monkeypatch.setattr(
            insp, "list_templates",
            lambda *a, **k: (_ for _ in ()).throw(
                inspection_api.InspectionApiUnavailable("API is down")
            ),
        )

        _start(rec, monkeypatch)

        assert "API is down" in rec.sent[-1]["text"]

    def test_rejected_point_is_reported(self, rec, monkeypatch) -> None:
        """Отказ API на вердикте виден человеку."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda *a, **k: (_ for _ in ()).throw(
                inspection_api.InspectionApiError("A comment is required")
            ),
        )

        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 70, path)

        assert "A comment is required" in rec.sent[-1]["text"]
        assert _session().item_index == 0, "пункт не должен считаться закрытым"

    def test_photo_failure_does_not_close_the_point(self, rec, monkeypatch) -> None:
        """Не сохранившееся фото не закрывает пункт как «норма»."""
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        monkeypatch.setattr(
            insp, "upload_item_photo",
            lambda *a, **k: (_ for _ in ()).throw(
                inspection_api.InspectionApiUnavailable("Storage is down")
            ),
        )
        called = []
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda *a, **k: called.append(1) or {},
        )

        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 71, path)

        assert called == [], "вердикт поставлен без фото"
        assert _session().item_index == 0


# ============================================================
# ДИСПЕТЧЕР: /inspection и топики
# ============================================================

class TestDispatcher:
    def test_command_is_known_and_documented(self) -> None:
        """Команда есть в списке известных и в /help."""
        assert "inspection" in bot.KNOWN_COMMANDS
        assert "/inspection" in bot.HELP_TEXT

    def test_not_allowed_outside_monitored_topic(self) -> None:
        """🔴 В чужом топике бот молчит: осмотр делают в рабочем.

        `OUTSIDE_TOPIC_COMMANDS` — это ровно список «отвечать можно и в чужом
        топике». Осмотру там не место: владелец требует, чтобы бот молчал в
        топиках, которые не обслуживает.
        """
        assert "inspection" not in bot.OUTSIDE_TOPIC_COMMANDS

    def test_callback_prefix_does_not_clash(self) -> None:
        """Префикс `ins` не занят другими диалогами."""
        used = {"dt", "ed", "rf", "em", "w", "st"}
        assert insp.CB_PREFIX not in used


class TestEndToEndDispatch:
    """Сквозной путь через `handle_update` — как это видит Telegram.

    🔴 **Зачем отдельно от тестов выше.** Те вызывают функции диалога напрямую
    и потому **не поймали бы** самую вероятную поломку: команда заведена, но
    диспетчер её не зовёт, или номер/фото не доходят до осмотра, потому что их
    перехватил приём ошибок. Здесь апдейт идёт тем же путём, что в проде:
    `handle_update` → маршрутизация топика → команда/текст/фото.
    """

    def _message(self, text=None, photo=False, message_id=900, thread_id=2):
        message = {
            "message_id": message_id,
            "from": {"id": USER_ID, "is_bot": False, "username": "t", "first_name": "T"},
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "date": int(__import__("time").time()),
        }

        if thread_id is not None:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True

        if text is not None:
            message["text"] = text

        if photo:
            message["photo"] = [
                {"file_id": "small", "file_unique_id": "s"},
                {"file_id": "big", "file_unique_id": "b"},
            ]

        return {"update_id": message_id, "message": message}

    def _callback(self, data, message_id=901, thread_id=2):
        return {
            "update_id": message_id,
            "callback_query": {
                "id": f"cb{message_id}",
                "from": {"id": USER_ID, "is_bot": False,
                         "username": "t", "first_name": "T"},
                "message": {
                    "message_id": message_id,
                    "chat": {"id": CHAT_ID, "type": "supergroup"},
                    "from": {"id": 1, "is_bot": True},
                    "message_thread_id": thread_id,
                },
                "data": data,
            },
        }

    def _pass_through_gates(self, monkeypatch):
        """Разрешает обработку в тестовом чате и топике.

        Это **не** отключение проверок бота, а снятие внешней конфигурации:
        белый список чатов и тема читаются из окружения, и без них диспетчер
        остановится **до** кода под тестом.
        """
        monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
        # Топик 2 — рабочий топик; маршрутизация ошибок не настроена, поэтому
        # `topic_allowed` считает его общим, а он бот обслуживает.
        monkeypatch.setattr(bot, "TELEGRAM_TOPIC_ID", None)
        monkeypatch.setattr(bot, "LISTEN_TOPIC_IDS", set())
        monkeypatch.setattr(bot, "LISTEN_TOPIC_NAMES", set())
        monkeypatch.setattr(bot, "_ALLOWED_OUTPUT_TOPICS", set())
        monkeypatch.setattr(bot, "_delete_user_message", lambda *a, **k: True)
        monkeypatch.setattr(bot, "_send_action", lambda *a, **k: None)

    def test_command_starts_the_dialogue(self, rec, monkeypatch) -> None:
        """`/inspection` из апдейта действительно открывает диалог."""
        self._pass_through_gates(monkeypatch)

        bot.handle_update(self._message(text="/inspection"), "TestBot")

        assert _session() is not None, "команда не дошла до диалога осмотра"
        assert _session().step == insp.STEP_TYPE
        assert any("What do you want to inspect" in m["text"] for m in rec.sent)

    def test_number_text_reaches_the_dialogue(self, rec, monkeypatch) -> None:
        """Номер, присланный текстом, доходит до осмотра, а не теряется.

        Топик рабочий (общий), и текст в нём обрабатывается отдельной веткой:
        без явного хука номер ушёл бы в `Ignored: текст в общем топике`.
        """
        self._pass_through_gates(monkeypatch)

        bot.handle_update(self._message(text="/inspection", message_id=910), "TestBot")
        bot.handle_update(
            self._callback("ins:t:robot", message_id=911), "TestBot"
        )
        bot.handle_update(
            self._message(text="3780", message_id=912), "TestBot"
        )

        assert _session().step == insp.STEP_TEMPLATE, (
            f"номер не дошёл: шаг = {_session().step}"
        )
        assert _session().equipment.get("id") == ROBOT["id"]

    def test_photo_reaches_the_dialogue(self, rec, monkeypatch) -> None:
        """🔴 Фото пункта не перехватывается приёмом ошибок.

        Приём ошибок показывает меню на **любое** фото. Без раннего хука осмотра
        снимок ушёл бы в Lark как новая ошибка, а диалог встал бы намертво.
        """
        self._pass_through_gates(monkeypatch)

        monkeypatch.setattr(bot.tg, "get_file", lambda file_id: "photos/f.jpg")
        monkeypatch.setattr(
            bot.tg, "download_file",
            lambda path, dest: _touch(dest),
        )

        bot.handle_update(self._message(text="/inspection", message_id=920), "TestBot")
        bot.handle_update(self._callback("ins:t:robot", message_id=921), "TestBot")
        bot.handle_update(self._message(text="3780", message_id=922), "TestBot")
        _choose_template(rec, monkeypatch)
        bot.handle_update(
            self._callback("ins:k:1", message_id=923), "TestBot"
        )

        saved = []
        monkeypatch.setattr(
            insp, "upload_item_photo",
            lambda *a, **k: saved.append(1) or {},
        )
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})

        bot.handle_update(
            self._message(photo=True, message_id=924), "TestBot"
        )

        assert saved, (
            "фото не дошло до осмотра — его перехватил приём ошибок ошибок "
            "или ветка «не тот топик»"
        )
        assert _session().item_index == 1

    def test_cancel_command_stops_the_dialogue(self, rec, monkeypatch) -> None:
        """`/cancel` завершает осмотр."""
        self._pass_through_gates(monkeypatch)

        bot.handle_update(self._message(text="/inspection", message_id=930), "TestBot")
        assert _session() is not None

        bot.handle_update(self._message(text="/cancel", message_id=931), "TestBot")

        assert _session() is None, "/cancel не остановил осмотр"

    def test_inspection_text_wins_over_intake(self, rec, monkeypatch) -> None:
        """🔴 Текст осмотра не перехватывается приёмом ошибок.

        У обоих флоу есть текстовый шаг, и порядок веток решает, кто заберёт
        сообщение. В ветке **фото** осмотр стоит раньше приёма ошибок; если в
        ветке текста он окажется позже, номер устройства уйдёт в дерево приёма,
        и диалог осмотра встанет — «кнопка сработала, а номер пропал».

        Проверяем именно порядок: приём ошибок «жадничает» и забирает текст.
        """
        self._pass_through_gates(monkeypatch)

        bot.handle_update(self._message(text="/inspection", message_id=940), "TestBot")
        bot.handle_update(self._callback("ins:t:robot", message_id=941), "TestBot")

        import equipment_intake.flow as intake_flow

        monkeypatch.setattr(intake_flow, "handle_text", lambda *a, **k: True)

        bot.handle_update(self._message(text="3780", message_id=942), "TestBot")

        assert _session().step == insp.STEP_TEMPLATE, (
            "текст забрал приём ошибок раньше осмотра — номер устройства потерян"
        )


# ============================================================
# СТАТИЧЕСКИЕ ПРОВЕРКИ: правило «по типу»
# ============================================================

class TestTypeRuleWiring:
    """Проверки того, что поиск **действительно** идёт по типу.

    🔴 **Почему проверки поведенческие, а не «по тексту исходника».**

    Первая версия этих тестов искала строки в `getsource(find_equipment)`. Она
    пропустила реальную поломку: когда из запроса убрали `category`, все 39
    тестов остались зелёными. Причина — проверка была про **наличие имени** в
    коде, а не про **параметры запроса**.

    Теперь перехватывается настоящий вызов `rest_get`, и проверяются
    параметры, с которыми он ушёл бы в базу. Убрать категорию, склад или
    заменить точное сравнение на подстрочное после этого не получится незаметно.
    """

    def _capture(self, monkeypatch) -> list:
        """Перехватывает запрос к справочнику, не пуская его в базу."""
        import sendToDataBase

        captured = []

        def fake_rest_get(table, params=None, optional=False):
            captured.append({"table": table, "params": dict(params or {})})
            return []

        monkeypatch.setattr(sendToDataBase, "rest_get", fake_rest_get)

        return captured

    def test_category_is_sent_to_the_database(self, monkeypatch) -> None:
        """🔴 Категория обязана быть в запросе: иначе робот сойдёт за станцию.

        Проверено на живой базе: в одном складе 34 номера принадлежат двум
        категориям сразу (в SMALL-P3 номер `13` — и робот, и зарядная станция).
        Без фильтра категории «осмотреть робота как станцию» — вопрос случая.
        """
        captured = self._capture(monkeypatch)

        validation.find_equipment(validation.KIND_STATION, "GLP-C", "3780")

        assert captured, "запрос в справочник не сделан"
        params = captured[0]["params"]

        assert params.get("category") == "eq.charging", (
            "категория не передана в запрос — станция ищется среди всех типов, "
            "и робота можно осмотреть как станцию"
        )

    def test_robot_kind_maps_to_robot_category(self, monkeypatch) -> None:
        """Вид «робот» ищется только среди роботов."""
        captured = self._capture(monkeypatch)

        validation.find_equipment(validation.KIND_ROBOT, "GLP-C", "3780")

        assert captured[0]["params"].get("category") == "eq.robot"

    def test_lookup_is_exact_not_partial(self, monkeypatch) -> None:
        """Поиск точный (`eq.`), а не подстрочный.

        Проверено на живой ручке API: `GET /v1/equipment?search=13` вернул `113`
        и `213`, но **не** `13`, а параметр `equipment_code` в API игнорируется
        вовсе. Поэтому точное сравнение обязано быть в этом запросе.
        """
        captured = self._capture(monkeypatch)

        validation.find_equipment(validation.KIND_ROBOT, "GLP-C", "3780")

        assert captured[0]["params"].get("equipment_code") == "eq.3780", (
            "номер ищется не точным сравнением — вернулись бы 37800 и 13780"
        )

    def test_warehouse_is_always_in_the_request(self, monkeypatch) -> None:
        """Склад попадает в запрос — без него вернулось бы чужое устройство.

        Номера повторяются между складами: проверено — 206 номеров встречаются
        более чем на одном складе (`715`, `716` есть и в GLP-C, и в P3-DC-1).
        """
        captured = self._capture(monkeypatch)

        validation.find_equipment(validation.KIND_ROBOT, "SMALL-P3", "715")

        assert captured[0]["params"].get("warehouse") == "eq.SMALL-P3", (
            "поиск без склада: номера повторяются между складами"
        )

    def test_no_second_equipment_lookup_in_api_client(self) -> None:
        """Вторая реализация поиска в `inspection_api` не завелась.

        В проекте уже дважды расходились «общие» функции, продублированные в
        двух местах (см. `test_cross_repo_parity.py`). Плюс именно эта копия и
        была ошибкой: API ищет по подстроке, а не по номеру.
        """
        source = (PROJECT_ROOT + "/inspection_api.py")
        text = open(source, encoding="utf-8").read()

        assert "def find_equipment" not in text, (
            "в inspection_api появилась вторая реализация поиска устройства — "
            "она разойдётся с equipment_intake.validation"
        )


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def _touch(destination: str) -> str:
    """Создаёт файл-заглушку на месте скачанного фото."""
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

    with open(destination, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xe0" + b"0" * 32)

    return destination


def _write_photo(monkeypatch) -> str:
    """Кладёт временный файл «фото» и возвращает путь."""
    import tempfile

    directory = tempfile.mkdtemp(prefix="inspection-test-")
    path = os.path.join(directory, "photo.jpg")

    with open(path, "wb") as handle:
        # Минимальный валидный JPEG-заголовок: содержимое не проверяется,
        # но файл должен читаться.
        handle.write(b"\xff\xd8\xff\xe0" + b"0" * 32)

    return path
