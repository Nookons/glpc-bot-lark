"""
Offline-проверка Telegram-бота: никаких сетевых вызовов.

Telegram API, Supabase и Lark-хук подменяются заглушками, поэтому
тест можно запускать локально без токена и без интернета:

    python3 tests/test_telegram_offline.py

Проверяет разбор команд, регистрацию сотрудника, основной поток
сохранения ошибки, алерт по порогу, фото и защиту от дублей.
"""

import os
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Картинки из тестов не должны попадать в рабочий каталог.
os.environ["IMAGES_DIR"] = tempfile.mkdtemp(prefix="glpc-tg-test-")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")
os.environ["TELEGRAM_TOPIC_ID"] = ""
os.environ["TELEGRAM_TOPIC_NAME"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""

import telegram_api as tg  # noqa: E402
import telegram_bot as bot  # noqa: E402


# ============================================================
# HARNESS
# ============================================================

RESULTS = []


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("PASS" if ok else "FAIL"), "-", name, "" if ok else "| " + str(detail))


class DummyConsole:
    def print(self, *args, **kwargs):
        pass


bot.console = DummyConsole()

# main() выставляет username бота до запуска polling.
bot.BOT_USERNAME = "TestBot"


# ============================================================
# STUBS
# ============================================================

SENT = []
LINKS = {}
DB_CALLS = []
FORWARDED = []
PHOTO_CALLS = []


def fake_send_message(
    chat_id,
    text,
    reply_to_message_id=None,
    disable_notification=False,
    message_thread_id=None,
):
    SENT.append({"chat_id": chat_id, "text": text, "thread_id": message_thread_id})
    return {"message_id": len(SENT)}


def fake_send_chat_action(*args, **kwargs):
    return True


def fake_get_file(file_id):
    return "photos/file_1.jpg"


def fake_download_file(file_path, destination):
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

    with open(destination, "wb") as f:
        f.write(b"fake-image")

    return destination


def fake_get_employee_name(telegram_id):
    return LINKS.get(telegram_id)


def fake_resolve_employee_name(raw_name):
    if raw_name.strip().casefold() == "ivan":
        return "Ivan Petrenko", []

    return None, ["Ivan Petrenko", "Ivanov Petr"]


def fake_link_user(telegram_id, username, employee_name):
    LINKS[telegram_id] = employee_name
    return True


def fake_unlink_user(telegram_id):
    LINKS.pop(telegram_id, None)
    return True


def fake_send_to_data_base(parsed, data_obj, chat_id):
    DB_CALLS.append({"parsed": parsed, "data": data_obj, "chat_id": chat_id})
    return [{"id": 1}]


def fake_count_robot_errors_in_shift(robot, shift_date, shift_name):
    return COUNTS.get(str(robot), 0)


def fake_forward_error(parsed, table_lines=None):
    FORWARDED.append({"parsed": parsed, "lines": table_lines})
    return True


def fake_handle_incoming_photo(image_path, console=None, caption=None):
    PHOTO_CALLS.append({"path": image_path, "caption": caption})
    return "lark"


def fake_shift_stats(shift_date, shift_name):
    return 2, {"3780": 2}, {"Security module failure": 2}


COUNTS = {}

tg.send_message = fake_send_message
tg.send_chat_action = fake_send_chat_action
tg.get_file = fake_get_file
tg.download_file = fake_download_file

bot.get_employee_name = fake_get_employee_name
bot.resolve_employee_name = fake_resolve_employee_name
bot.link_user = fake_link_user
bot.unlink_user = fake_unlink_user
bot.send_to_data_base = fake_send_to_data_base
bot.count_robot_errors_in_shift = fake_count_robot_errors_in_shift
bot.forward_error = fake_forward_error
bot.handle_incoming_photo = fake_handle_incoming_photo
bot.shift_stats = fake_shift_stats

import sendToDataBase as stdb  # noqa: E402

