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

#: **Настоящая функция поиска**, снятая ДО подмены фикстурой `rec`.
#:
#: Нужна тесту «выбранный склад уходит в поиск»: он обязан перехватить
#: **реальный** запрос к справочнику, а не строку в исходнике и не заглушку,
#: которая вернёт что угодно независимо от склада. На этом в проекте уже
#: обжигались (см. `TestTypeRuleWiring`).
_REAL_FIND_EQUIPMENT = validation.find_equipment


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


def _start_in_service_topic(rec, monkeypatch, warehouse="SMALL-P3", category="robot",
                            number="3780"):
    """
    Вход из **служебного** топика: склад спрашивают и выбирают.

    Маршрутизация топиков ошибок настроена (`_error_routing`), а сам вход — не
    из топика ошибок, поэтому `error_warehouse_for_thread` вернёт `None` — ровно
    как в боевом служебном топике `Inspection` (25189). Это сценарий владельца.
    """
    _error_routing(monkeypatch)
    insp.start(CHAT_ID, {"id": USER_ID}, None)

    if _session().step == insp.STEP_WAREHOUSE:
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_WAREHOUSE, warehouse], 1, "cbw"
        )

    insp.handle_callback(
        CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_TYPE, category], 2, "cb1"
    )
    insp.handle_text(CHAT_ID, {"id": USER_ID}, number, 3)


def _session():
    return insp.get_session(CHAT_ID, USER_ID)


#: Боевой служебный топик `Inspection` (владелец, 17.10.2026). Именно в нём
#: теперь живёт `/inspection`, и именно он не определяет склад сам по себе.
SERVICE_THREAD_ID = 25189


