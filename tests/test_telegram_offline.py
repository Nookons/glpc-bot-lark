"""
Offline-проверка Telegram-бота: никаких сетевых вызовов.

Telegram API, Supabase и Lark-хук подменяются заглушками, поэтому
тест можно запускать локально без токена и без интернета:

    python3 tests/test_telegram_offline.py

Проверяет разбор команд, регистрацию сотрудника, основной поток
сохранения ошибки, алерт по порогу, фото и защиту от дублей.
"""

import json
import os
import signal
import threading
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

# Настоящая send_message нужна для проверки обрезки текста:
# дальше в файле tg.send_message подменяется заглушкой.
REAL_SEND_MESSAGE = tg.send_message
REAL_EDIT_MESSAGE_TEXT = tg.edit_message_text

import telegram_bot as bot  # noqa: E402
import bot_lease  # noqa: E402
import robot_status  # noqa: E402
import shift_report as sr  # noqa: E402


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


DELETED = []
EDITED = []
ANSWERED = []


def fake_send_message(
    chat_id,
    text,
    reply_to_message_id=None,
    disable_notification=False,
    message_thread_id=None,
    reply_markup=None,
):
    SENT.append({
        "chat_id": chat_id,
        "text": text,
        "thread_id": message_thread_id,
        "reply_markup": reply_markup,
        "message_id": len(SENT) + 1,
    })
    return {"message_id": len(SENT)}


def fake_delete_message(chat_id, message_id):
    DELETED.append((chat_id, message_id))
    return True


def fake_edit_message_text(chat_id, message_id, text, reply_markup=None):
    EDITED.append({"chat_id": chat_id, "message_id": message_id, "text": text})
    return {"message_id": message_id}


def fake_answer_callback_query(callback_query_id, text=None):
    ANSWERED.append({"id": callback_query_id, "text": text})
    return True


def fake_send_chat_action(*args, **kwargs):
    return True


def fake_get_file(file_id):
    return "photos/file_1.jpg"


def fake_download_file(file_path, destination):
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

    with open(destination, "wb") as f:
        f.write(b"fake-image")

    return destination


ROBOTS = {}


def fake_get_employee_name(telegram_id):
    return LINKS.get(telegram_id)


def fake_get_employee(telegram_id):
    if telegram_id not in LINKS:
        return None

    return {
        "user_name": LINKS[telegram_id],
        "card_id": 60072001,
        "is_leader": False,
        "home_warehouse": "GLP-C",
    }


def fake_find_robot(robot_number, warehouse=None):
    return ROBOTS.get(str(robot_number).strip().lstrip("#"))


def fake_find_robot_by_id(robot_id):
    for row in ROBOTS.values():
        if str(row.get("id")) == str(robot_id):
            return row

    return None


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
tg.delete_message = fake_delete_message
tg.edit_message_text = fake_edit_message_text
tg.answer_callback_query = fake_answer_callback_query
tg.get_file = fake_get_file
tg.download_file = fake_download_file

bot.get_employee_name = fake_get_employee_name
bot.get_employee = fake_get_employee
robot_status.find_robot = fake_find_robot
robot_status.find_robot_by_id = fake_find_robot_by_id
bot.resolve_employee_name = fake_resolve_employee_name
bot.link_user = fake_link_user
bot.unlink_user = fake_unlink_user
bot.send_to_data_base = fake_send_to_data_base

# Offset Telegram в тестах не читаем и не пишем в настоящий Storage.
bot.load_saved_offset = lambda: None
bot.save_offset = lambda offset: True
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


def make_callback(data, user_id=100, chat_id=-500, message_id=1, thread_id=2):
    MESSAGE_SEQ[0] += 1

    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": 8732612039, "is_bot": True},
    }

    if thread_id is not None:
        message["message_thread_id"] = thread_id

    return {
        "update_id": MESSAGE_SEQ[0],
        "callback_query": {
            "id": f"cb{MESSAGE_SEQ[0]}",
            "from": {
                "id": user_id,
                "is_bot": False,
                "username": "tester",
                "first_name": "Tester",
            },
            "message": message,
            "data": data,
        },
    }


def run(update):
    stdb.set_notifier(notifier)
    SENT.clear()
    DELETED.clear()
    EDITED.clear()
    ANSWERED.clear()
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
        "stats: отвечает (вид отчёта проверяется отдельно)",
        len(sent) == 1,
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
    bot._chat_types.clear()


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

    def flaky(chat_id, text, reply_to_message_id=None, disable_notification=False,
              message_thread_id=None, reply_markup=None):
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