# Как в main(): ответы пользователю (алерты/подтверждения) идут в Telegram
# через _send — с автоопределением топика.
def notifier(chat_id, text):
    return bot._send(chat_id, text)


stdb.set_notifier(notifier)


MESSAGE_SEQ = [1000]

# update_id последнего сгенерированного апдейта (для проверки offset)
SENT_UPDATE_ID = 0


def make_update(
    text=None,
    user_id=100,
    chat_id=-500,
    date=None,
    photo=False,
    caption=None,
    message_id=None,
    thread_id=None,
    topic_created=None,
):
    MESSAGE_SEQ[0] += 1

    message = {
        "message_id": message_id if message_id is not None else MESSAGE_SEQ[0],
        "from": {
            "id": user_id,
            "is_bot": False,
            "username": "tester",
            "first_name": "Tester",
        },
        "chat": {"id": chat_id, "type": "supergroup"},
        "date": int(time.time()) if date is None else date,
    }

    if text is not None:
        message["text"] = text

    if photo:
        message["photo"] = [
            {"file_id": "small", "file_unique_id": "small-id"},
            {"file_id": "big", "file_unique_id": "big-id"},
        ]

    if caption is not None:
        message["caption"] = caption

    if thread_id is not None:
        message["message_thread_id"] = thread_id
        message["is_topic_message"] = True

    if topic_created is not None:
        message["forum_topic_created"] = {"name": topic_created}

    global SENT_UPDATE_ID
    SENT_UPDATE_ID = MESSAGE_SEQ[0]

    return {"update_id": MESSAGE_SEQ[0], "message": message}


def run(update):
    stdb.set_notifier(notifier)
    SENT.clear()
    DB_CALLS.clear()
    FORWARDED.clear()
    PHOTO_CALLS.clear()
    bot.handle_update(update, "TestBot")
    return list(SENT)


# ============================================================
# TESTS
# ============================================================

def test_parse_command():
    check(
        "parse_command: /reg@TestBot Ivan",
        bot.parse_command("/reg@TestBot Ivan", "TestBot") == ("reg", "Ivan"),
    )
    check(
        "parse_command: /reg for another bot is ignored",
        bot.parse_command("/reg@OtherBot Ivan", "TestBot") == (None, ""),
    )
    check(
        "parse_command: plain /help",
        bot.parse_command("/help", "TestBot") == ("help", ""),
    )
    check(
        "parse_command: not a command",
        bot.parse_command("Unable to drive: X. 1", "TestBot") == (None, ""),
    )


def test_message_age():
    now = time.time()
    check("age: fresh message", bot._is_message_too_old(int(now)) is False)
    check("age: old message", bot._is_message_too_old(int(now) - 3600) is True)
    check("age: missing date", bot._is_message_too_old(None) is False)


def test_unregistered_text():
    LINKS.clear()
    sent = run(make_update(text="Unable to drive: Security module failure. 3780"))

    check(
        "unregistered: получает подсказку /reg",
        len(sent) == 1 and "not registered" in sent[0]["text"],
        sent,
    )
    check("unregistered: в БД не пишем", DB_CALLS == [], DB_CALLS)