def _error_routing(monkeypatch, mapping=None):
    """
    Приводит окружение топиков к **боевому** и ставит источник в служебный топик.

    🔴 **Зачем тестам это нужно.** `error_warehouse_for_thread` в режиме «ни один
    топик ошибок не настроен» намеренно отдаёт `DEFAULT_WAREHOUSE` — это
    историческая совместимость (`error_routing_configured`), иначе бот не принял
    бы ничего вообще. Но **в проде маршрутизация настроена**
    (`TELEGRAM_TOPIC_ID=2`, `TELEGRAM_TOPIC_ERROR_SP3=318`, служебный 25189),
    поэтому запрос для служебного топика `Inspection` даёт `None` — и именно это
    включает вопрос о складе. Тест, оставивший окружение пустым, проверял бы
    другой режим и «падал бы на конфигурации оператора».

    **Почему вместе с топиками настраивается источник.** Барьер
    `_output_topic_allowed` — последняя линия защиты бота, и обходить его в
    тестах нельзя. Боевой ответ уходит в 25189; он разрешён на отправку лишь
    потому, что `service` теперь в `allowed_output_topics()`. Здесь это и
    проверяется попутно: если `service` из списка убрать, ответы «уйдут в
    никуда» и тесты это заметят (отправок просто не будет).

    **Подмена — только через `monkeypatch`.** Мутировать `bot.TOPIC_RAW` вручную
    нельзя: это общий словарь модуля, и `clear()` в одном тесте утёк бы в
    следующие (проверено: ломало диспетчерские тесты ниже по файлу).
    """
    monkeypatch.setattr(bot, "TOPIC_RAW", dict(mapping or {
        "error": "2",
        "error:SMALL-P3": "318",
        "service": str(SERVICE_THREAD_ID),
    }))
    monkeypatch.setattr(bot, "TELEGRAM_TOPIC_ID", 2)
    monkeypatch.setattr(bot, "TELEGRAM_TOPIC_NAME", "")
    # Кэш разрешённых топиков пересчитывается от подменённого `TOPIC_RAW`.
    monkeypatch.setattr(bot, "_ALLOWED_OUTPUT_TOPICS", set())
    # Источник ответа — служебный топик: как `/inspection` в `Inspection`.
    #
    # Подменяем именно `_reply_thread`, а не thread-local `_origin_thread`:
    # ответы в тестах уходят через `_send`, который при пустом `thread_id`
    # спрашивает этот помощник. Подмена чистая — `monkeypatch` вернёт функцию
    # обратно сам, и состояние не утечёт в следующие тесты.
    monkeypatch.setattr(
        bot, "_reply_thread",
        lambda chat_id, thread_id=None: (
            thread_id if thread_id is not None else SERVICE_THREAD_ID
        ),
    )


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

    def test_cancel_removes_the_problem_question(self, rec, monkeypatch) -> None:
        """🔴 Cancel убирает и вопрос «Describe what is wrong».

        Владелец 23.10.2026: «cancel работает криво очень».

        Причина дефекта: `Cancel` удалял карточку пункта и фото-пример, но
        сообщение, которое бот присылает **после кнопки `Problem`**, нигде не
        сохранялось — и оставалось в чате после отмены. За осмотр из
        четырнадцати пунктов топик забивался брошенными «Describe what is
        wrong» с кнопками, которые уже ничего не делают.

        Хуже: человек нажимал эти старые кнопки снова, получал «This inspection
        expired» и решал, что бот сломан.
        """
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )

        problem_message_id = _session().data.get("problem_message_id")

        assert problem_message_id, "id вопроса о проблеме не сохранён — Cancel его не найдёт"

        rec.deleted.clear()

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_CANCEL], 70, "cb9"
        )

        assert _session() is None
        assert (CHAT_ID, problem_message_id) in rec.deleted, (
            "вопрос «Describe what is wrong» остался в чате после отмены"
        )
        # Само нажатие тоже убираем: иначе кнопки висят без владельца.
        assert (CHAT_ID, 70) in rec.deleted

    def test_cancel_removes_the_question_after_a_problem_was_reported(
        self, rec, monkeypatch
    ) -> None:
        """Cancel после описания проблемы (шаг фото) тоже чистит чат."""
        _start(rec, monkeypatch)
        _choose_template(
            rec, monkeypatch,
            items=[{
                "id": 101, "position": 1, "code": "R-01", "title": "Horn",
                "requires_photo": True, "is_critical": False, "verdict": None,
            }, {
                "id": 102, "position": 2, "code": "R-02", "title": "Button",
                "requires_photo": False, "is_critical": False, "verdict": None,
            }],
        )
        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Horn is cracked", 51)

        problem_message_id = _session().data.get("problem_message_id")

        assert problem_message_id

        rec.deleted.clear()
        insp.cancel(CHAT_ID, {"id": USER_ID})

        assert (CHAT_ID, problem_message_id) in rec.deleted, (
            "после /cancel вопрос о проблеме остался"
        )

    def test_step_message_is_deleted_before_the_next_point(self, rec, monkeypatch) -> None:
        """🔴 Сообщение пункта убирается из чата при переходе к следующему.

        Владелец 17.10.2026: «шаги потом не удаляются». Убирались **только два
        фото**, а текст «Step N of M» с кнопками `Problem` / `Cancel` оставался:
        за осмотр из четырнадцати пунктов в топике накапливалось столько же
        сообщений, и оператор не понимал, какой пункт активен — **старые кнопки
        выглядят рабочими**.

        Найдено воспроизведением: при переходе удалялись фото оператора и
        примера, а `prompt_message_id` прошлого пункта оставался, хотя сессия уже
        указывала на новое сообщение. Старый текст терял владельца, но не исчезал.
        """
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        # Сообщение пункта, которое показывает бот (текст + кнопки).
        step_message_id = _session().data["prompt_message_id"]
        rec.deleted.clear()

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})

        path = _write_photo(monkeypatch)
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 5150, path)

        assert (CHAT_ID, step_message_id) in rec.deleted, (
            f"сообщение пункта {step_message_id} осталось в чате — шаги копятся"
        )

    def test_each_transition_deletes_exactly_one_step(self, rec, monkeypatch) -> None:
        """За каждый переход убирается **ровно одно** сообщение пункта.

        Сторож от обратной ошибки: удалить лишнее (например, новое сообщение
        сразу после отправки) значило бы показать пустоту вместо пункта.
        """
        _start(rec, monkeypatch)
        _choose_template(rec, monkeypatch)

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})

        first = _session().data["prompt_message_id"]
        rec.deleted.clear()

        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 6001, _write_photo(monkeypatch))

        # Прошлое сообщение удалено, новое — осталось в сессии и в чате.
        assert (CHAT_ID, first) in rec.deleted
        second = _session().data["prompt_message_id"]
        assert second != first, "новый пункт переиспользовал id прошлого сообщения"
        assert (CHAT_ID, second) not in rec.deleted, "удалено сообщение нового пункта"

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
        требует объяснения, иначе тому, кто будет чинить, нечего делать.

        **Пункт без обязательного снимка (уточнено 23.10.2026).** Раньше тест
        брал пункт из общего набора, а у него `requires_photo=True`. После
        исправления дефекта `A photo is required for: K-12` такой пункт
        **нельзя** закрыть без снимка — и тест падал, хотя поведение верное.
        Здесь проверяется именно вердикт, поэтому снимок не требуется; путь
        «проблема + обязательный снимок» проверяет следующий тест.
        """
        _start(rec, monkeypatch)
        _choose_template(
            rec, monkeypatch,
            items=[{
                "id": 101, "position": 1, "code": "R-01", "title": "Horn",
                "requires_photo": False, "is_critical": True, "verdict": None,
            }, {
                # Второй пункт нужен, чтобы сессия пережила вердикт первого:
                # на последнем пункте диалог закрывается и сессии уже нет.
                "id": 102, "position": 2, "code": "R-02", "title": "Button",
                "requires_photo": False, "is_critical": False, "verdict": None,
            }],
        )

        verdict_calls = []
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda auth_id, inspection_id, item_id, verdict, comment=None: (
                verdict_calls.append((item_id, verdict, comment)), {}
            )[1],
        )
        # Осмотр после последнего пункта закрывается — сеть в тестах запрещена.
        # `create_share` создаёт публичную ссылку отдельным запросом.
        monkeypatch.setattr(insp, "create_share", lambda *a, **k: {"token": "t"})
        # Возвращаем правдоподобный итог: код читает `id` и `items`.
        monkeypatch.setattr(
            insp, "complete_inspection",
            lambda *a, **k: {
                "id": 77, "status": "done", "verdict": "failed", "template_id": 1,
                "warehouse": "GLP-C", "equipment_id": 5,
                "items": [{
                    "id": 101, "code": "R-01", "title": "Horn",
                    "verdict": "failed", "comment": "Horn is cracked",
                    "requires_photo": False, "is_critical": True,
                }],
            },
        )

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Horn is cracked", 51)

        # 🔴 Снимок доказательства обязателен и для критичного пункта
        # (владелец 23.10.2026). Без него вердикта быть не должно.
        assert verdict_calls == [], "вердикт поставлен без снимка доказательства"

        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})
        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 52, _write_photo(monkeypatch))

        assert verdict_calls == [(101, "failed", "Horn is cracked")], verdict_calls
        assert _session().item_index == 1

    def test_problem_on_photo_point_asks_for_photo_and_keeps_comment(
        self, rec, monkeypatch
    ) -> None:
        """🔴 Пункт с обязательным снимком: сначала фото, но текст НЕ теряется.

        Дефект 23.10.2026 из живого бота:
        `⚠️ Couldn't close the inspection: A photo is required for: K-12`.

        `Problem` ставил вердикт **без снимка**, и пункт с `requires_photo`
        оказывался закрыт «без фото» — после чего осмотр **нельзя было
        завершить никак**: сервер требовал снимок, а вернуться к пункту уже
        нельзя, он считался пройденным.

        Проверяем два свойства исправления:
          1. вердикт **не ставится**, пока нет снимка (иначе тупик);
          2. объяснение человека **сохраняется** — иначе после снимка пункт
             закрылся бы как `ok`, то есть «проблемы нет».
        """
        _start(rec, monkeypatch)
        _choose_template(
            rec, monkeypatch,
            items=[{
                "id": 101, "position": 1, "code": "K-12", "title": "Lower Lens",
                "requires_photo": True, "is_critical": False, "verdict": None,
            }],
        )

        verdict_calls = []
        monkeypatch.setattr(
            insp, "set_item_verdict",
            lambda auth_id, inspection_id, item_id, verdict, comment=None: (
                verdict_calls.append((item_id, verdict, comment)), {}
            )[1],
        )
        # Сеть в тесте запрещена (`tests/conftest.py`): путь до вердикта не
        # доходит, но заглушки нужны, чтобы тест не зависел от порядка веток.
        monkeypatch.setattr(insp, "upload_item_photo", lambda *a, **k: {})

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_PROBLEM, "0"], 50, "cb3"
        )
        insp.handle_text(CHAT_ID, {"id": USER_ID}, "Lens is dirty", 51)

        # Вердикта нет: снимка ещё нет.
        assert verdict_calls == [], "пункт закрыт без обязательного снимка"

        session = _session()

        # Объяснение сохранено, а не выброшено.
        assert session.problem_comment == "Lens is dirty"

        # Пункт НЕ сдвинулся: он всё ещё открыт.
        assert session.item_index == 0

        texts = "\n".join(item["text"] for item in rec.sent)

        # Текст изменился 23.10.2026: снимок теперь требуется **любому**
        # проблемному пункту, и сообщение говорит не «нужно фото», а «пришлите
        # фото проблемы» — вместе с сохранённым описанием.
        assert "Send a photo of the problem" in texts, texts[-400:]
        assert "Lower Lens" in texts
        # Человек видит, что его объяснение не потеряно.
        assert "Lens is dirty" in texts

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

        # 🔴 **Снимок обязателен для ЛЮБОГО проблемного пункта** (владелец
        # 23.10.2026: «если Problem то тоже нужно фото просто пометить что
        # проблемное»). До этой правки вердикт ставился сразу, и снимка у
        # проблемного пункта не было вовсе.
        assert verdict_calls == [], "вердикт поставлен без снимка доказательства"

        insp.handle_photo(CHAT_ID, {"id": USER_ID}, {}, 63, _write_photo(monkeypatch))

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

    def test_warehouse_action_is_nested_under_ins(self) -> None:
        """🔴 `CB_WAREHOUSE = "w"` — действие **внутри** `ins`, а не префикс.

        Совпадение буквы с чужим префиксом `w` (`_handle_warehouse_choice`)
        безопасно ровно потому, что диспетчер смотрит **первый** сегмент
        `callback_data`: наш данные начинаются с `ins:`, и чужой обработчик их
        не увидит. Проверяем и то, и другое: буква «w» занята как префикс, но
        наши кнопки так не начинаются.
        """
        assert insp.CB_WAREHOUSE == "w"
        keyboard = insp.warehouse_keyboard(
            insp.Session(chat_id=1, user_id=2, warehouses=["GLP-C", "SMALL-P3"])
        )
        flat = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        assert "ins:w:GLP-C" in flat
        assert "ins:w:SMALL-P3" in flat
        for data in flat:
            assert data.split(":")[0] == "ins", data


# ============================================================
# ШАГ 0: СКЛАД (17.10.2026)
# ============================================================

class TestWarehouseStep:
    """Склад спрашивается **только там, где топик его не задал**.

    Владелец перенёс `/inspection` в служебный топик `Inspection` (25189). Там
    склад не выводится из топика, и молчаливая привязка к `DEFAULT_WAREHOUSE`
    (GLP-C) искала бы станцию SMALL-P3 не на том складе — номера повторяются
    между складами.
    """

    def _employee(self, monkeypatch, **fields):
        import telegram_store

        card = {
            "user_name": "Felipe Dominguez",
            "card_id": 60143682,
            "auth_id": AUTH_ID,
            "warehouse": "GLP-C",
            "role": "worker",
            "is_leader": False,
        }
        card.update(fields)
        monkeypatch.setattr(
            telegram_store, "get_employee_context",
            lambda *a, **k: ("Felipe Dominguez", dict(card)),
        )

    def test_service_topic_asks_for_warehouse_first(self, rec, monkeypatch) -> None:
        """🔴 В служебном топике первый вопрос — склад, а не тип.

        Ключ к правке владельца: «добавь вопрос что за склад ещё».
        Проверяется на **настоящем** `error_warehouse_for_thread`: маршрутизация
        топиков ошибок настроена, а вход — не из топика ошибок (как боевой 25189).
        """
        _error_routing(monkeypatch)
        # Администратор: доступны оба склада, значит вопрос задаётся.
        self._employee(monkeypatch, role="admin", warehouse="GLP-C")

        assert bot.error_warehouse_for_thread(CHAT_ID, 25189) is None, (
            "предусловие: служебный топик не должен определять склад"
        )

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        session = _session()
        assert session is not None
        assert session.step == insp.STEP_WAREHOUSE, (
            f"первый шаг должен быть склад, а не {session.step}"
        )

        prompt = rec.sent[0]
        assert "Which warehouse" in prompt["text"]
        assert "What do you want to inspect" not in prompt["text"]

        flat = [
            b["callback_data"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        assert "ins:w:GLP-C" in flat
        assert "ins:w:SMALL-P3" in flat
        assert "ins:x" in flat, "нет кнопки отмены"

    def test_error_topic_is_refused(self, rec, monkeypatch) -> None:
        """🔴 В топике ошибок команда **отказывает** — она живёт только в `Inspection`.

        Требование владельца 17.10.2026: «нужно только в топике что я тебе дал».
        Отказ **проговаривается вслух**, а не молчанием: молчание выглядело бы
        как поломка — ровно так `/inspection` уже «не работала», когда топик не
        был разрешён на отправку.
        """
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="worker", warehouse="SMALL-P3")

        assert bot.error_warehouse_for_thread(CHAT_ID, 318) == "SMALL-P3"

        # Топик ошибок SMALL-P3: команда пришла оттуда же.
        monkeypatch.setattr(bot, "_reply_thread", lambda chat_id, thread_id=None: 318)

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session() is None, "в топике ошибок диалог начинаться не должен"
        assert "Inspection topic" in rec.sent[0]["text"], rec.sent[0]["text"]

    def test_error_topic_still_skips_the_question_when_service_is_unset(
        self, rec, monkeypatch
    ) -> None:
        """Без `TELEGRAM_TOPIC_SERVICE` ограничение снято — и склад берётся из топика.

        🔴 **Почему эта ветка осталась.** Если служебный топик не настроен,
        ограничивать команду нечем: иначе на свежей установке она стала бы
        мёртвой везде и это выглядело бы как «бот не отвечает». В этом режиме
        работает прежняя логика: в топике ошибок склад известен из топика, и
        вопрос не задаётся — лишний шаг замедлял бы работу.
        """
        _error_routing(monkeypatch)
        # Снимаем настройку служебного топика — только для этого теста.
        monkeypatch.setattr(bot, "topic_thread", lambda kind, chat_id=None: None)
        self._employee(monkeypatch, role="worker", warehouse="SMALL-P3")

        assert bot.error_warehouse_for_thread(CHAT_ID, 318) == "SMALL-P3"
        monkeypatch.setattr(bot, "_reply_thread", lambda chat_id, thread_id=None: 318)

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        session = _session()
        assert session is not None
        assert session.step == insp.STEP_TYPE, "в топике ошибок шага склада быть не должно"
        assert session.warehouse == "SMALL-P3", "склад не взят из топика ошибок"
        assert "What do you want to inspect" in rec.sent[0]["text"]

    def test_choosing_warehouse_shows_type_next(self, rec, monkeypatch) -> None:
        """После выбора склада идёт выбор типа — порядок шагов сохранён."""
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="admin")

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_WAREHOUSE, "SMALL-P3"], 1, "cbw"
        )

        session = _session()
        assert session.step == insp.STEP_TYPE
        assert session.warehouse == "SMALL-P3"

        prompt = rec.sent[-1]
        assert "What do you want to inspect" in prompt["text"]
        flat = [
            b["callback_data"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        assert "ins:t:robot" in flat

    def test_chosen_warehouse_reaches_the_lookup(self, rec, monkeypatch) -> None:
        """🔴 Выбранный склад уходит **в реальный поиск** номера.

        Проверяется не строка в исходнике и не заглушка, а запрос, который
        уехал бы в справочник: перехватывается `sendToDataBase.rest_get` под
        **настоящей** функцией `validation.find_equipment`. На проверке «по
        исходнику» в проекте уже обжигались: из запроса убрали категорию, и все
        тесты остались зелёными.
        """
        import sendToDataBase

        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="admin")

        captured = []

        def fake_rest_get(table, params=None, optional=False):
            captured.append({"table": table, "params": dict(params or {})})
            return []

        monkeypatch.setattr(sendToDataBase, "rest_get", fake_rest_get)

        # Возвращаем **настоящую** функцию поиска поверх заглушки фикстуры:
        # иначе склад в запрос не попадёт вообще, и тест проверял бы заглушку.
        monkeypatch.setattr(validation, "find_equipment", _REAL_FIND_EQUIPMENT)

        _start_in_service_topic(rec, monkeypatch, warehouse="SMALL-P3", number="13")

        equipment_calls = [
            call for call in captured if call["table"] == "equipment"
        ]
        assert equipment_calls, "реального запроса к справочнику не было"
        assert equipment_calls[0]["params"].get("warehouse") == "eq.SMALL-P3", (
            "поиск ушёл не на выбранный склад: "
            f"{equipment_calls[0]['params']}"
        )

    def test_buttons_offer_only_known_warehouses(self, rec, monkeypatch) -> None:
        """В кнопках ровно GLP-C и SMALL-P3 — из `warehouses.WAREHOUSES`."""
        from warehouses import WAREHOUSES

        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="manager", warehouse=None)

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        prompt = rec.sent[-1]
        labels = [
            b["text"]
            for row in prompt["reply_markup"]["inline_keyboard"]
            for b in row
            if b["callback_data"] != "ins:x"
        ]
        assert labels == list(WAREHOUSES.values()), labels
        assert labels == ["GLP-C", "SMALL-P3"], labels


class TestWarehouseButtons:
    """🔴 Кнопки склада — **всегда две, без роли и без чтения карточки**.

    Решение владельца 17.10.2026, дословно: «пускай просто будет две кнопки и
    всё, не с сервера». До этого список вычислялся по роли работника и повторял
    правило API `can_access_warehouse`.

    Проверяется именно то, что просили: **две кнопки у всех**, независимо от
    роли и склада в карточке. Отказ по чужому складу теперь приходит от API —
    и приходит **сразу после выбора шаблона** (осмотр создаётся там же), а не
    после пройденных пунктов с фото. Это и делает размен допустимым.
    """

    def _employee(self, monkeypatch, **fields):
        import telegram_store

        card = {"user_name": "Felipe", "auth_id": AUTH_ID, "warehouse": "GLP-C"}
        card.update(fields)
        monkeypatch.setattr(
            telegram_store, "get_employee_context",
            lambda *a, **k: ("Felipe", dict(card)),
        )

    def test_choices_are_always_the_bot_warehouses(self) -> None:
        """Список кнопок не зависит ни от чего, кроме `TELEGRAM_WAREHOUSES`."""
        from warehouses import WAREHOUSES

        assert insp._warehouse_choices() == list(WAREHOUSES.values())
        assert insp._warehouse_choices() == ["GLP-C", "SMALL-P3"]

    def test_choices_need_no_arguments(self) -> None:
        """У функции нет аргументов — роль и карточка в неё не попадают.

        Это сторож решения владельца: если кто-то снова начнёт передавать сюда
        сотрудника, тест упадёт и заставит обсудить это явно.
        """
        import inspect as _inspect

        params = _inspect.signature(insp._warehouse_choices).parameters
        assert list(params) == [], f"у выбора складов появились аргументы: {list(params)}"

    def test_worker_sees_both_warehouses(self, rec, monkeypatch) -> None:
        """🔴 Работник GLP-C видит и SMALL-P3 — владелец просил две кнопки.

        Раньше здесь проверялось обратное. Изменение осознанное: чужой склад
        упрётся в 403, но уже **на создании осмотра**, а не после фото.
        """
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="worker", warehouse="GLP-C")

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session().step == insp.STEP_WAREHOUSE
        flat = [
            b["callback_data"]
            for row in rec.sent[0]["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        assert "ins:w:GLP-C" in flat
        assert "ins:w:SMALL-P3" in flat, "работник должен видеть обе кнопки"

    def test_shift_leader_sees_both_warehouses(self, rec, monkeypatch) -> None:
        """Лидер тоже видит обе — роль на кнопки не влияет."""
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="shift leader", warehouse="GLP-C")

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session().step == insp.STEP_WAREHOUSE

    def test_employee_without_warehouse_sees_both(self, rec, monkeypatch) -> None:
        """Карточка без склада больше не мешает: кнопки есть у всех.

        Раньше такой работник получал отказ («ask a leader to fill in the
        warehouse»), потому что список строился из карточки. Теперь список
        складов от карточки не зависит, и осмотр возможен — а отказ по доступу
        придёт от API, если человек выбрал не свой склад.
        """
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="worker", warehouse=None)

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert _session().step == insp.STEP_WAREHOUSE
        flat = [
            b["callback_data"]
            for row in rec.sent[0]["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        assert "ins:w:GLP-C" in flat
        assert "ins:w:SMALL-P3" in flat

    def test_unknown_warehouse_but_not_in_bot_is_still_offered(self, rec, monkeypatch) -> None:
        """Склад карточки вне бота (`PNT-A`) больше не ломает диалог.

        Раньше это был отказ. Теперь список — склады бота, а `PNT-A` в нём нет:
        человек видит GLP-C и SMALL-P3 и выбирает, где действительно осматривает.
        """
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="worker", warehouse="PNT-A")

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        session = _session()
        assert session is not None, "диалог не должен обрываться из-за склада карточки"
        assert session.warehouses == ["GLP-C", "SMALL-P3"]

    def test_warehouse_cancel_works_on_the_new_step(self, rec, monkeypatch) -> None:
        """`/cancel` работает на шаге склада, как и на любом другом."""
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="admin")

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        assert _session().step == insp.STEP_WAREHOUSE

        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_CANCEL], 2, "cbx"
        )

        assert _session() is None
        assert rec.answered[-1]["text"] == "Cancelled"

    def test_unknown_warehouse_callback_is_refused(self, rec, monkeypatch) -> None:
        """Кнопка устаревшего сообщения с чужим складом не подменяет выбор."""
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="admin")

        insp.start(CHAT_ID, {"id": USER_ID}, None)
        insp.handle_callback(
            CHAT_ID, {"id": USER_ID}, ["ins", insp.CB_WAREHOUSE, "PNT-A"], 1, "cbw"
        )

        assert _session().step == insp.STEP_WAREHOUSE, "чужой склад принят"
        assert rec.answered[-1]["text"] == "Unknown warehouse"

    def test_text_step_does_not_hijack_the_warehouse_step(self, rec, monkeypatch) -> None:
        """Свободный текст на шаге склада не принимается — выбор только кнопкой."""
        _error_routing(monkeypatch)
        self._employee(monkeypatch, role="admin")

        insp.start(CHAT_ID, {"id": USER_ID}, None)

        assert insp.handle_text(CHAT_ID, {"id": USER_ID}, "GLP-C", 1) is False
        assert _session().step == insp.STEP_WAREHOUSE



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
# СЛУЖЕБНЫЙ ТОПИК: ОСМОТР РАБОТАЕТ, ОШИБКА — НЕТ
# ============================================================

class TestServiceTopicDispatch:
    """`/inspection` в служебном топике `Inspection` (thread 25189).

    🔴 **Две вещи, которые легко сломать одной правкой.**

    1. **Осмотр из служебного топика должен работать полностью** — команда,
       кнопки, номер, фото пункта. Топик принимается `is_shared_topic`, а
       **ответ** уходит только потому, что `service` добавлен в
       `allowed_output_topics()`. Раньше ответ отклонил бы барьер отправки, и
       человек видел бы молчание при «сработавшей» кнопке.
    2. **Фото ОШИБКИ из служебного топика по-прежнему отвергается.**
       `error_warehouse_for_thread` для не-топика ошибок возвращает `None`, и
       ошибка из службы не должна записаться на `DEFAULT_WAREHOUSE` (GLP-C).

    Это **разные ветки** диспетчера: сначала `_inspection_pending()` (осмотр),
    и только потом проверка топика ошибок. Тесты сторожат именно этот порядок.
    """

    SERVICE_THREAD = SERVICE_THREAD_ID

    def _gates(self, monkeypatch):
        """Боевое окружение топиков + служебный топик разрешён на отправку."""
        monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
        monkeypatch.setattr(bot, "LISTEN_TOPIC_IDS", set())
        monkeypatch.setattr(bot, "LISTEN_TOPIC_NAMES", set())
        monkeypatch.setattr(bot, "_delete_user_message", lambda *a, **k: True)
        monkeypatch.setattr(bot, "_send_action", lambda *a, **k: None)
        _error_routing(monkeypatch)

        # Администратор: доступны оба склада, значит в службе спросят склад.
        import telegram_store

        monkeypatch.setattr(
            telegram_store, "get_employee_context",
            lambda *a, **k: ("Dmytro", {
                "user_name": "Dmytro", "auth_id": AUTH_ID,
                "warehouse": "GLP-C", "role": "admin", "is_leader": True,
            }),
        )

    def _message(self, text=None, photo=False, message_id=900, thread_id=None):
        thread_id = self.SERVICE_THREAD if thread_id is None else thread_id
        message = {
            "message_id": message_id,
            "from": {"id": USER_ID, "is_bot": False, "username": "t",
                     "first_name": "T"},
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "date": int(__import__("time").time()),
            "message_thread_id": thread_id,
            "is_topic_message": True,
        }

        if text is not None:
            message["text"] = text

        if photo:
            message["photo"] = [
                {"file_id": "small", "file_unique_id": "s"},
                {"file_id": "big", "file_unique_id": "b"},
            ]

        return {"update_id": message_id, "message": message}

    def _callback(self, data, message_id=901):
        return {
            "update_id": message_id,
            "callback_query": {
                "id": f"cb{message_id}",
                "from": {"id": USER_ID, "is_bot": False, "username": "t",
                         "first_name": "T"},
                "message": {
                    "message_id": message_id,
                    "chat": {"id": CHAT_ID, "type": "supergroup"},
                    "from": {"id": 1, "is_bot": True},
                    "message_thread_id": self.SERVICE_THREAD,
                },
                "data": data,
            },
        }

    def test_service_topic_is_shared_and_allowed_to_answer(self, monkeypatch) -> None:
        """🔴 Топик 25189 принимается **и** разрешён на отправку.

        Первое давал `is_shared_topic` и раньше, второго не было — именно это и
        чинилось (`allowed_output_topics`).
        """
        _error_routing(monkeypatch)

        assert bot.is_shared_topic(CHAT_ID, self.SERVICE_THREAD) is True
        # `topic_allowed` отвечает на другой вопрос («брать ли входящее») и может
        # быть False — в диспетчере участвует пара `allowed or shared`.
        assert bot._output_topic_allowed(CHAT_ID, self.SERVICE_THREAD) is True, (
            "ответ в служебный топик отклонён: осмотр ответил бы молчанием"
        )

    def test_foreign_topic_is_still_silent(self, monkeypatch) -> None:
        """Чужой топик по-прежнему закрыт на отправку — защита не ослаблена."""
        _error_routing(monkeypatch)

        assert bot._output_topic_allowed(CHAT_ID, 777) is False
        assert bot._output_topic_allowed(CHAT_ID, None) is False
        assert not bot.is_shared_topic(CHAT_ID, 777)

    def test_error_photo_is_rejected_in_the_service_topic(self, monkeypatch) -> None:
        """🔴 Фото **ошибки** из служебного топика не принимается.

        Топик ошибок не определён → `error_warehouse_for_thread` отдаёт `None`,
        и приём ошибок обязан отказаться: иначе запись ушла бы на
        `DEFAULT_WAREHOUSE` (GLP-C) не с того склада.
        """
        _error_routing(monkeypatch)

        assert bot.error_warehouse_for_thread(CHAT_ID, self.SERVICE_THREAD) is None

        # Диалога осмотра нет — фото идёт в приём ошибок и получает отказ.
        assert insp.peek_session(CHAT_ID, USER_ID) is None

        import unittest.mock as mock

        monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
        with mock.patch.object(bot, "handle_photo") as photo, \
             mock.patch.object(bot, "_handle_wrong_topic") as wrong:
            bot._handle_update_inner(
                self._message(photo=True, message_id=3101), "TestBot"
            )

        photo.assert_not_called()
        assert wrong.called, "оператору не сказали, что топик не тот"

    def test_inspection_photo_works_in_the_service_topic(self, rec, monkeypatch) -> None:
        """🔴 Фото **пункта осмотра** из служебного топика доходит до диалога.

        Порядок веток решает: осмотр проверяется **раньше** топика ошибок,
        иначе снимок пункта ушёл бы в Lark как новая ошибка.
        """
        self._gates(monkeypatch)

        monkeypatch.setattr(bot.tg, "get_file", lambda file_id: "photos/f.jpg")
        monkeypatch.setattr(
            bot.tg, "download_file", lambda path, dest: _touch(dest)
        )
        monkeypatch.setattr(
            validation, "find_equipment",
            lambda kind, warehouse, code: dict(ROBOT),
        )

        bot.handle_update(self._message(text="/inspection", message_id=2001), "TestBot")
        assert _session() is not None, "команда не дошла из служебного топика"
        assert _session().step == insp.STEP_WAREHOUSE

        bot.handle_update(self._callback("ins:w:GLP-C", message_id=2002), "TestBot")
        bot.handle_update(self._callback("ins:t:robot", message_id=2003), "TestBot")
        bot.handle_update(self._message(text="3780", message_id=2004), "TestBot")

        assert _session().step == insp.STEP_TEMPLATE

        _choose_template(rec, monkeypatch)
        bot.handle_update(self._callback("ins:k:1", message_id=2005), "TestBot")

        saved = []
        monkeypatch.setattr(
            insp, "upload_item_photo", lambda *a, **k: saved.append(1) or {}
        )
        monkeypatch.setattr(insp, "set_item_verdict", lambda *a, **k: {})

        bot.handle_update(self._message(photo=True, message_id=2006), "TestBot")

        assert saved, "фото пункта не дошло — его перехватил приём ошибок"
        assert _session().item_index == 1

        # Ответы ушли именно в служебный топик, а не в чужой.
        assert rec.sent, "бот не отправил ни одного сообщения"
        assert all(m["thread_id"] == self.SERVICE_THREAD for m in rec.sent), (
            "ответ ушёл не в служебный топик: "
            f"{[m['thread_id'] for m in rec.sent]}"
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


# ============================================================
# ТОКЕН И ЕГО ЗАВИСИМОСТИ (17.10.2026)
# ============================================================

class TestTokenDependencies:
    """🔴 Осмотр не должен обвинять карточку работника, когда виноват не он.

    Реальный случай владельца 17.10.2026: в Telegram пришло

        ⚠️ Your Telegram account is not linked to a system user (no auth_id)

    хотя **и привязка, и `auth_id` были на месте**. Не хватало библиотеки
    **PyJWT** в контейнере бота: локально она стояла (её поставили руками при
    разработке), а в `requirements.txt` её не было — и `Dockerfile` ставит
    только этот файл. `issue_token` возвращал `None`, а сообщение обвиняло
    `auth_id`.

    Цена ошибки: человек пошёл бы чинить **карточку сотрудника**, то есть не туда.
    """

    def test_pyjwt_is_declared_in_requirements(self) -> None:
        """PyJWT объявлен в `requirements.txt` — иначе прод не подпишет токен.

        Это **сторож упаковки**, а не кода: локально библиотека может стоять, и
        все тесты будут зелёными, а контейнер соберётся без неё. Именно так
        дефект и дожил до прода.
        """
        from pathlib import Path

        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        text = requirements.read_text(encoding="utf-8").lower()

        assert "pyjwt" in text, (
            "PyJWT не объявлен в requirements.txt — контейнер бота соберётся "
            "без него, и /inspection не сможет выпустить токен"
        )

    def test_pyjwt_is_importable_here(self) -> None:
        """И он действительно импортируется — объявление соответствует установке."""
        assert inspection_api._import_jwt() is not None

    def test_missing_library_names_the_library_not_the_card(self, monkeypatch) -> None:
        """🔴 Нет библиотеки — сообщение говорит про библиотеку, не про `auth_id`."""
        monkeypatch.setattr(inspection_api, "_import_jwt", lambda: None)

        reason = inspection_api.token_failure_reason("79fdec66-66a5-4935-9d22-efbc15a2e3e8")

        assert reason is not None
        assert "PyJWT" in reason
        assert "auth_id" not in reason, (
            "нельзя обвинять карточку работника, когда причина в библиотеке"
        )

    def test_missing_secret_names_the_secret(self, monkeypatch) -> None:
        """Нет секрета — сообщение говорит про секрет, не про карточку."""
        monkeypatch.setattr(inspection_api, "JWT_SECRET", "")

        reason = inspection_api.token_failure_reason("79fdec66-66a5-4935-9d22-efbc15a2e3e8")

        assert reason is not None
        assert "SUPABASE_JWT_SECRET" in reason
        assert "auth_id" not in reason

    def test_missing_auth_id_names_the_card(self) -> None:
        """А вот когда `auth_id` действительно нет — говорим именно про него."""
        reason = inspection_api.token_failure_reason("")

        assert reason is not None
        assert "auth_id" in reason

    def test_working_setup_has_no_reason(self) -> None:
        """Когда всё на месте, причины нет — и токен выпускается."""
        auth_id = "79fdec66-66a5-4935-9d22-efbc15a2e3e8"

        assert inspection_api.token_failure_reason(auth_id) is None
        assert inspection_api.issue_token(auth_id)

    def test_request_raises_with_the_real_reason(self, monkeypatch) -> None:
        """`_request` не подменяет причину своим текстом про `auth_id`."""
        monkeypatch.setattr(inspection_api, "_import_jwt", lambda: None)
        monkeypatch.setattr(inspection_api, "API_BASE", "http://api.test")

        with pytest.raises(inspection_api.InspectionApiUnavailable) as exc:
            inspection_api._request("GET", "/v1/inspections", "some-auth-id")

        assert "PyJWT" in str(exc.value)