def test_send_fallback_scoped_to_groups():
    """Откат в отслеживаемый топик — только для групп, не для лички."""
    reset_topics(topic_id=555)

    original_allowed = bot.ALLOWED_CHAT_IDS
    original_send = tg.send_message
    bot.ALLOWED_CHAT_IDS = set()

    calls = []

    def always_fail(chat_id, text, reply_to_message_id=None, disable_notification=False,
                    message_thread_id=None, reply_markup=None):
        calls.append((chat_id, message_thread_id))
        return None

    tg.send_message = always_fail

    try:
        bot._set_route(-500, 2, "supergroup")
        bot._send(-500, "привет")
        group_calls = list(calls)

        calls.clear()
        bot._set_route(100500, None, "private")
        bot._send(100500, "привет")
        private_calls = list(calls)

        calls.clear()
        bot._set_route(777, 2, None)
        bot._send(777, "привет")
        unknown_calls = list(calls)
    finally:
        tg.send_message = original_send
        bot.ALLOWED_CHAT_IDS = original_allowed
        reset_topics()

    check(
        "fallback: группа получает ответ в отслеживаемый топик",
        group_calls == [(-500, 2), (-500, 555)],
        group_calls,
    )
    check(
        "fallback: в личке ответ в группу не уходит",
        private_calls == [(100500, None)],
        private_calls,
    )
    check(
        "fallback: неизвестный чат без белого списка не получает откат",
        unknown_calls == [(777, 2)],
        unknown_calls,
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


# ============================================================
# SHIFT REPORT
# ============================================================

def _report_data_stub(payloads, default=None):
    """Заглушка shift_report_data + список вызовов."""
    calls = []

    empty = default or {
        "total": 0,
        "robots": {},
        "types": {},
        "employees": {},
        "downtime_minutes": 0,
        "maintenance": [],
    }

    def fake(shift_date, shift_name, warehouse=None, maintenance_threshold=3):
        calls.append((shift_date, shift_name))
        return dict(payloads.get((shift_date, shift_name), empty))

    return fake, calls


def test_stats_command_matches_report():
    """`/stats` должен отдавать тот же вид, что и отчёт за смену."""
    original = sr.shift_report_data

    fake, calls = _report_data_stub({
        ("2026-09-23", "day"): {
            "total": 14,
            "robots": {"3638": 3, "3680": 2},
            "types": {"Unable to drive": 12, "Other": 2},
            "employees": {"Huseyn": 12, "Dmytro": 2},
            "downtime_minutes": 88,
            "maintenance": [("3638", 3)],
        },
        ("2026-09-22", "night"): {
            "total": 23,
            "robots": {},
            "types": {},
            "employees": {},
            "downtime_minutes": 0,
            "maintenance": [],
        },
    })

    sr.shift_report_data = fake

    try:
        sent = run(make_update(text="/stats 2026-09-23 day"))
    finally:
        sr.shift_report_data = original

    text = sent[0]["text"] if sent else ""

    check("stats: тот же заголовок отчёта", "Shift report · GLP-C · 23.09.2026 · Day" in text, text)
    check("stats: итоги и простой", "Total 14 exceptions" in text and "Downtime 1h 28m" in text, text)
    check("stats: динамика к прошлой смене", "vs previous shift (22.09.2026 night) -9 ▼" in text, text)
    check("stats: обслуживание", "Maintenance (3+ per shift): 3638 (3)" in text, text)
    check(
        "stats: топы типов и людей",
        "Unable to drive — 12 (86%)" in text and "Huseyn (12) · Dmytro (2)" in text,
        text,
    )
    check(
        "stats: смена и предыдущая смена запрошены",
        calls == [("2026-09-23", "day"), ("2026-09-22", "night")],
        calls,
    )
    check(
        "stats: ответ ушёл в топик запроса",
        sent and sent[0]["thread_id"] is None,
        sent,
    )


def test_self_deleting_confirmations():
    """Служебные подтверждения должны исчезать через TTL, а важное — нет."""
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    original_send = tg.send_message
    original_delete = tg.delete_message

    deleted = []
    next_id = [5000]

    def fake_send(chat_id, text, reply_to_message_id=None,
                  disable_notification=False, message_thread_id=None,
                  reply_markup=None):
        next_id[0] += 1
        SENT.append({
            "chat_id": chat_id,
            "text": text,
            "thread_id": message_thread_id,
            "message_id": next_id[0],
        })
        return {"message_id": next_id[0]}

    def fake_delete(chat_id, message_id):
        deleted.append((chat_id, message_id))
        return True

    tg.send_message = fake_send
    tg.delete_message = fake_delete

    try:
        check("self-delete: TTL по умолчанию 10 секунд", bot.CONFIRM_TTL_SECONDS == 10, bot.CONFIRM_TTL_SECONDS)

        # 1) Подтверждение сохранения ставится в очередь на удаление.
        with bot._pending_lock:
            bot._pending_deletions.clear()

        run(make_update(text="Unable to drive: Security module failure. 3780"))

        with bot._pending_lock:
            queued = list(bot._pending_deletions)

        check("self-delete: подтверждение сохранения в очереди", len(queued) == 1, queued)
        check(
            "self-delete: срок — примерно TTL",
            queued and 0 < queued[0][0] - time.time() <= bot.CONFIRM_TTL_SECONDS + 1,
            queued,
        )
        check(
            "self-delete: очередь указывает на сообщение бота",
            queued and queued[0][2] == SENT[0]["message_id"],
            (queued, SENT),
        )

        removed = bot._drain_pending_deletions(
            now=time.time() + bot.CONFIRM_TTL_SECONDS + 1
        )
        check(
            "self-delete: сообщение удалено по сроку",
            removed == 1 and deleted == [(-500, SENT[0]["message_id"])],
            (removed, deleted),
        )
        with bot._pending_lock:
            check("self-delete: очередь очищена", bot._pending_deletions == [], bot._pending_deletions)

        # 2) Алерт про обслуживание удалять нельзя — его читает вся смена.
        with bot._pending_lock:
            bot._pending_deletions.clear()

        COUNTS["3780"] = 3
        run(make_update(text="Unable to drive: Security module failure. 3780"))

        with bot._pending_lock:
            queued = list(bot._pending_deletions)

        check(
            "self-delete: алерт про обслуживание НЕ удаляется",
            queued == [] and any("maintenance" in item["text"] for item in SENT),
            (queued, SENT),
        )

        # 3) Подтверждение по фото — тоже удаляется.
        with bot._pending_lock:
            bot._pending_deletions.clear()

        COUNTS["3780"] = 1
        run(make_update(photo=True, caption="broken robot"))

        with bot._pending_lock:
            queued = list(bot._pending_deletions)

        check("self-delete: подтверждение по фото в очереди", len(queued) == 1, queued)

        # 4) Инструкции (не статус) не удаляем.
        with bot._pending_lock:
            bot._pending_deletions.clear()

        LINKS.clear()
        run(make_update(text="Unable to drive: Security module failure. 3780"))

        with bot._pending_lock:
            queued = list(bot._pending_deletions)

        check(
            "self-delete: подсказка про /reg остаётся",
            queued == [] and any("not registered" in item["text"] for item in SENT),
            (queued, SENT),
        )

        # 5) delay=0 отключает удаление, ошибка удаления не ломает очередь.
        check("self-delete: delay=0 не планирует", bot.schedule_deletion(-500, 42, delay=0) is False)
        check("self-delete: None message_id не планирует", bot.schedule_deletion(-500, None, delay=10) is False)

        bot.schedule_deletion(-500, 777, delay=1)
        tg.delete_message = lambda chat_id, message_id: False  # уже удалено кем-то
        removed = bot._drain_pending_deletions(now=time.time() + 2)

        with bot._pending_lock:
            check(
                "self-delete: неудачное удаление не застревает в очереди",
                removed == 1 and bot._pending_deletions == [],
                (removed, bot._pending_deletions),
            )
    finally:
        tg.send_message = original_send
        tg.delete_message = original_delete

        with bot._pending_lock:
            bot._pending_deletions.clear()
        LINKS.clear()


def _robot(number=3783, robot_id=3542, status=None):
    return {
        "id": robot_id,
        "robot_number": number,
        "robot_type": "RT_KUBOT_MINI_HAIFLEX",
        "status": status or robot_status.ONLINE,
        "warehouse": "GLP-C",
    }


def test_robot_status_module():
    check(
        "status: подпись причины",
        robot_status.reason_label("offline", "abnormal_walking") == "Abnormal walking",
        robot_status.reason_label("offline", "abnormal_walking"),
    )
    check("status: неизвестный код причины", robot_status.reason_label("offline", "nope") is None)

    robot = _robot()
    check(
        "status: определение текущего состояния",
        robot_status.is_in_status(robot, "online") is True
        and robot_status.is_in_status(robot, "offline") is False,
    )

    original_patch = robot_status.rest_patch
    original_post = robot_status.rest_post

    patched, posted = [], []

    robot_status.rest_patch = lambda table, params, payload: (
        patched.append((table, params, payload)), [{}]
    )[1]
    robot_status.rest_post = lambda table, payload: (
        posted.append((table, payload)), [{}]
    )[1]

    try:
        result = robot_status.change_robot_status(
            robot, "offline", "Other", "сломан ролик", {"card_id": 60072001, "user_name": "Ivan"}
        )
    finally:
        robot_status.rest_patch = original_patch
        robot_status.rest_post = original_post

    check(
        "status: PATCH статуса робота",
        patched and patched[0][0] == "robots_maintenance_list"
        and patched[0][2]["status"] == robot_status.OFFLINE,
        patched,
    )
    check(
        "status: PATCH проставляет updated_by",
        patched and patched[0][2]["updated_by"] == 60072001,
        patched,
    )
    check(
        "status: CURRENT ISSUE в карточке робота заполняется",
        patched and patched[0][2]["type_problem"] == "Other"
        and patched[0][2]["problem_note"] == "сломан ролик",
        patched,
    )
    check(
        "status: запись в журнал change_status_robots",
        posted and posted[0][0] == "change_status_robots"
        and posted[0][1]["old_status"] == robot_status.ONLINE
        and posted[0][1]["new_status"] == robot_status.OFFLINE
        and posted[0][1]["add_by"] == 60072001
        and posted[0][1]["robot_id"] == 3542,
        posted,
    )
    check(
        "status: причина и заметка в журнале",
        posted and posted[0][1]["type_problem"] == "Other"
        and posted[0][1]["problem_note"] == "сломан ролик",
        posted,
    )
    check(
        "status: результат для карточки",
        result and result["old_status"] == robot_status.ONLINE
        and result["new_status"] == robot_status.OFFLINE,
        result,
    )

    card = robot_status.build_status_card("offline", result, "Ivan Petrenko")
    body = json.dumps(card, ensure_ascii=False)

    check("status: офлайн-карточка оранжевая", card["header"]["template"] == "orange", card["header"])
    check(
        "status: в карточке причина, заметка и автор",
        "сломан ролик" in body and "Ivan Petrenko" in body and "Other" in body,
        body[:200],
    )

    # Возврат в работу очищает текущую проблему в карточке робота.
    offline_robot = _robot(status=robot_status.OFFLINE)
    patched_online = []
    original_patch = robot_status.rest_patch
    original_post = robot_status.rest_post
    robot_status.rest_patch = lambda table, params, payload: (
        patched_online.append(payload), [{}]
    )[1]
    robot_status.rest_post = lambda table, payload: [{}]

    try:
        robot_status.change_robot_status(
            offline_robot, "online", "Software fix", "починили", {"card_id": 60072001}
        )
    finally:
        robot_status.rest_patch = original_patch
        robot_status.rest_post = original_post

    check(
        "status: онлайн очищает CURRENT ISSUE",
        patched_online
        and patched_online[0]["status"] == robot_status.ONLINE
        and patched_online[0]["type_problem"] is None
        and patched_online[0]["problem_note"] is None,
        patched_online,
    )

    online_card = robot_status.build_status_card(
        "online",
        {
            "robot": _robot(status=robot_status.OFFLINE),
            "old_status": robot_status.OFFLINE,
            "new_status": robot_status.ONLINE,
            "type_problem": "Software fix",
            "problem_note": "",
            "changed_at": "2026-09-23T10:00:00+00:00",
        },
        "Ivan",
    )
    check("status: онлайн-карточка зелёная", online_card["header"]["template"] == "green", online_card["header"])
    check(
        "status: текстовый запасной вариант",
        "Offline" in robot_status.build_status_text("offline", result, "Ivan")
        and "сломан ролик" in robot_status.build_status_text("offline", result, "Ivan"),
    )


def test_offline_command_validation():
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    LINKS.clear()
    sent = run(make_update(text="/offline 3783"))
    check("offline: без регистрации — подсказка", len(sent) == 1 and "not registered" in sent[0]["text"], sent)
    LINKS[100] = "Ivan Petrenko"

    sent = run(make_update(text="/offline"))
    check("offline: без номера — usage", len(sent) == 1 and "Usage: /offline" in sent[0]["text"], sent)

    sent = run(make_update(text="/offline 99999"))
    check("offline: робот не найден", len(sent) == 1 and "not found" in sent[0]["text"], sent)

    sent = run(make_update(text="/online 3783"))
    check(
        "online: робот уже онлайн — отказ без записи",
        len(sent) == 1 and "already" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/offline 3783"))
    keyboard = (sent[0].get("reply_markup") or {}).get("inline_keyboard") or []
    codes = [button["callback_data"] for row in keyboard for button in row]

    check(
        "offline: показан робот и переход статуса",
        "Online" in sent[0]["text"] and "Offline" in sent[0]["text"],
        sent,
    )
    check(
        "offline: кнопки всех причин + отмена",
        len(codes) == len(robot_status.REASONS["offline"]) + 1
        and any("abnormal_walking" in code for code in codes)
        and any(code.endswith(":cancel") for code in codes),
        codes,
    )


def test_status_flow_end_to_end():
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    # 1) команда -> кнопки
    sent = run(make_update(text="/offline 3783", thread_id=2))
    prompt_id = sent[0]["message_id"]

    # 2) тап по причине
    sent = run(make_callback(
        "st:offline:3542:abnormal_walking",
        message_id=prompt_id,
        thread_id=2,
    ))

    check("flow: нажатие подтверждено", ANSWERED and "Abnormal" in (ANSWERED[-1]["text"] or ""), ANSWERED)
    check(
        "flow: бот просит описать причину",
        EDITED and "Describe the reason" in EDITED[-1]["text"],
        EDITED,
    )

    pending = bot.peek_pending_status(-500, 100)
    check(
        "flow: флоу ждёт описание причины",
        pending and pending["type_problem"] == "Abnormal walking"
        and str(pending["robot_number"]) == "3783",
        pending,
    )

    # 3) пустое описание — просим ещё раз, флоу не теряется
    run(make_update(text="   ", thread_id=2))
    check(
        "flow: пустое описание не принимается",
        bot.peek_pending_status(-500, 100) is not None,
    )

    # 4) описание есть -> меняем статус, чистим за собой, шлём карточку
    original_patch = robot_status.rest_patch
    original_post = robot_status.rest_post
    original_card = bot.send_card_via_hook

    patched, posted, cards = [], [], []

    robot_status.rest_patch = lambda table, params, payload: (
        patched.append((table, params, payload)), [{}]
    )[1]
    robot_status.rest_post = lambda table, payload: (
        posted.append((table, payload)), [{}]
    )[1]
    bot.send_card_via_hook = lambda url, card: (cards.append(card), {"code": 0})[1]

    try:
        sent = run(make_update(text="сломан ролик, заменили", thread_id=2))
    finally:
        robot_status.rest_patch = original_patch
        robot_status.rest_post = original_post
        bot.send_card_via_hook = original_card

    check(
        "flow: статус робота изменён на Offline",
        patched and patched[0][2]["status"] == robot_status.OFFLINE,
        patched,
    )
    check(
        "flow: в журнал ушло описание сотрудника",
        posted and posted[0][1]["problem_note"] == "сломан ролик, заменили"
        and posted[0][1]["type_problem"] == "Abnormal walking",
        posted,
    )
    check(
        "flow: CURRENT ISSUE заполнен в карточке робота",
        patched and patched[0][2]["type_problem"] == "Abnormal walking"
        and patched[0][2]["problem_note"] == "сломан ролик, заменили",
        patched,
    )
    check(
        "flow: своё сообщение с кнопками удалено",
        (-500, prompt_id) in DELETED,
        DELETED,
    )
    check(
        "flow: подтверждение в Telegram с причиной",
        sent and "Offline" in sent[0]["text"] and "сломан ролик" in sent[0]["text"],
        sent,
    )
    check(
        "flow: карточка ушла в Lark",
        cards and "сломан ролик" in json.dumps(cards[0], ensure_ascii=False),
        cards,
    )
    check("flow: состояние флоу очищено", bot.peek_pending_status(-500, 100) is None)


def test_status_flow_cancel_and_fallbacks():
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    # Отмена кнопкой.
    sent = run(make_update(text="/offline 3783", thread_id=2))
    prompt_id = sent[0]["message_id"]

    run(make_callback("st:offline:3542:cancel", message_id=prompt_id, thread_id=2))

    check("cancel: сообщение с кнопками удалено", (-500, prompt_id) in DELETED, DELETED)
    check("cancel: состояние сброшено", bot.peek_pending_status(-500, 100) is None)

    # Отмена командой уже после выбора причины.
    sent = run(make_update(text="/offline 3783", thread_id=2))
    prompt_id = sent[0]["message_id"]
    run(make_callback("st:offline:3542:other", message_id=prompt_id, thread_id=2))
    check("cancel: причина выбрана", bot.peek_pending_status(-500, 100) is not None)

    run(make_update(text="/cancel", thread_id=2))
    check("cancel: /cancel сбрасывает флоу", bot.peek_pending_status(-500, 100) is None)
    check("cancel: сообщение бота убрано", (-500, prompt_id) in DELETED, DELETED)

    # Кнопка без регистрации.
    sent = run(make_update(text="/offline 3783", thread_id=2))
    prompt_id = sent[0]["message_id"]
    LINKS.clear()

    run(make_callback("st:offline:3542:other", message_id=prompt_id, thread_id=2))
    check(
        "callback: без регистрации — всплывашка про /reg",
        ANSWERED and "Register first" in (ANSWERED[-1]["text"] or ""),
        ANSWERED,
    )
    LINKS[100] = "Ivan Petrenko"

    # Карточка не прошла -> уходит текстом.
    original_card = bot.send_card_via_hook
    original_text = bot.send_text_via_hook
    hooks = []

    bot.send_card_via_hook = lambda url, card: {"code": 9499, "msg": "bad"}
    bot.send_text_via_hook = lambda url, text: (hooks.append(text), {"code": 0})[1]

    try:
        sent = run(make_update(text="/offline 3783", thread_id=2))
        prompt_id = sent[0]["message_id"]
        run(make_callback("st:offline:3542:other", message_id=prompt_id, thread_id=2))

        original_patch = robot_status.rest_patch
        original_post = robot_status.rest_post
        robot_status.rest_patch = lambda table, params, payload: [{}]
        robot_status.rest_post = lambda table, payload: [{}]

        try:
            run(make_update(text="нет запчасти", thread_id=2))
        finally:
            robot_status.rest_patch = original_patch
            robot_status.rest_post = original_post
    finally:
        bot.send_card_via_hook = original_card
        bot.send_text_via_hook = original_text

    check(
        "flow: карточка не прошла — отчёт ушёл текстом",
        hooks and "нет запчасти" in hooks[-1],
        hooks,
    )

    # Просроченный флоу не перехватывает обычные сообщения.
    bot.set_pending_status(-500, 100, {
        "direction": "offline",
        "robot_number": 3783,
        "type_problem": "Other",
        "prompt_message_id": None,
    })

    with bot._pending_status_lock:
        bot._pending_status[(-500, 100)]["expires"] = 0

    sent = run(make_update(text="Unable to drive: Security module failure. 3783", thread_id=2))
    check(
        "flow: просроченный флоу не перехватывает ошибку",
        bot.peek_pending_status(-500, 100) is None,
        bot._pending_status,
    )

    bot.clear_pending_status(-500, 100)
    ROBOTS.clear()
    LINKS.clear()


# ============================================================
# ЛИЗ ЕДИНСТВЕННОГО ОПРАШИВАЮЩЕГО (защита от дублей)
# ============================================================

def _lease_env(rows=None, post_result=None, patch_result=None, table=True):
    """Заглушки Supabase для лиза + журнал вызовов."""
    calls = {"get": [], "post": [], "patch": []}

    original = (
        bot_lease.table_exists,
        bot_lease.rest_get,
        bot_lease.rest_post,
        bot_lease.rest_patch,
    )

    bot_lease._table_ok = None
    bot_lease._warned_no_table = False

    bot_lease.table_exists = lambda table_name: table
    bot_lease.rest_get = lambda table_name, params=None: (
        calls["get"].append(params), rows
    )[1]
    bot_lease.rest_post = lambda table_name, payload: (
        calls["post"].append(payload), post_result
    )[1]
    bot_lease.rest_patch = lambda table_name, params, payload: (
        calls["patch"].append((params, payload)), patch_result
    )[1]

    return calls, original


def _lease_restore(original):
    (
        bot_lease.table_exists,
        bot_lease.rest_get,
        bot_lease.rest_post,
        bot_lease.rest_patch,
    ) = original

    bot_lease._table_ok = None
    bot_lease._warned_no_table = False


def _iso(seconds_ago=0):
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_text_truncation_helpers():
    from text_utils import TELEGRAM_TEXT_LIMIT, truncate

    check("truncate: короткий текст не трогаем", truncate("abc", 10) == "abc")
    check("truncate: длинный обрезается", truncate("x" * 100, 20).endswith("…"), truncate("x" * 100, 20))
    check("truncate: длина не превышает лимит", len(truncate("x" * 100, 20)) <= 20)
    check("truncate: None -> пустая строка", truncate(None) == "")

    # Telegram отклоняет сообщения > 4096: проверяем, что обрезаем до отправки.
    original_call = tg.call
    payloads = []

    tg.call = lambda method, payload=None, timeout=40: (
        payloads.append((method, payload)), {"message_id": 1}
    )[1]

    try:
        REAL_SEND_MESSAGE(-500, "y" * 10_000)
    finally:
        tg.call = original_call

    sent_text = payloads[0][1]["text"] if payloads else ""
    check(
        "telegram: длинный текст обрезается перед отправкой",
        len(sent_text) <= TELEGRAM_TEXT_LIMIT and sent_text.endswith("…"),
        len(sent_text),
    )


def test_stats_command_validates_date():
    sent = run(make_update(text="/stats 2026-99-99 day"))
    check(
        "stats: некорректная дата — подсказка, а не падение",
        len(sent) == 1 and "Bad date" in sent[0]["text"] and "Usage" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/stats abc day"))
    check(
        "stats: мусор вместо даты — подсказка",
        len(sent) == 1 and "Bad date" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/stats 2026-09-23"))
    check(
        "stats: без смены — общая подсказка",
        len(sent) == 1 and "Usage: /stats" in sent[0]["text"],
        sent,
    )


def test_images_janitor_removes_old_files():
    import tempfile

    old_dir = bot._IMAGES_DIR
    tmp = tempfile.mkdtemp(prefix="glpc-janitor-")

    old_file = os.path.join(tmp, "old.jpg")
    fresh_file = os.path.join(tmp, "fresh.jpg")

    with open(old_file, "wb") as f:
        f.write(b"old")
    with open(fresh_file, "wb") as f:
        f.write(b"fresh")

    past = time.time() - (bot.IMAGES_RETENTION_DAYS + 1) * 86400
    os.utime(old_file, (past, past))

    bot._IMAGES_DIR = tmp

    try:
        removed = bot.cleanup_old_images()
    finally:
        bot._IMAGES_DIR = old_dir

    check("janitor: старое фото удалено", removed == 1, removed)
    check("janitor: старое фото исчезло", not os.path.exists(old_file))
    check("janitor: свежее фото осталось", os.path.exists(fresh_file))

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_shift_stats_endpoint_token():
    client = bot.app.test_client()
    original_token = bot.STATS_TOKEN

    try:
        bot.STATS_TOKEN = ""
        response = client.get("/shift_stats?date=2026-09-23&shift=day")
        check(
            "stats endpoint: без токена открыт (обратная совместимость)",
            response.status_code == 200,
            response.status_code,
        )

        bot.STATS_TOKEN = "s3cret"
        forbidden = client.get("/shift_stats?date=2026-09-23&shift=day")
        allowed = client.get("/shift_stats?date=2026-09-23&shift=day&token=s3cret")

        check(
            "stats endpoint: с токеном закрыт",
            forbidden.status_code == 403,
            forbidden.status_code,
        )
        check(
            "stats endpoint: с правильным токеном открыт",
            allowed.status_code == 200,
            allowed.status_code,
        )
    finally:
        bot.STATS_TOKEN = original_token


def test_update_key_and_dedupe_after_success():
    message_update = make_update(text="/help", message_id=4242, chat_id=-500)
    check(
        "dedupe: ключ = чат:сообщение",
        bot._update_key(message_update) == "-500:4242",
        bot._update_key(message_update),
    )

    callback_update = make_callback("st:offline:3542:other")
    check(
        "dedupe: ключ кнопки",
        bot._update_key(callback_update).startswith("cb:"),
        bot._update_key(callback_update),
    )

    # Один и тот же апдейт дважды: второй раз пропускаем.
    update = make_update(text="/help", message_id=5252)
    run(update)
    first = len(SENT)
    run(update)
    check(
        "dedupe: повторный апдейт не обрабатывается",
        first == 1 and SENT == [] or len(SENT) == 0,
        (first, SENT),
    )


def test_polling_persists_offset():
    """После батча offset уходит в Storage, чтобы Telegram не переотдал его."""
    import threading as _threading

    stop = _threading.Event()
    saved = []
    batches = [[make_update(text="/help", message_id=6262)]]

    def fake_get_updates(offset=None, timeout=30):
        if batches:
            return batches.pop(0)
        stop.set()
        return []

    original_updates = tg.get_updates
    original_save = bot.save_offset
    original_load = bot.load_saved_offset

    tg.get_updates = fake_get_updates
    bot.save_offset = lambda offset: (saved.append(offset), True)[1]
    bot.load_saved_offset = lambda: None

    try:
        bot.polling_loop(stop, None)
    finally:
        tg.get_updates = original_updates
        bot.save_offset = original_save
        bot.load_saved_offset = original_load

    check(
        "offset: сохранён после обработки батча",
        saved and saved[0] and saved[0] > 0,
        saved,
    )


def test_report_sent_ok_semantics():
    check(
        "report retry: маркер already-sent не повторяем",
        sr._report_sent_ok({"skipped": True, "reason": "already-sent"}) is True,
    )
    check(
        "report retry: ошибка БД — повторяем",
        sr._report_sent_ok({"skipped": True, "reason": "db-error"}) is False,
    )
    check(
        "report retry: отказ хука — повторяем",
        sr._report_sent_ok({"code": 9499, "msg": "bad"}) is False,
    )
    check(
        "report retry: успех — отметка",
        sr._report_sent_ok({"code": 0, "msg": "success"}) is True,
    )


def test_health_reports_degraded():
    client = bot.app.test_client()

    original_status = bot.LEASE_STATUS
    original_last_poll = bot._LAST_POLL_AT

    try:
        bot.LEASE_STATUS = "standby (someone-else)"
        response = client.get("/health")
        check(
            "health: standby — сервис здоров",
            response.status_code == 200,
            response.status_code,
        )

        bot.LEASE_STATUS = "poller"
        bot._LAST_POLL_AT = time.time() - (bot.POLL_STALL_SECONDS + 30)
        response = client.get("/health")
        payload = response.get_json()
        check(
            "health: зависший poller -> 503",
            response.status_code == 503 and payload.get("reason") == "poller stalled",
            (response.status_code, payload),
        )

        bot.LEASE_STATUS = "lease-lost"
        response = client.get("/health")
        check(
            "health: потеря лиза -> 503",
            response.status_code == 503,
            response.status_code,
        )

        bot.LEASE_STATUS = "poller"
        bot._LAST_POLL_AT = time.time()
        response = client.get("/health")
        check(
            "health: живой poller -> 200",
            response.status_code == 200,
            response.status_code,
        )
    finally:
        bot.LEASE_STATUS = original_status
        bot._LAST_POLL_AT = original_last_poll


def test_parser_and_count_guards():
    from error_parser import parse_error_message

    check("parser: None не роняет разбор", parse_error_message(None) is None)
    check("parser: пустая строка", parse_error_message("   ") is None)

    import sendToDataBase as stdb

    original_get = stdb._rest_get
    captured = {}

    def fake_get(table, params=None):
        captured["table"] = table
        captured["params"] = params
        return [{"error_robot": 3783}, {"error_robot": 3783}, {"error_robot": 1}]

    stdb._rest_get = fake_get

    try:
        count = stdb.count_robot_errors_in_shift(3783, "2026-09-23", "day")
    finally:
        stdb._rest_get = original_get

    check("count: считает только нужного робота", count == 2, count)
    check(
        "count: тянет только номер робота",
        captured.get("params", {}).get("select") == "error_robot",
        captured.get("params"),
    )
    check(
        "count: фильтрует по складу",
        captured.get("params", {}).get("warehouse") == "eq.GLP-C",
        captured.get("params"),
    )


def test_stats_endpoint_handles_db_error():
    client = bot.app.test_client()
    original = bot.shift_stats

    def boom(shift_date, shift_name):
        raise RuntimeError("db down")

    bot.shift_stats = boom

    try:
        response = client.get("/shift_stats?date=2026-09-23&shift=day")
    finally:
        bot.shift_stats = original

    check(
        "stats endpoint: ошибка БД -> 503 JSON, а не 500",
        response.status_code == 503 and response.get_json().get("error"),
        (response.status_code, response.get_data(as_text=True)[:80]),
    )


def test_edit_message_truncates():
    original_call = tg.call
    payloads = []

    tg.call = lambda method, payload=None, timeout=40: (
        payloads.append((method, payload)), {"message_id": 1}
    )[1]

    try:
        REAL_EDIT_MESSAGE_TEXT(-500, 1, "z" * 10_000)
    finally:
        tg.call = original_call

    text = payloads[0][1]["text"] if payloads else ""
    check(
        "telegram: editMessageText тоже обрезает текст",
        text.endswith("…") and len(text) <= 4000,
        len(text),
    )


def test_missing_robot_is_queued_and_reported():
    """send_to_data_base: нет робота -> в очередь на добавление + маркер для Lark."""
    import sendToDataBase as stdb

    template = {
        "employee_title": "Security module failure",
        "id": 122,
        "solving_time": 6,
        "issue_sub_type": "sub",
        "issue_description": "desc",
        "issue_type": "Unable to drive",
        "recovery_title": "recovery",
    }

    parsed = {
        "error_type": "Unable to drive",
        "robot": "3884",
        "error_text": "Security module failure",
    }

    original_get = stdb._rest_get
    original_post = stdb._rest_post
    original_notify = stdb.notify_user

    posted, notified = [], []

    def fake_get(table, params=None):
        if table == "issue_templates":
            return [template]
        if table == "employees":
            return [{
                "card_id": 60072001,
                "user_name": "Ivan Petrenko",
                "home_warehouse": "GLP-C",
            }]
        if table == "robots_maintenance_list":
            return []
        return []

    stdb._rest_get = fake_get
    stdb._rest_post = lambda table, payload: (posted.append((table, payload)), [{}])[1]
    stdb.notify_user = lambda chat_id, text: notified.append(text)

    try:
        result = stdb.send_to_data_base(
            parsed,
            {"employee": "Ivan Petrenko", "robot": "3884",
             "error_text": "Security module failure"},
            -500,
        )
    finally:
        stdb._rest_get = original_get
        stdb._rest_post = original_post
        stdb.notify_user = original_notify

    check(
        "robot missing: возвращается маркер для пересылки в Lark",
        isinstance(result, dict)
        and result.get("robot_missing") is True
        and result.get("robot") == "3884",
        result,
    )
    check(
        "robot missing: робот поставлен в очередь добавления",
        posted and posted[0][0] == "robots_to_add"
        and posted[0][1]["robot_number"] == "3884",
        posted,
    )
    check(
        "robot missing: сотруднику сказано, что ошибка ушла в Lark",
        notified and "Lark" in notified[0],
        notified,
    )
    check(
        "robot missing: в базу исключений ничего не пишем",
        all(table != "exceptions" for table, _payload in posted),
        posted,
    )


def test_bot_forwards_missing_robot_to_lark():
    """Бот должен переслать в Lark ошибку по роботу, которого нет в системе."""
    LINKS[100] = "Ivan Petrenko"

    original_save = bot.send_to_data_base
    original_count = bot.count_robot_errors_in_shift

    counted = []

    bot.send_to_data_base = lambda parsed, data_obj, chat_id: {
        "robot_missing": True,
        "robot": parsed["robot"],
    }
    bot.count_robot_errors_in_shift = lambda *args, **kwargs: counted.append(args) or 0

    try:
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3884",
            thread_id=2,
        ))
    finally:
        bot.send_to_data_base = original_save
        bot.count_robot_errors_in_shift = original_count

    lines = FORWARDED[-1]["lines"] if FORWARDED else []
    flat = " ".join(f"{label}: {value}" for label, value in lines)

    check(
        "robot missing: карточка всё равно ушла в Lark",
        len(FORWARDED) == 1 and "3884" in flat,
        FORWARDED,
    )
    check(
        "robot missing: в карточке есть автор, время и детали",
        "Ivan Petrenko" in flat and "Security module failure" in flat
        and "Time" in flat,
        flat,
    )
    check(
        "robot missing: в Lark нет пометки, что робота нет в системе",
        "not in the system" not in flat
        and "NOT saved" not in flat
        and "missing" not in flat,
        flat,
    )
    check(
        "robot missing: счётчик смены не запрашивается",
        counted == [],
        counted,
    )
    check(
        "robot missing: нет подтверждения о сохранении",
        not any("Saved" in item["text"] for item in sent),
        sent,
    )


def test_lease_acquire_and_refresh():
    # Свободный лиз занимаем вставкой строки.
    calls, original = _lease_env(rows=[], post_result=[{"name": "glpc-bot-telegram"}])

    try:
        result = bot_lease.acquire("me-1")
    finally:
        _lease_restore(original)

    check(
        "lease: свободный лиз занимается",
        result["acquired"] is True and result["status"] == "acquired",
        result,
    )
    check(
        "lease: строка создаётся с нашим holder",
        calls["post"] and calls["post"][0]["holder"] == "me-1",
        calls["post"],
    )

    # Продление своего лиза.
    calls, original = _lease_env(
        rows=[{"name": "glpc-bot-telegram", "holder": "me-1", "heartbeat_at": _iso()}],
        patch_result=[{"name": "glpc-bot-telegram"}],
    )

    try:
        renewed = bot_lease.refresh("me-1")
    finally:
        _lease_restore(original)

    check("lease: свой лиз продлевается", renewed is True)
    check(
        "lease: продление фильтруется по holder",
        calls["patch"] and calls["patch"][0][0].get("holder") == "eq.me-1",
        calls["patch"],
    )

    # Продление, когда лиз уже не наш.
    calls, original = _lease_env(
        rows=[{"holder": "other", "heartbeat_at": _iso()}],
        patch_result=None,
    )

    try:
        renewed = bot_lease.refresh("me-1")
    finally:
        _lease_restore(original)

    check("lease: чужой лиз продлить нельзя", renewed is False)


def test_lease_release_and_shutdown_handler():
    """Лиз должен отпускаться при остановке — иначе после деплоя пауза."""
    calls, original = _lease_env(
        rows=[{"holder": "me-1", "heartbeat_at": _iso()}],
    )

    original_delete = bot_lease.rest_delete
    deleted = []
    bot_lease.rest_delete = lambda table_name, params=None: (
        deleted.append(params), True
    )[1]

    try:
        released = bot_lease.release("me-1")
    finally:
        bot_lease.rest_delete = original_delete
        _lease_restore(original)

    check("lease: лиз отпускается", released is True, released)
    check(
        "lease: отпускаем только свой лиз (по holder)",
        deleted and deleted[0].get("holder") == "eq.me-1",
        deleted,
    )

    # Обработчик SIGTERM отпускает лиз.
    original_release = bot_lease.release
    released_holders = []
    bot_lease.release = lambda holder=None: (
        released_holders.append(holder), True
    )[1]

    original_exit = os._exit
    os._exit = lambda code=None: None
    original_sigterm = signal.getsignal(signal.SIGTERM)

    try:
        bot.install_shutdown_handler("me-42")
        handler = signal.getsignal(signal.SIGTERM)

        check("shutdown: обработчик установлен", callable(handler), handler)
        handler(signal.SIGTERM, None)
    finally:
        bot_lease.release = original_release
        os._exit = original_exit
        signal.signal(signal.SIGTERM, original_sigterm)

    check(
        "shutdown: по сигналу лиз отпускается",
        released_holders and released_holders[-1] == "me-42",
        released_holders,
    )


def test_lease_held_by_other_and_takeover():
    # Живой чужой лиз не трогаем.
    calls, original = _lease_env(
        rows=[{"holder": "live-1", "heartbeat_at": _iso(5)}],
        patch_result=[{"holder": "me-2"}],
    )

    try:
        result = bot_lease.acquire("me-2")
    finally:
        _lease_restore(original)

    check(
        "lease: живой чужой лиз — standby",
        result["acquired"] is False
        and result["status"] == "held-by-other"
        and result["holder"] == "live-1",
        result,
    )
    check("lease: чужой живой лиз не перезаписываем", calls["patch"] == [], calls["patch"])

    # Просроченный лиз забираем.
    calls, original = _lease_env(
        rows=[{"holder": "dead-1", "heartbeat_at": _iso(10_000)}],
        patch_result=[{"holder": "me-3"}],
    )

    try:
        result = bot_lease.acquire("me-3")
    finally:
        _lease_restore(original)

    check(
        "lease: просроченный лиз забирается",
        result["acquired"] is True and result["status"] == "taken-over",
        result,
    )
    check(
        "lease: замена идёт по прежнему holder",
        calls["patch"] and calls["patch"][0][0].get("holder") == "eq.dead-1",
        calls["patch"],
    )

    # Гонка: сосед успел забрать лиз первым.
    calls, original = _lease_env(
        rows=[{"holder": "dead-1", "heartbeat_at": _iso(10_000)}],
        patch_result=None,
    )

    try:
        result = bot_lease.acquire("me-4")
    finally:
        _lease_restore(original)

    check(
        "lease: проигранная гонка не даёт опрашивать",
        result["acquired"] is False and result["status"] == "raced",
        result,
    )


def test_lease_edge_cases_and_polling_guard():
    # Таблицы нет — работаем с предупреждением (доступность важнее).
    calls, original = _lease_env(rows=[], table=False)

    try:
        result = bot_lease.acquire("me-5")
    finally:
        _lease_restore(original)

    check(
        "lease: без таблицы бот работает, но с предупреждением",
        result["acquired"] is True and result["status"] == "no-table",
        result,
    )

    # Ошибка чтения — не опрашиваем, ждём следующей попытки.
    calls, original = _lease_env(rows=None)

    try:
        result = bot_lease.acquire("me-6")
    finally:
        _lease_restore(original)

    check(
        "lease: ошибка чтения — в standby",
        result["acquired"] is False and result["status"] == "error",
        result,
    )

    # Потеря лиза останавливает polling.
    original_check = bot_lease.check
    original_updates = tg.get_updates
    updates_called = []

    bot_lease.check = lambda holder=None: "lost"
    tg.get_updates = lambda offset=None, timeout=30: (updates_called.append(offset), [])[1]

    try:
        bot.LEASE_STATUS = "poller"
        bot.polling_loop(None, "me-7")
    finally:
        bot_lease.check = original_check
        tg.get_updates = original_updates

    check(
        "lease: потеря лиза останавливает polling",
        updates_called == [] and bot.LEASE_STATUS == "lease-lost",
        (updates_called, bot.LEASE_STATUS),
    )

    # Транзиентный сбой базы НЕ должен останавливать опрос.
    states = ["error", "error", "lost"]
    original_check = bot_lease.check
    original_updates = tg.get_updates
    updates_called = []

    bot_lease.check = lambda holder=None: states.pop(0) if states else "lost"
    tg.get_updates = lambda offset=None, timeout=30: (updates_called.append(offset), [])[1]

    try:
        bot.polling_loop(None, "me-8")
    finally:
        bot_lease.check = original_check
        tg.get_updates = original_updates

    check(
        "lease: сбой базы не останавливает опрос",
        len(updates_called) == 2,
        updates_called,
    )

    # Standby возвращает True, когда лиз получен.
    original_acquire = bot_lease.acquire
    bot_lease.acquire = lambda holder=None: {
        "acquired": True,
        "status": "taken-over",
        "holder": holder,
    }

    try:
        got = bot.standby_loop("me-9", interval=0)
    finally:
        bot_lease.acquire = original_acquire

    check("lease: standby получает лиз", got is True, got)

    # Супервизор: потеряли лиз -> standby -> снова опрашиваем.
    original_acquire = bot_lease.acquire
    original_polling = bot.start_polling
    original_scheduler = bot.start_shift_scheduler
    original_retry = bot.STANDBY_RETRY_SECONDS

    poll_starts, scheduler_starts = [], []

    class _FakeThread:
        def join(self):
            return None

    bot_lease.acquire = lambda holder=None: {
        "acquired": True,
        "status": "taken-over",
        "holder": holder,
    }
    bot.STANDBY_RETRY_SECONDS = 0
    bot.start_polling = lambda holder=None: (poll_starts.append(holder), _FakeThread())[1]
    bot.start_shift_scheduler = lambda: scheduler_starts.append(True)

    try:
        worker = threading.Thread(
            target=bot.poller_supervisor,
            args=("me-77", True),
            daemon=True,
        )
        worker.start()

        deadline = time.time() + 5
        while time.time() < deadline and len(poll_starts) < 2:
            time.sleep(0.02)
    finally:
        bot_lease.acquire = original_acquire
        bot.start_polling = original_polling
        bot.start_shift_scheduler = original_scheduler
        bot.STANDBY_RETRY_SECONDS = original_retry

    check(
        "supervisor: после потери лиза опрос запускается снова",
        len(poll_starts) >= 2,
        poll_starts,
    )
    check(
        "supervisor: планировщик отчётов стартует ровно один раз",
        scheduler_starts == [True],
        scheduler_starts,
    )

    bot.LEASE_STATUS = "starting"


def test_report_sent_only_once_per_shift():
    """Перезапуск внутри окна отчёта не должен слать дубль."""
    original_data = sr.shift_report_data
    original_card = sr.send_card_via_hook
    original_marker_get = sr.download_json
    original_marker_put = sr.upload_json

    fake, _ = _report_data_stub({
        ("2026-09-23", "day"): {
            "total": 4,
            "robots": {"1": 4},
            "types": {"X": 4},
            "employees": {"A": 4},
            "downtime_minutes": 20,
            "maintenance": [],
        },
    })

    sent = []
    markers = {}

    sr.shift_report_data = fake
    sr.send_card_via_hook = lambda url, card: (sent.append("card"), {"code": 0})[1]
    sr.download_json = lambda bucket, name: markers.get(name)
    sr.upload_json = lambda bucket, name, payload: (
        markers.update({name: payload}), True
    )[1]

    try:
        sr.send_shift_report("2026-09-23", "day")
        check("report: первая отправка проходит", sent == ["card"], sent)
        check(
            "report: маркер отправки поставлен",
            "2026-09-23-day.json" in markers,
            list(markers),
        )

        result = sr.send_shift_report("2026-09-23", "day")
        check(
            "report: повторная отправка блокируется маркером",
            sent == ["card"] and isinstance(result, dict)
            and result.get("skipped") is True,
            (sent, result),
        )

        sr.send_shift_report("2026-09-23", "day", force=True)
        check(
            "report: force отправляет повторно",
            sent == ["card", "card"],
            sent,
        )

        sr.send_shift_report("2026-09-22", "night")
        check(
            "report: другая смена не блокируется",
            sent == ["card", "card", "card"],
            sent,
        )
    finally:
        sr.shift_report_data = original_data
        sr.send_card_via_hook = original_card
        sr.download_json = original_marker_get
        sr.upload_json = original_marker_put


def test_report_previous_shift():
    check(
        "report: день -> ночь предыдущего дня",
        sr.previous_shift("2026-09-23", "day") == ("2026-09-22", "night"),
        sr.previous_shift("2026-09-23", "day"),
    )
    check(
        "report: ночь -> день того же дня",
        sr.previous_shift("2026-09-23", "night") == ("2026-09-23", "day"),
        sr.previous_shift("2026-09-23", "night"),
    )
    check(
        "report: переход через месяц",
        sr.previous_shift("2026-10-01", "day") == ("2026-09-30", "night"),
        sr.previous_shift("2026-10-01", "day"),
    )


def test_report_formatting():
    check("report: 40 минут", sr.format_duration(40) == "40m", sr.format_duration(40))
    check("report: ровно час", sr.format_duration(60) == "1h 00m", sr.format_duration(60))
    check("report: 95 минут", sr.format_duration(95) == "1h 35m", sr.format_duration(95))
    check("report: дельта +3", sr.format_delta(3) == "+3 ▲", sr.format_delta(3))
    check("report: дельта -9", sr.format_delta(-9) == "-9 ▼", sr.format_delta(-9))
    check("report: дельта 0", sr.format_delta(0) == "±0", sr.format_delta(0))

    top = sr._top_line([("a", 3), ("b", 2), ("c", 1)], limit=2)
    check("report: топ обрезается с хвостом", top == "a (3) · b (2) (+1 more)", top)
    check("report: пустой топ", sr._top_line([]) == "—", sr._top_line([]))

    issues = sr._issues_line({"Unable to drive": 3, "Other": 1}, 4)
    check(
        "report: проценты по типам",
        "Unable to drive — 3 (75%)" in issues and "Other — 1 (25%)" in issues,
        issues,
    )


def test_report_metrics_and_text():
    original = sr.shift_report_data

    fake, calls = _report_data_stub({
        ("2026-09-23", "day"): {
            "total": 14,
            "robots": {"3638": 3, "3680": 2},
            "types": {"Unable to drive": 12, "Other": 2},
            "employees": {"Huseyn": 12, "Dmytro": 2},
            "downtime_minutes": 88,
            "maintenance": [("3638", 3)],
        },
        ("2026-09-22", "night"): {
            "total": 23,
            "robots": {},
            "types": {},
            "employees": {},
            "downtime_minutes": 0,
            "maintenance": [],
        },
    })

    sr.shift_report_data = fake

    try:
        metrics = sr.shift_metrics("2026-09-23", "day")
        text = sr.build_shift_summary("2026-09-23", "day", metrics)
    finally:
        sr.shift_report_data = original

    check(
        "report: запрос текущей и прошлой смены",
        calls == [("2026-09-23", "day"), ("2026-09-22", "night")],
        calls,
    )
    check("report: дельта -9", metrics["delta"] == -9, metrics.get("delta"))
    check("report: простой в тексте", "Downtime 1h 28m" in text, text)
    check("report: динамика в тексте", "-9 ▼" in text, text)
    check("report: роботы на обслуживание", "Maintenance (3+ per shift): 3638 (3)" in text, text)
    check("report: топ типов с процентами", "Unable to drive — 12 (86%)" in text, text)
    check("report: разбивка по сотрудникам", "Huseyn (12) · Dmytro (2)" in text, text)
    check(
        "report: топ роботов без разбора по каждому",
        "Top robots: 3638 (3) · 3680 (2)" in text and "more" not in text.split("Top robots")[1],
        text,
    )


def test_report_empty_shift():
    original = sr.shift_report_data
    fake, _ = _report_data_stub({})
    sr.shift_report_data = fake

    try:
        metrics = sr.shift_metrics("2026-09-23", "day")
        text = sr.build_shift_summary("2026-09-23", "day", metrics)
        card = sr.build_shift_card("2026-09-23", "day", metrics)
    finally:
        sr.shift_report_data = original

    check("report: пустая смена в тексте", "No exceptions this shift." in text, text)
    check("report: пустая смена — зелёная карточка", card["header"]["template"] == "green", card["header"])
    check("report: пустая смена — один блок", len(card["elements"]) == 1, card["elements"])


def test_report_card_structure_and_colors():
    def card_for(total, maintenance):
        metrics = {
            "total": total,
            "robots": {"3638": 2},
            "types": {"Unable to drive": total},
            "employees": {"Huseyn": total},
            "downtime_minutes": 10,
            "maintenance": maintenance,
            "previous": {"date": "2026-09-22", "shift": "night", "total": 1},
            "delta": total - 1,
        }
        return sr.build_shift_card("2026-09-23", "day", metrics)

    check("report: мало ошибок — зелёная", card_for(3, [])["header"]["template"] == "green")
    check("report: много ошибок — оранжевая", card_for(7, [])["header"]["template"] == "orange")
    check(
        "report: есть обслуживание — красная",
        card_for(7, [("3638", 3)])["header"]["template"] == "red",
    )

    card = card_for(7, [("3638", 3)])
    tags = [e["tag"] for e in card["elements"]]
    check("report: в карточке есть разделитель", "hr" in tags, tags)
    check("report: роботы уходят в примечание", tags[-1] == "note", tags)

    body = json.dumps(card, ensure_ascii=False)
    check(
        "report: в карточке есть обслуживание и динамика",
        "Maintenance (3+ per shift)" in body and "▲" in body,
        body[:200],
    )
    check(
        "report: карточка не перечисляет всех роботов",
        "Top robots" in body and "+0 more" not in body,
        body[:200],
    )


def test_report_send_fallback():
    original_data = sr.shift_report_data
    original_card = sr.send_card_via_hook
    original_text = sr.send_text_via_hook
    original_marker_get = sr.download_json
    original_marker_put = sr.upload_json

    sr.download_json = lambda bucket, name: None
    sr.upload_json = lambda bucket, name, payload: True

    fake, _ = _report_data_stub({
        ("2026-09-23", "day"): {
            "total": 4,
            "robots": {"1": 4},
            "types": {"X": 4},
            "employees": {"A": 4},
            "downtime_minutes": 20,
            "maintenance": [("1", 4)],
        },
    })

    sent = []

    sr.shift_report_data = fake

    try:
        # 1) Карточка принята — текстом не дублируем.
        sr.send_card_via_hook = lambda url, card: (sent.append("card"), {"code": 0})[1]
        sr.send_text_via_hook = lambda url, text: (sent.append("text"), {"code": 0})[1]
        sr.send_shift_report("2026-09-23", "day")
        check("report: карточка отправлена", sent == ["card"], sent)

        # 2) Карточку отклонили — уходит текстовый вариант.
        sent.clear()
        sr.send_card_via_hook = lambda url, card: {"code": 9499, "msg": "bad"}
        sr.send_shift_report("2026-09-23", "day")
        check("report: откат на текст", sent == ["text"], sent)
    finally:
        sr.shift_report_data = original_data
        sr.send_card_via_hook = original_card
        sr.send_text_via_hook = original_text
        sr.download_json = original_marker_get
        sr.upload_json = original_marker_put


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
        test_send_fallback_scoped_to_groups,
        test_stats_command_matches_report,
        test_self_deleting_confirmations,
        test_text_truncation_helpers,
        test_stats_command_validates_date,
        test_images_janitor_removes_old_files,
        test_shift_stats_endpoint_token,
        test_update_key_and_dedupe_after_success,
        test_polling_persists_offset,
        test_report_sent_ok_semantics,
        test_health_reports_degraded,
        test_parser_and_count_guards,
        test_stats_endpoint_handles_db_error,
        test_edit_message_truncates,
        test_missing_robot_is_queued_and_reported,
        test_bot_forwards_missing_robot_to_lark,
        test_lease_acquire_and_refresh,
        test_lease_release_and_shutdown_handler,
        test_lease_held_by_other_and_takeover,
        test_lease_edge_cases_and_polling_guard,
        test_robot_status_module,
        test_offline_command_validation,
        test_status_flow_end_to_end,
        test_status_flow_cancel_and_fallbacks,
        test_report_previous_shift,
        test_report_formatting,
        test_report_metrics_and_text,
        test_report_empty_shift,
        test_report_card_structure_and_colors,
        test_report_send_fallback,
        test_report_sent_only_once_per_shift,
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