def test_registration():
    LINKS.clear()
    sent = run(make_update(text="/reg Ivan"))

    check(
        "reg: привязка сохранена",
        LINKS.get(100) == "Ivan Petrenko",
        LINKS,
    )
    check(
        "reg: подтверждение с именем сотрудника",
        len(sent) == 1 and "Ivan Petrenko" in sent[0]["text"] and "✅" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/reg Vasya"))
    check(
        "reg: неизвестное имя -> подсказки",
        len(sent) == 1 and "Did you mean" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/reg"))
    check(
        "reg: без аргумента -> usage",
        len(sent) == 1 and "Usage: /reg" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/whoami"))
    check(
        "whoami: показывает привязку",
        len(sent) == 1 and "Ivan Petrenko" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/unreg"))
    check(
        "unreg: привязка удалена",
        LINKS.get(100) is None and "removed" in sent[0]["text"],
        (LINKS, sent),
    )


def test_valid_error_flow():
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    sent = run(make_update(text="Unable to drive: Security module failure. 3780"))

    check("valid: ошибка записана в БД", len(DB_CALLS) == 1, DB_CALLS)
    check(
        "valid: в БД ушло имя из привязки",
        DB_CALLS and DB_CALLS[0]["data"]["employee"] == "Ivan Petrenko",
        DB_CALLS,
    )
    check(
        "valid: робот распарсен",
        DB_CALLS and DB_CALLS[0]["parsed"]["robot"] == "3780",
        DB_CALLS,
    )
    check("valid: карточка ушла в Lark-хук", len(FORWARDED) == 1, FORWARDED)
    check(
        "valid: карточка содержит имя и счётчик смены",
        FORWARDED
        and ("👤 Employee", "Ivan Petrenko") in FORWARDED[0]["lines"]
        and ("📊 Shift issues", "1") in FORWARDED[0]["lines"],
        FORWARDED,
    )
    check(
        "valid: подтверждение в Telegram (не Lark API)",
        len(sent) == 1 and sent[0]["text"].startswith("✅ Saved"),
        sent,
    )


def test_threshold_alert():
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 3

    sent = run(make_update(text="Unable to drive: Security module failure. 3780"))

    check(
        "threshold: алерт про обслуживание",
        len(sent) == 1 and "sent to maintenance" in sent[0]["text"],
        sent,
    )


def test_bad_format():
    LINKS[100] = "Ivan Petrenko"

    sent = run(make_update(text="just a random message"))
    check(
        "bad format: подсказка формата",
        len(sent) == 1 and "Can't parse" in sent[0]["text"],
        sent,
    )
    check("bad format: в БД не пишем", DB_CALLS == [], DB_CALLS)

    sent = run(make_update(text="Unable to drive: Security module failure. ABC"))
    check(
        "non-numeric robot: отдельная подсказка",
        len(sent) == 1 and "must be digits" in sent[0]["text"],
        sent,
    )
    check("non-numeric robot: в БД не пишем", DB_CALLS == [], DB_CALLS)


def test_not_saved_no_forward():
    LINKS[100] = "Ivan Petrenko"

    original = bot.send_to_data_base
    bot.send_to_data_base = lambda parsed, data_obj, chat_id: None

    try:
        sent = run(make_update(text="Unable to drive: Security module failure. 3780"))
    finally:
        bot.send_to_data_base = original

    check("not saved: карточка в Lark не уходит", FORWARDED == [], FORWARDED)
    check("not saved: подтверждения нет", sent == [], sent)


def test_photo_flow():
    LINKS[100] = "Ivan Petrenko"

    sent = run(make_update(photo=True, caption="broken robot"))

    check("photo: фото обработано", len(PHOTO_CALLS) == 1, PHOTO_CALLS)
    check(
        "photo: подпись с именем сотрудника",
        PHOTO_CALLS and PHOTO_CALLS[0]["caption"] == "📷 Photo from Ivan Petrenko",
        PHOTO_CALLS,
    )
    check(
        "photo: подтверждение отправки в Lark",
        len(sent) == 1 and "Photo forwarded" in sent[0]["text"],
        sent,
    )


def test_photo_delivery_modes():
    LINKS[100] = "Ivan Petrenko"

    original = bot.handle_incoming_photo
    called = []

    try:
        for mode, marker in (
            ("lark", "Photo forwarded to Lark"),
            ("link", "as a link"),
            ("none", "Can\'t forward the photo"),
        ):
            bot.handle_incoming_photo = (
                lambda image_path, console=None, caption=None, _mode=mode: (
                    called.append(_mode),
                    _mode,
                )[1]
            )
            sent = run(make_update(photo=True, caption="broken robot", thread_id=42))
            check(
                f"photo mode={mode}: ответ в Telegram",
                len(sent) == 1 and marker in sent[0]["text"],
                sent,
            )
            check(
                f"photo mode={mode}: ответ ушёл в топик сообщения",
                sent and sent[0]["thread_id"] == 42,
                sent,
            )
    finally:
        bot.handle_incoming_photo = original

    check(
        "photo: обработчик вызывается во всех трёх режимах",
        called == ["lark", "link", "none"],
        called,
    )


def test_photo_fallback_logic():
    """pending_photos: Lark -> Storage-ссылка -> отказ."""
    import pending_photos as pp

    hooks = []

    original_upload = pp.upload_image
    original_url = pp.upload_photo_and_get_url
    original_text = pp.send_text_via_hook
    original_image = pp.send_image_via_hook
    original_post = pp.send_post_via_hook

    pp.send_text_via_hook = lambda url, text: (hooks.append(("text", text)), {"code": 0})[1]
    pp.send_image_via_hook = lambda url, key: (hooks.append(("image", key)), {"code": 0})[1]
    pp.send_post_via_hook = lambda url, key, text: (hooks.append(("post", key, text)), {"code": 0})[1]

    try:
        # 1) Штатный путь: Lark принимает картинку.
        hooks.clear()
        pp.upload_image = lambda path: "img_key_1"
        mode = pp.handle_incoming_photo("x.jpg", caption="📷 Photo from Ivan")
        check("fallback: штатный путь отдаёт картинку", mode == "lark", mode)
        check(
            "fallback: картинка ушла через post-хук",
            hooks and hooks[0][0] == "post" and hooks[0][1] == "img_key_1",
            hooks,
        )

        # 2) Квота исчерпана -> ссылка через Supabase Storage.
        hooks.clear()

        def quota_error(path):
            raise RuntimeError(
                "Failed to upload image: {'code': 99991403, "
                "'msg': \"This month's API call quota has been exceeded\"}"
            )

        pp.upload_image = quota_error
        pp.upload_photo_and_get_url = (
            lambda path, object_name=None: "https://storage.example/link.jpg"
        )
        mode = pp.handle_incoming_photo("x.jpg", caption="📷 Photo from Ivan")
        check("fallback: при ошибке квоты режим link", mode == "link", mode)
        check(
            "fallback: в сообщении есть подпись и ссылка",
            hooks
            and hooks[0][0] == "text"
            and "📷 Photo from Ivan" in hooks[0][1]
            and "https://storage.example/link.jpg" in hooks[0][1],
            hooks,
        )

        # 3) Ни Lark, ни Storage.
        hooks.clear()
        pp.upload_photo_and_get_url = lambda path, object_name=None: None
        check(
            "fallback: обе дороги недоступны -> none",
            pp.handle_incoming_photo("x.jpg") == "none",
        )

        # 4) Ошибка вебхука после успешной загрузки -> тоже пробуем ссылкой.
        hooks.clear()
        pp.upload_image = lambda path: "img_key_2"
        pp.send_post_via_hook = lambda url, key, text: {"code": 9499, "msg": "bad"}
        pp.upload_photo_and_get_url = (
            lambda path, object_name=None: "https://storage.example/link2.jpg"
        )
        mode = pp.handle_incoming_photo("x.jpg", caption="cap")
        check("fallback: вебхук упал -> ссылка", mode == "link", mode)
    finally:
        pp.upload_image = original_upload
        pp.upload_photo_and_get_url = original_url
        pp.send_text_via_hook = original_text
        pp.send_image_via_hook = original_image
        pp.send_post_via_hook = original_post


def test_ignored_cases():
    LINKS[100] = "Ivan Petrenko"

    sent = run(make_update(text="/reg@OtherBot Ivan"))
    check("другая команда другого бота: тишина", sent == [], sent)

    sent = run(make_update(text="Unable to drive: X. 3780", date=int(time.time()) - 7200))
    check("старое сообщение: пропущено", sent == [], sent)

    original_allowed = bot.ALLOWED_CHAT_IDS
    bot.ALLOWED_CHAT_IDS = {999}

    try:
        sent = run(make_update(text="Unable to drive: X. 3780"))
    finally:
        bot.ALLOWED_CHAT_IDS = original_allowed

    check("чат не в allow-list: пропущено", sent == [], sent)

    dup_id = 777001
    run(make_update(text="/help", message_id=dup_id))
    first_count = len(SENT)
    sent = run(make_update(text="/help", message_id=dup_id))
    check(
        "дубликат message_id: не обрабатываем дважды",
        sent == [] and first_count > 0,
        (sent, first_count),
    )


def test_commands():
    sent = run(make_update(text="/help"))
    check("help: текст помощи", len(sent) == 1 and "Robot exception bot" in sent[0]["text"], sent)

    sent = run(make_update(text="/id"))
    check(
        "id: chat_id и user_id",
        len(sent) == 1 and "chat_id: -500" in sent[0]["text"] and "user_id: 100" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/stats"))
    check(
        "stats: статистика смены",
        len(sent) == 1 and "Shift statistics" in sent[0]["text"] and "Total exceptions: 2" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/foo"))
    check("unknown: подсказка", len(sent) == 1 and "Unknown command" in sent[0]["text"], sent)


def test_supabase_notifier():
    captured = []
    stdb.set_notifier(lambda chat_id, text: captured.append((chat_id, text)))
    stdb.notify_user(-500, "hello")

    check(
        "notifier: ответы уходят в Telegram-функцию",
        captured == [(-500, "hello")],
        captured,
    )

    lark_calls = []
    original = stdb.send_text_message
    stdb.send_text_message = lambda chat_id, text: lark_calls.append((chat_id, text))

    try:
        stdb.set_notifier(None)
        stdb.notify_user(-500, "fallback")
    finally:
        stdb.send_text_message = original
        stdb.set_notifier(notifier)

    check(
        "notifier: без notifier используется старый Lark-путь",
        lark_calls == [(-500, "fallback")],
        lark_calls,
    )


def test_lark_hook_payload():
    import pending_photos as pp

    ok = pp._hook_ok({"code": 0, "msg": "success"})
    ok_old = pp._hook_ok({"StatusCode": 0, "StatusMessage": "success"})
    bad = pp._hook_ok({"code": 9499, "msg": "Bad Request"})
    empty = pp._hook_ok(None)

    check(
        "hook: распознаёт успех в обеих схемах ответа",
        ok and ok_old and not bad and not empty,
        (ok, ok_old, bad, empty),
    )


def test_polling_loop_and_offset():
    """polling_loop: обрабатывает апдейт и двигает offset."""
    import threading

    stop = threading.Event()
    offsets = []
    updates = [make_update(text="/help")]

    def fake_get_updates(offset=None, timeout=30):
        offsets.append(offset)

        if updates:
            return [updates.pop(0)]

        stop.set()
        return []

    original = tg.get_updates
    tg.get_updates = fake_get_updates

    try:
        SENT.clear()
        stdb.set_notifier(fake_send_message)
        bot.BOT_USERNAME = "TestBot"
        bot.polling_loop(stop)
    finally:
        tg.get_updates = original

    check(
        "polling: апдейт обработан (help отправлен)",
        len(SENT) == 1 and "Robot exception bot" in SENT[0]["text"],
        SENT,
    )
    check(
        "polling: offset сдвинут на update_id + 1",
        len(offsets) >= 2 and offsets[0] is None and offsets[1] == SENT_UPDATE_ID + 1,
        offsets,
    )


def test_flask_endpoints():
    """Railway healthcheck и /shift_stats."""
    client = bot.app.test_client()

    response = client.get("/health")
    payload = response.get_json()

    check(
        "flask: /health отвечает ok",
        response.status_code == 200 and payload.get("status") == "ok",
        (response.status_code, payload),
    )

    response = client.get("/shift_stats?date=2026-03-08&shift=day")
    payload = response.get_json()

    check(
        "flask: /shift_stats отдаёт метрики смены",
        response.status_code == 200
        and payload.get("total_errors") == 2
        and payload.get("shift_name") == "day",
        (response.status_code, payload),
    )


# ============================================================
# FORUM TOPICS
# ============================================================

def reset_topics(topic_id=None, topic_name=""):
    """Готовит состояние фильтра топиков для теста."""
    bot.TELEGRAM_TOPIC_ID = topic_id
    bot.TELEGRAM_TOPIC_NAME = topic_name
    bot._topic_names.clear()
    bot._routes.clear()
    bot._hinted_threads.clear()


def test_topic_filter_by_id():
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1
    reset_topics(topic_id=555)

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=555,
    ))
    check("topic id: сообщение из нужного топика обработано", len(DB_CALLS) == 1, DB_CALLS)
    check("topic id: карточка ушла в Lark", len(FORWARDED) == 1, FORWARDED)
    check(
        "topic id: ответ ушёл в тот же топик",
        all(item["thread_id"] == 555 for item in sent),
        sent,
    )

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=777,
    ))
    check(
        "topic id: чужой топик не обрабатываем",
        DB_CALLS == [] and FORWARDED == [],
        (DB_CALLS, FORWARDED),
    )
    check(
        "topic id: в чужом топике одна подсказка",
        len(sent) == 1 and "not monitored" in sent[0]["text"],
        sent,
    )
    check("topic id: подсказка ушла в чужой топик", sent and sent[0]["thread_id"] == 777, sent)

    sent = run(make_update(text="Unable to drive: Security module failure. 3780"))
    check(
        "topic id: General без thread_id игнорируется",
        DB_CALLS == [] and len(sent) == 1,
        (DB_CALLS, sent),
    )

    reset_topics()


def test_topic_filter_by_name():
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1
    reset_topics(topic_name="Ex GLPC")

    # Имя топика бот узнаёт из сервисного сообщения о создании топика.
    run(make_update(thread_id=42, topic_created="Ex GLPC"))
    check(
        "topic name: имя выучено из сервисного сообщения",
        bot.topic_name(-500, 42) == "Ex GLPC",
        bot._topic_names,
    )

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=42,
    ))
    check("topic name: сообщение из Ex GLPC обработано", len(DB_CALLS) == 1, DB_CALLS)
    check("topic name: ответ ушёл в топик", sent and sent[0]["thread_id"] == 42, sent)

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=43,
    ))
    check("topic name: неизвестный топик не обрабатываем", DB_CALLS == [], DB_CALLS)
    check(
        "topic name: подсказка про /id и TELEGRAM_TOPIC_ID",
        len(sent) == 1 and "/id" in sent[0]["text"] and "TELEGRAM_TOPIC_ID" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=43,
    ))
    check("topic name: подсказка не дублируется", sent == [], sent)

    reset_topics()


def test_commands_work_in_any_topic():
    reset_topics(topic_id=555)

    sent = run(make_update(text="/id", thread_id=777))

    check(
        "команды: /id работает в чужом топике",
        len(sent) == 1 and "message_thread_id: 777" in sent[0]["text"],
        sent,
    )
    check(
        "команды: /id сообщает, что топик не отслеживается",
        sent and "ignored" in sent[0]["text"] and "topic id 555" in sent[0]["text"],
        sent,
    )
    check("команды: ответ ушёл в топик запроса", sent and sent[0]["thread_id"] == 777, sent)

    sent = run(make_update(text="/help", thread_id=777))
    check(
        "команды: /help доступен везде и показывает топик",
        len(sent) == 1 and "Monitored: topic id 555" in sent[0]["text"],
        sent,
    )

    # /reg тоже работает в любом топике (привязка не зависит от топика).
    sent = run(make_update(text="/reg Ivan", thread_id=777))
    check(
        "команды: /reg работает в чужом топике",
        LINKS.get(100) == "Ivan Petrenko",
        LINKS,
    )

    reset_topics()


def test_command_normalization():
    # «куй» = «req» в русской раскладке, а «req» — псевдоним «reg».
    check("раскладка: /куй -> reg", bot.normalize_command("куй") == "reg")
    check("раскладка: /куп -> reg", bot.normalize_command("куп") == "reg")
    check("раскладка: /рудз -> help", bot.normalize_command("рудз") == "help")
    check("регистр: /REG -> reg", bot.normalize_command("REG") == "reg")
    check("латиница не портится", bot.normalize_command("whoami") == "whoami")

    check("псевдоним: /req -> reg", bot.normalize_command("req") == "reg")
    check("псевдоним: /register -> reg", bot.normalize_command("register") == "reg")
    check("псевдоним: /stat -> stats", bot.normalize_command("stat") == "stats")
    check("псевдонимов у /unreg нет", bot.normalize_command("unregs") == "unregs")

    check("подсказка: stas -> stats", bot.suggest_command("stas") == "stats")
    check("подсказка: whoam -> whoami", bot.suggest_command("whoam") == "whoami")
    check("подсказка: мусор не угадывается", bot.suggest_command("zzzzzz") is None)


def test_alias_req_registers():
    """Частая опечатка /req должна работать как /reg."""
    LINKS.clear()

    sent = run(make_update(text="/req Ivan"))

    check("псевдоним: /req выполнил привязку", LINKS.get(100) == "Ivan Petrenko", LINKS)
    check(
        "псевдоним: подтверждение привязки",
        len(sent) == 1 and "Ivan Petrenko" in sent[0]["text"],
        sent,
    )

    LINKS.clear()


def test_unknown_command_suggests():
    LINKS[100] = "Ivan Petrenko"

    sent = run(make_update(text="/stas"))
    check(
        "unknown: подсказка про /stats",
        len(sent) == 1 and "Did you mean /stats?" in sent[0]["text"],
        sent,
    )
    check(
        "unknown: без простыни help",
        sent and "Robot exception bot" not in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/zzzzzz"))
    check(
        "unknown: без похожих — отправляем в /help",
        len(sent) == 1 and "Send /help" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/unregs"))
    check(
        "unknown: опасная команда не угадывается молча",
        len(sent) == 1 and "Did you mean /unreg?" in sent[0]["text"] and LINKS.get(100) == "Ivan Petrenko",
        (sent, LINKS),
    )


def test_cyrillic_layout_command_works():
    LINKS.clear()

    sent = run(make_update(text="/куп Ivan"))

    check(
        "раскладка: /куп выполнил /reg",
        LINKS.get(100) == "Ivan Petrenko",
        LINKS,
    )
    check(
        "раскладка: подтверждение привязки",
        len(sent) == 1 and "Ivan Petrenko" in sent[0]["text"],
        sent,
    )

    LINKS.clear()


def test_send_fallback_to_monitored_topic():
    reset_topics(topic_id=555)

    original_allowed = bot.ALLOWED_CHAT_IDS
    original_send = tg.send_message
    bot.ALLOWED_CHAT_IDS = {-500}

    calls = []

    def flaky(chat_id, text, reply_to_message_id=None, disable_notification=False, message_thread_id=None):
        calls.append(message_thread_id)

        if message_thread_id is None:
            return None  # имитируем TOPIC_CLOSED для General

        SENT.append({"chat_id": chat_id, "text": text, "thread_id": message_thread_id})
        return {"message_id": 1}

    tg.send_message = flaky

    try:
        SENT.clear()
        result = bot._send(-500, "привет")
    finally:
        tg.send_message = original_send
        bot.ALLOWED_CHAT_IDS = original_allowed
        reset_topics()

    check("fallback: первая попытка была в General", calls and calls[0] is None, calls)
    check("fallback: повтор в отслеживаемый топик", calls and calls[-1] == 555, calls)
    check(
        "fallback: ответ доставлен",
        result is not None and len(SENT) == 1 and SENT[0]["thread_id"] == 555,
        (result, SENT),
    )


def test_send_fallback_guarded_by_allow_list():
    """Без чата в белом списке ответ не должен улетать в группу."""
    reset_topics(topic_id=555)

    original_allowed = bot.ALLOWED_CHAT_IDS
    original_send = tg.send_message
    bot.ALLOWED_CHAT_IDS = {999}

    calls = []

    def always_fail(chat_id, text, reply_to_message_id=None, disable_notification=False, message_thread_id=None):
        calls.append(message_thread_id)
        return None

    tg.send_message = always_fail

    try:
        result = bot._send(-500, "привет")
    finally:
        tg.send_message = original_send
        bot.ALLOWED_CHAT_IDS = original_allowed
        reset_topics()

    check(
        "fallback: чужие чаты не получают ответ в группу",
        result is None and calls == [None],
        calls,
    )


def test_allow_list_bootstrap_commands():
    """Вне белого списка чатов /id и /help всё равно отвечают."""
    reset_topics()
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    original_allowed = bot.ALLOWED_CHAT_IDS
    bot.ALLOWED_CHAT_IDS = {999}

    try:
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3780",
            chat_id=-500,
        ))
        check(
            "allow-list: содержимое из чужого чата игнорируется",
            sent == [] and DB_CALLS == [],
            (sent, DB_CALLS),
        )

        sent = run(make_update(text="/id", chat_id=-500))
        check(
            "allow-list: /id отвечает и показывает chat_id",
            len(sent) == 1 and "chat_id: -500" in sent[0]["text"],
            sent,
        )

        sent = run(make_update(text="/help", chat_id=-500))
        check("allow-list: /help отвечает", len(sent) == 1, sent)

        sent = run(make_update(text="/reg Ivan", chat_id=-500))
        check(
            "allow-list: /reg из чужого чата не выполняется",
            sent == [] and LINKS.get(100) == "Ivan Petrenko",
            (sent, LINKS),
        )
    finally:
        bot.ALLOWED_CHAT_IDS = original_allowed

    reset_topics()


def test_topic_not_configured():
    reset_topics()
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    sent = run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=99,
    ))

    check("без фильтра: любой топик обрабатывается", len(DB_CALLS) == 1, DB_CALLS)
    check(
        "без фильтра: мониторится any topic",
        bot.monitored_topic_label() == "any topic",
        bot.monitored_topic_label(),
    )
    check(
        "без фильтра: ответ без message_thread_id не ломает отправку",
        sent and sent[0]["thread_id"] == 99,
        sent,
    )


def main():
    tests = [
        test_parse_command,
        test_message_age,
        test_unregistered_text,
        test_registration,
        test_valid_error_flow,
        test_threshold_alert,
        test_bad_format,
        test_not_saved_no_forward,
        test_photo_flow,
        test_photo_delivery_modes,
        test_photo_fallback_logic,
        test_ignored_cases,
        test_commands,
        test_supabase_notifier,
        test_lark_hook_payload,
        test_polling_loop_and_offset,
        test_flask_endpoints,
        test_topic_filter_by_id,
        test_topic_filter_by_name,
        test_commands_work_in_any_topic,
        test_command_normalization,
        test_alias_req_registers,
        test_unknown_command_suggests,
        test_cyrillic_layout_command_works,
        test_send_fallback_to_monitored_topic,
        test_send_fallback_guarded_by_allow_list,
        test_allow_list_bootstrap_commands,
        test_topic_not_configured,
    ]

    for test in tests:
        print(f"\n--- {test.__name__} ---")
        test()

    failed = [name for name, ok in RESULTS if not ok]
    total = len(RESULTS)

    print("\n" + "=" * 60)
    print(f"ИТОГО: {total - len(failed)}/{total} проверок пройдено")

    if failed:
        print("ПРОВАЛЫ:")
        for name in failed:
            print("  -", name)
        return 1

    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
