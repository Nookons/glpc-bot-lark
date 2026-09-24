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

# load_dotenv() не перезаписывает уже заданные переменные, поэтому здесь
# подставляем заведомо нерабочие значения: если какой-то вызов забудут
# заглушить, тест упадёт, а не постучится в боевую базу из .env.
os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"

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
        "reply_to": reply_to_message_id,
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


def fake_get_employee_name(telegram_id, strict=False):
    return LINKS.get(telegram_id)


def fake_get_employee(telegram_id, strict=False):
    if telegram_id not in LINKS:
        return None

    return {
        "user_name": LINKS[telegram_id],
        "card_id": 60072001,
        "is_leader": False,
        "home_warehouse": "GLP-C",
    }


def fake_find_robot(robot_number, warehouse=None, strict=False):
    robot = ROBOTS.get(str(robot_number).strip().lstrip("#"))

    if not robot or warehouse is None:
        return robot

    own = robot.get("warehouse")

    # Робот принадлежит своему складу: для чужого склада его нет.
    if own and own != warehouse:
        return None

    return robot


def fake_find_robot_by_id(robot_id, strict=False):
    for row in ROBOTS.values():
        if str(row.get("id")) == str(robot_id):
            return row

    return None


def fake_resolve_employee_name(raw_name, strict=False):
    if raw_name.strip().casefold() == "ivan":
        return "Ivan Petrenko", []

    return None, ["Ivan Petrenko", "Ivanov Petr"]


def fake_link_user(telegram_id, username, employee_name):
    LINKS[telegram_id] = employee_name
    return True


def fake_unlink_user(telegram_id):
    LINKS.pop(telegram_id, None)
    return True


def fake_send_to_data_base(parsed, data_obj, chat_id, defer_missing=False,
                           warehouse=None):
    DB_CALLS.append({
        "parsed": parsed,
        "data": data_obj,
        "chat_id": chat_id,
        "warehouse": warehouse,
    })
    return {
        "status": "saved",
        "rows": [{"id": 1}],
        "glpc_id": 42,
        "exception_id": 41,
        "robot": parsed["robot"],
    }


QUEUED_ROBOTS = []


def fake_queue_missing_robot(robot_number, employee_card_id=None, chat_id=None,
                             notify=True, warehouse=None):
    QUEUED_ROBOTS.append({
        "robot": str(robot_number),
        "employee_card_id": employee_card_id,
        "chat_id": chat_id,
    })
    return True


def fake_count_robot_errors_in_shift(robot, shift_date, shift_name, warehouse=None):
    return COUNTS.get(str(robot), 0)


def fake_forward_error(parsed, table_lines=None, warehouse=None):
    FORWARDED.append({"parsed": parsed, "lines": table_lines, "warehouse": warehouse})
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
bot.queue_missing_robot = fake_queue_missing_robot

# По умолчанию похожих номеров нет: тесты про подсказку подменяют это сами,
# иначе каждый «неизвестный робот» ходил бы в базу за списком номеров.
import robot_card as robot_card_module  # noqa: E402

REAL_SUGGEST_ROBOT_NUMBERS = robot_card_module.suggest_robot_numbers
robot_card_module.suggest_robot_numbers = lambda *args, **kwargs: []

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
    QUEUED_ROBOTS.clear()
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
    bot.send_to_data_base = lambda parsed, data_obj, chat_id, defer_missing=False, warehouse=None: None

    try:
        sent = run(make_update(text="Unable to drive: Security module failure. 3780"))
    finally:
        bot.send_to_data_base = original

    check("not saved: карточка в Lark не уходит", FORWARDED == [], FORWARDED)
    check("not saved: подтверждения нет", sent == [], sent)


def test_photo_with_error_caption_creates_one_record():
    original_flag = bot.PHOTO_ATTACH_ENABLED
    bot.PHOTO_ATTACH_ENABLED = True

    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    original_combined = bot.send_error_with_photo
    original_store = bot.store_photo_for_record

    combined, stored = [], []

    bot.send_error_with_photo = (
        lambda parsed, lines, photo_path=None, photo_url=None, warehouse=None: (
            combined.append({"parsed": parsed, "path": photo_path}), "link"
        )[1]
    )
    bot.store_photo_for_record = lambda saved, path: (
        stored.append((saved.get("glpc_id"), path)), "https://storage/photo.jpg"
    )[1]

    try:
        sent = run(make_update(
            photo=True,
            caption="Unable to drive: Security module failure. 3780",
            thread_id=2,
        ))
    finally:
        bot.send_error_with_photo = original_combined
        bot.store_photo_for_record = original_store
        bot.PHOTO_ATTACH_ENABLED = original_flag

    check("photo caption: запись создана", len(DB_CALLS) == 1, DB_CALLS)
    check(
        "photo caption: робот взят из подписи",
        DB_CALLS and DB_CALLS[0]["parsed"]["robot"] == "3780",
        DB_CALLS,
    )
    check(
        "photo caption: фото привязано к записи",
        stored and stored[0][0] == 42,
        stored,
    )
    check(
        "photo caption: в Lark ушло одно сообщение с фото",
        len(combined) == 1 and combined[0]["path"],
        combined,
    )
    check(
        "photo caption: подтверждение помечает фото",
        sent and "+ photo" in sent[0]["text"],
        sent,
    )


def test_photo_waits_for_text_then_combines():
    original_flag = bot.PHOTO_ATTACH_ENABLED
    bot.PHOTO_ATTACH_ENABLED = True

    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    with bot._photo_lock:
        bot._pending_photo.clear()
        bot._last_error.clear()

    original_combined = bot.send_error_with_photo
    original_store = bot.store_photo_for_record

    combined = []
    bot.send_error_with_photo = (
        lambda parsed, lines, photo_path=None, photo_url=None, warehouse=None: (
            combined.append(photo_path), "link"
        )[1]
    )
    bot.store_photo_for_record = lambda saved, path: "https://storage/p.jpg"

    try:
        sent = run(make_update(photo=True, thread_id=2))

        with bot._photo_lock:
            queued = len(bot._pending_photo)

        check(
            "photo hold: фото отложено и есть подсказка",
            queued == 1 and "Photo received" in sent[0]["text"],
            (queued, sent),
        )

        sent = run(make_update(
            text="Unable to drive: Security module failure. 3780",
            thread_id=2,
        ))
    finally:
        bot.send_error_with_photo = original_combined
        bot.store_photo_for_record = original_store
        bot.PHOTO_ATTACH_ENABLED = original_flag

        with bot._photo_lock:
            bot._pending_photo.clear()
            bot._last_error.clear()

    check("photo hold: запись создана", len(DB_CALLS) == 1, DB_CALLS)
    check(
        "photo hold: ожидавшее фото прикреплено к записи",
        combined and combined[0],
        combined,
    )
    check(
        "photo hold: подтверждение с фото",
        any("+ photo" in item["text"] for item in sent),
        sent,
    )


def test_photo_attaches_to_recent_error():
    original_flag = bot.PHOTO_ATTACH_ENABLED
    bot.PHOTO_ATTACH_ENABLED = True

    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    with bot._photo_lock:
        bot._pending_photo.clear()
        bot._last_error.clear()

    run(make_update(
        text="Unable to drive: Security module failure. 3780",
        thread_id=2,
    ))

    check(
        "photo after: последняя запись запомнена",
        bot.recent_last_error(-500, 100) is not None,
    )

    original_send = bot.send_photo
    original_set = bot.set_exception_photo

    captions, patched_photo = [], []
    bot.send_photo = lambda path, caption=None, console=None, warehouse=None: (
        captions.append(caption), {"mode": "link", "url": "https://storage/p.jpg"}
    )[1]
    bot.set_exception_photo = lambda table, row_id, url: (
        patched_photo.append((table, row_id, url)), True
    )[1]

    try:
        sent = run(make_update(photo=True, thread_id=2))
    finally:
        bot.send_photo = original_send
        bot.set_exception_photo = original_set
        bot.PHOTO_ATTACH_ENABLED = original_flag

        with bot._photo_lock:
            bot._pending_photo.clear()
            bot._last_error.clear()

    check(
        "photo after: фото привязано к последней записи",
        patched_photo and patched_photo[0][0] == "exceptions_glpc"
        and patched_photo[0][1] == 42,
        patched_photo,
    )
    check(
        "photo after: картинка ушла с номером робота",
        captions and "3780" in (captions[0] or ""),
        captions,
    )
    check(
        "photo after: подтверждение о прикреплении",
        sent and "attached" in sent[0]["text"],
        sent,
    )


def test_db_error_is_not_reported_as_unregistered():
    """Сбой чтения БД не должен выглядеть как «вы не зарегистрированы»."""
    from telegram_store import StoreUnavailable

    original_name = bot.get_employee_name
    original_resolve = bot.resolve_employee_name

    def boom(*args, **kwargs):
        raise StoreUnavailable("db down")

    bot.get_employee_name = boom

    try:
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3780",
            thread_id=2,
        ))
    finally:
        bot.get_employee_name = original_name

    check(
        "db error: пишем о сбое базы, а не «not registered»",
        sent and "database" in sent[0]["text"].lower(),
        sent,
    )
    check("db error: в базу ничего не пишем", DB_CALLS == [], DB_CALLS)

    bot.resolve_employee_name = boom

    try:
        sent = run(make_update(text="/reg Ivan", thread_id=2))
    finally:
        bot.resolve_employee_name = original_resolve

    check(
        "db error: /reg сообщает о сбое базы",
        sent and "database" in sent[0]["text"].lower(),
        sent,
    )


def test_send_to_data_base_db_error_paths():
    """Ошибка чтения сотрудника/робота — не «не найден»."""
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
    failing_table = ["employees"]

    def fake_get(table, params=None):
        if table in failing_table:
            return None

        if table == "issue_templates":
            return [template]

        if table == "employees":
            return [{"card_id": 1, "user_name": "Ivan", "home_warehouse": "GLP-C"}]

        return []

    stdb._rest_get = fake_get
    stdb._rest_post = lambda table, payload, ignore_conflict=False: (
        posted.append(table), [{}]
    )[1]
    stdb.notify_user = lambda chat_id, text: notified.append(text)

    try:
        result = stdb.send_to_data_base(
            parsed,
            {"employee": "Ivan", "robot": "3884",
             "error_text": "Security module failure"},
            -500,
        )

        check(
            "db error: сотрудник — сообщаем о сбое, а не «не найден»",
            notified and "database" in notified[0].lower(),
            notified,
        )
        check(
            "db error: при сбое чтения сотрудника в базу не пишем",
            posted == [] and result is None,
            (posted, result),
        )

        # Теперь сбой на чтении робота: в очередь фантомного робота не пишем.
        failing_table[0] = "robots_maintenance_list"
        posted.clear()
        notified.clear()

        result = stdb.send_to_data_base(
            parsed,
            {"employee": "Ivan", "robot": "3884",
             "error_text": "Security module failure"},
            -500,
        )

        check(
            "db error: робот — сообщаем о сбое",
            notified and "database" in notified[0].lower(),
            notified,
        )
        check(
            "db error: фантомного робота в очередь не пишем",
            posted == [] and result is None,
            (posted, result),
        )
    finally:
        stdb._rest_get = original_get
        stdb._rest_post = original_post
        stdb.notify_user = original_notify


def test_shutdown_saves_offset_and_releases_lease():
    original_save = bot.save_offset
    original_release = bot_lease.release
    original_exit = os._exit
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_grace = bot.SHUTDOWN_GRACE_SECONDS
    original_offset = bot._CURRENT_OFFSET

    saved, released = [], []

    bot.save_offset = lambda offset: (saved.append(offset), True)[1]
    bot_lease.release = lambda holder=None: (released.append(holder), True)[1]
    bot.SHUTDOWN_GRACE_SECONDS = 0
    bot._CURRENT_OFFSET = 4242
    os._exit = lambda code=None: None

    try:
        bot.install_shutdown_handler("me-offset")
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
    finally:
        bot.save_offset = original_save
        bot_lease.release = original_release
        bot.SHUTDOWN_GRACE_SECONDS = original_grace
        bot._CURRENT_OFFSET = original_offset
        os._exit = original_exit
        signal.signal(signal.SIGTERM, original_sigterm)

    check("shutdown: offset сохранён перед выходом", saved == [4242], saved)
    check("shutdown: лиз отпущен", released == ["me-offset"], released)


def test_user_commands_are_deleted():
    """Команды боту бот затирает, сообщения об ошибках — нет."""
    LINKS[100] = "Ivan Petrenko"
    COUNTS["3780"] = 1

    original_flag = bot.DELETE_USER_MESSAGES
    bot.DELETE_USER_MESSAGES = True

    try:
        sent = run(make_update(text="/stats", message_id=9001, thread_id=2))
        command_deleted = (-500, 9001) in DELETED

        check("delete: команда удалена", command_deleted, DELETED)
        check("delete: ответ на команду остался", len(sent) >= 1, sent)

        run(make_update(
            text="Unable to drive: Security module failure. 3780",
            message_id=9002,
            thread_id=2,
        ))
        check(
            "delete: сообщение об ошибке не трогаем",
            (-500, 9002) not in DELETED,
            DELETED,
        )

        run(make_update(text="/zzz", message_id=9003, thread_id=2))
        check(
            "delete: неизвестная команда тоже удалена",
            (-500, 9003) in DELETED,
            DELETED,
        )

        bot.DELETE_USER_MESSAGES = False
        run(make_update(text="/help", message_id=9004, thread_id=2))
        check(
            "delete: при выключенном флаге команда остаётся",
            (-500, 9004) not in DELETED,
            DELETED,
        )
    finally:
        bot.DELETE_USER_MESSAGES = original_flag


def test_status_note_deleted_and_change_message_kept():
    """При offline/online бот убирает команду и описание, а смену статуса — оставляет."""
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    original_flag = bot.DELETE_USER_MESSAGES
    original_patch = robot_status.rest_patch
    original_post = robot_status.rest_post
    original_card = bot.send_card_via_hook

    bot.DELETE_USER_MESSAGES = True
    robot_status.rest_patch = lambda table, params, payload: [{}]
    robot_status.rest_post = lambda table, payload: [{}]
    bot.send_card_via_hook = lambda url, card: {"code": 0, "msg": "success"}

    try:
        sent = run(make_update(text="/offline 3783", message_id=9101, thread_id=2))
        command_deleted = (-500, 9101) in DELETED
        prompt_id = sent[0]["message_id"]

        run(make_callback("st:offline:3542:other", message_id=prompt_id, thread_id=2))

        with bot._pending_lock:
            bot._pending_deletions.clear()

        sent = run(make_update(text="сломался датчик", message_id=9102, thread_id=2))
        note_deleted = (-500, 9102) in DELETED
    finally:
        bot.DELETE_USER_MESSAGES = original_flag
        robot_status.rest_patch = original_patch
        robot_status.rest_post = original_post
        bot.send_card_via_hook = original_card
        ROBOTS.clear()

    check("status delete: команда /offline удалена", command_deleted, DELETED)
    check("status delete: описание причины удалено", note_deleted, DELETED)

    change_messages = [
        item for item in sent if "Offline" in item["text"] and "Reason" in item["text"]
    ]
    check(
        "status delete: сообщение о смене статуса отправлено",
        bool(change_messages),
        sent,
    )

    with bot._pending_lock:
        queued = list(bot._pending_deletions)

    check(
        "status delete: сообщение о смене статуса не удаляется",
        queued == [],
        queued,
    )


def test_short_photo_link_and_redirect():
    from supabase_storage import PHOTO_LINK_BASE, short_photo_url

    name = "tg_AQADox9rGxg5oEl-.jpg"
    link = short_photo_url(name)

    check(
        "short link: короткий адрес на нашем домене",
        link == f"{PHOTO_LINK_BASE}/p/{name}",
        link,
    )
    check("short link: заметно короче signed url", len(link) < 100, len(link))
    check(
        "short link: имя файла проверяется",
        bot.is_safe_object_name(name) is True
        and bot.is_safe_object_name("../../etc/passwd") is False
        and bot.is_safe_object_name("bad name!.jpg") is False,
    )

    original = bot.resolve_photo_url
    bot.resolve_photo_url = lambda object_name, strict=False: (
        "https://supabase.example/signed?token=abc"
        if object_name == name
        else None
    )

    client = bot.app.test_client()

    try:
        found = client.get(f"/p/{name}")
        missing = client.get("/p/unknown-file.jpg")
        bad = client.get("/p/bad%20name!.jpg")
    finally:
        bot.resolve_photo_url = original

    check(
        "short link: редирект на подписанный URL",
        found.status_code == 302
        and "supabase" in found.headers.get("Location", ""),
        (found.status_code, found.headers.get("Location")),
    )
    check("short link: неизвестный файл -> 404", missing.status_code == 404, missing.status_code)
    check("short link: опасное имя -> 400", bad.status_code == 400, bad.status_code)


def test_photo_forwarded_when_attachment_disabled():
    """При выключенной привязке фото просто уходит в группу, как раньше."""
    LINKS[100] = "Ivan Petrenko"

    original_flag = bot.PHOTO_ATTACH_ENABLED
    original_send = bot.send_photo

    calls = []
    bot.PHOTO_ATTACH_ENABLED = False
    bot.send_photo = lambda path, caption=None, console=None, warehouse=None: (
        calls.append(caption), {"mode": "link", "url": "https://x/p/1"}
    )[1]

    try:
        sent = run(make_update(photo=True, caption="Общий план робота", thread_id=2))
    finally:
        bot.PHOTO_ATTACH_ENABLED = original_flag
        bot.send_photo = original_send

    check(
        "photo off: фото уходит сразу с именем сотрудника",
        calls and "Photo from Ivan Petrenko" in calls[0],
        calls,
    )
    check(
        "photo off: подпись сотрудника сохраняется",
        calls and "Общий план робота" in calls[0],
        calls,
    )
    check("photo off: запись об ошибке не создаётся", DB_CALLS == [], DB_CALLS)
    check(
        "photo off: подтверждение про ссылку",
        sent and "as a link" in sent[0]["text"],
        sent,
    )


def test_flush_pending_photo_modes():
    """Отложенное фото, не дождавшееся текста, уходит отдельно."""
    LINKS[100] = "Ivan Petrenko"

    for mode, marker in (
        ("lark", "Photo forwarded"),
        ("link", "as a link"),
        ("none", "Can\'t forward"),
    ):
        original = bot.send_photo
        bot.send_photo = (
            lambda path, caption=None, console=None, warehouse=None, _mode=mode: {
                "mode": _mode,
                "url": None,
            }
        )

        try:
            SENT.clear()
            bot.flush_pending_photo(-500, 100, {"path": "x.jpg", "caption": "cap"})
        finally:
            bot.send_photo = original

        check(
            f"photo flush {mode}: ответ сотруднику",
            any(marker in item["text"] for item in SENT),
            (mode, SENT),
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


def test_lark_hook_post_never_raises():
    """Любой сбой вебхука -> {"code": -1}, а не исключение."""
    import lark_media as lm
    import requests as _requests

    original_post = lm.requests.post

    class _BadResponse:
        status_code = 502
        content = b"<html>bad gateway</html>"

        def json(self):
            raise ValueError("not json")

    try:
        # 1) сеть/таймаут
        def boom(*args, **kwargs):
            raise _requests.exceptions.Timeout("timed out")

        lm.requests.post = boom
        result = lm._hook_post("https://hook.example", {"msg_type": "text"})

        check(
            "hook: таймаут -> мягкая ошибка, без исключения",
            isinstance(result, dict) and result.get("code") == -1,
            result,
        )

        # 2) не-JSON ответ
        lm.requests.post = lambda *args, **kwargs: _BadResponse()
        result = lm._hook_post("https://hook.example", {"msg_type": "text"})

        check(
            "hook: не-JSON -> мягкая ошибка, без исключения",
            isinstance(result, dict) and result.get("code") == -1,
            result,
        )

        # 3) проверяем, что и обёртки не бросают
        check(
            "hook: send_text_via_hook не бросает",
            lm.send_text_via_hook("https://hook.example", "текст").get("code") == -1,
        )
        check(
            "hook: send_card_via_hook не бросает",
            lm.send_card_via_hook("https://hook.example", {"a": 1}).get("code") == -1,
        )
    finally:
        lm.requests.post = original_post


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
        len(sent) == 1 and "Monitored: GLP-C: topic id 555" in sent[0]["text"],
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
        bot_lease.table_probe,
        bot_lease.rest_get,
        bot_lease.rest_post,
        bot_lease.rest_patch,
    )

    bot_lease._table_ok = None
    bot_lease._table_checked_at = 0.0
    bot_lease._warned_no_table = False

    bot_lease.table_probe = lambda table_name: (
        "ok" if table else "missing"
    )
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
        bot_lease.table_probe,
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


def test_failed_update_is_retried_then_skipped():
    """Падение обработки не подтверждает апдейт: Telegram пришлёт его снова."""
    import threading as _threading

    original_handle = bot.handle_update
    original_save = bot.save_offset
    original_load = bot.load_saved_offset
    original_attempts = bot.MAX_UPDATE_ATTEMPTS

    update = make_update(text="/help", message_id=8801)

    def boom(*args, **kwargs):
        raise RuntimeError("обработка упала")

    # 1) одна попытка: апдейт не подтверждён
    stop = _threading.Event()
    saved, batches = [], [[update]]

    def fake_get_updates(offset=None, timeout=30):
        if batches:
            return batches.pop(0)

        stop.set()
        return []

    bot.handle_update = boom
    bot.save_offset = lambda offset: (saved.append(offset), True)[1]
    bot.load_saved_offset = lambda: None
    bot.MAX_UPDATE_ATTEMPTS = 3

    original_updates = tg.get_updates
    tg.get_updates = fake_get_updates

    try:
        bot.polling_loop(stop, None)
    finally:
        tg.get_updates = original_updates

    check(
        "update retry: упавший апдейт не подтверждается",
        saved == [],
        saved,
    )

    # 2) три попытки: апдейт пропускается, offset двигается дальше
    stop = _threading.Event()
    saved, batches = [], [[update], [update], [update]]

    bot.save_offset = lambda offset: (saved.append(offset), True)[1]

    def fake_get_updates2(offset=None, timeout=30):
        if batches:
            return batches.pop(0)

        stop.set()
        return []

    tg.get_updates = fake_get_updates2

    try:
        bot.polling_loop(stop, None)
    finally:
        tg.get_updates = original_updates
        bot.handle_update = original_handle
        bot.save_offset = original_save
        bot.load_saved_offset = original_load
        bot.MAX_UPDATE_ATTEMPTS = original_attempts

    check(
        "update retry: после лимита апдейт пропускается, offset едет дальше",
        len(saved) == 1 and saved[0] == update["update_id"] + 1,
        saved,
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


def test_rest_post_ignores_conflict():
    """409 при вставке в очередь — «уже есть», а не ошибка в логах."""
    import sendToDataBase as stdb
    import requests as _requests

    class _ConflictResponse:
        status_code = 409
        text = "conflict"
        content = b"conflict"

        def raise_for_status(self):
            raise _requests.exceptions.HTTPError("409 Conflict", response=self)

    original_post = stdb.requests.post
    stdb.requests.post = lambda *args, **kwargs: _ConflictResponse()

    try:
        ignored = stdb._rest_post(
            "robots_to_add", {"robot_number": 1}, ignore_conflict=True
        )
        strict = stdb._rest_post("robots_to_add", {"robot_number": 1})
    finally:
        stdb.requests.post = original_post

    check("409: для очереди — не ошибка", ignored == [], ignored)
    check("409: без флага — ошибка", strict is None, strict)


def test_parser_and_count_guards():
    from error_parser import parse_error_message

    check("parser: None не роняет разбор", parse_error_message(None) is None)
    check("parser: пустая строка", parse_error_message("   ") is None)

    trailing = parse_error_message("Unable to drive: Security module failure. 3780.")
    check(
        "parser: точка в конце не ломает разбор",
        trailing and trailing["robot"] == "3780",
        trailing,
    )

    dotted = parse_error_message("first: text with. dot inside. 123")
    check(
        "parser: точки внутри описания сохраняются",
        dotted and dotted["error_text"] == "text with. dot inside",
        dotted,
    )

    import sendToDataBase as stdb

    original_count = stdb.rest_count
    captured = {}

    def fake_count(table, params=None):
        captured["table"] = table
        captured["params"] = params
        return 2

    stdb.rest_count = fake_count

    try:
        count = stdb.count_robot_errors_in_shift(3783, "2026-09-23", "day")
    finally:
        stdb.rest_count = original_count

    check("count: возвращает точное число с базы", count == 2, count)
    check(
        "count: фильтрует по роботу, смене и складу",
        captured.get("params", {}).get("error_robot") == "eq.3783"
        and captured["params"].get("issue_data") == "eq.2026-09-23"
        and captured["params"].get("shift_type") == "eq.day"
        and captured["params"].get("warehouse") == "eq.GLP-C",
        captured.get("params"),
    )

    stdb.rest_count = lambda table, params=None: None

    try:
        broken = stdb.count_robot_errors_in_shift(3783, "2026-09-23", "day")
    finally:
        stdb.rest_count = original_count

    check("count: сбой подсчёта -> 0, без падения", broken == 0, broken)


def test_rest_count_parses_content_range():
    """Точный подсчёт читает Content-Range, а не считает строки в Python."""
    import sendToDataBase as stdb

    class _Resp:
        status_code = 200
        content = b"[]"
        text = "[]"

        def __init__(self, content_range):
            self.headers = {"Content-Range": content_range} if content_range else {}

        def raise_for_status(self):
            return None

        def json(self):
            return []

    original_get = stdb.requests.get

    try:
        stdb.requests.get = lambda *a, **k: _Resp("*/42")
        total = stdb.rest_count("exceptions_glpc", {"issue_data": "eq.2026-09-23"})

        stdb.requests.get = lambda *a, **k: _Resp("0-0/7")
        ranged = stdb.rest_count("exceptions_glpc")

        stdb.requests.get = lambda *a, **k: _Resp(None)
        missing = stdb.rest_count("exceptions_glpc")
    finally:
        stdb.requests.get = original_get

    check("count: разбирает Content-Range */N", total == 42, total)
    check("count: разбирает Content-Range 0-0/N", ranged == 7, ranged)
    check("count: без Content-Range -> None", missing is None, missing)


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
    stdb._rest_post = lambda table, payload, ignore_conflict=False: (
        posted.append((table, payload)), [{}]
    )[1]
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

    bot.send_to_data_base = lambda parsed, data_obj, chat_id, defer_missing=False, warehouse=None: {
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


# ============================================================
# ПРАВКИ ПО РЕЗУЛЬТАТАМ АУДИТА (M/L/T)
# ============================================================

def test_offset_saved_for_every_confirmed_update():
    """M7: offset пишется после каждого апдейта, а не только в конце батча."""
    stop = threading.Event()
    saved = []
    handled = []
    first = make_update(text="/help", message_id=7001)
    second = make_update(text="/help", message_id=7002)
    batches = [[first, second]]

    def fake_get_updates(offset=None, timeout=30):
        if batches:
            return batches.pop(0)

        stop.set()
        return []

    original = (
        tg.get_updates,
        bot.save_offset,
        bot.load_saved_offset,
        bot.handle_update,
    )

    tg.get_updates = fake_get_updates
    bot.save_offset = lambda offset: (saved.append(offset), True)[1]
    bot.load_saved_offset = lambda: None
    bot.handle_update = lambda update, username: handled.append(update["update_id"])

    try:
        bot.polling_loop(stop, None)
    finally:
        (
            tg.get_updates,
            bot.save_offset,
            bot.load_saved_offset,
            bot.handle_update,
        ) = original

    check(
        "offset M7: оба апдейта батча подтверждены и сохранены",
        saved == [first["update_id"] + 1, second["update_id"] + 1],
        saved,
    )


def test_failed_update_never_confirms_offset():
    """M7: упавший апдейт не подтверждаем — Telegram пришлёт его снова."""
    stop = threading.Event()
    saved = []
    update = make_update(text="/help", message_id=7003)
    batches = [[update]]

    def fake_get_updates(offset=None, timeout=30):
        if batches:
            return batches.pop(0)

        stop.set()
        return []

    def boom(update, username):
        raise RuntimeError("handler failed")

    original = (
        tg.get_updates,
        bot.save_offset,
        bot.load_saved_offset,
        bot.handle_update,
    )

    tg.get_updates = fake_get_updates
    bot.save_offset = lambda offset: (saved.append(offset), True)[1]
    bot.load_saved_offset = lambda: None
    bot.handle_update = boom

    try:
        bot.polling_loop(stop, None)
    finally:
        (
            tg.get_updates,
            bot.save_offset,
            bot.load_saved_offset,
            bot.handle_update,
        ) = original

    check("offset M7: упавший апдейт не подтверждён", saved == [], saved)


def test_status_note_kept_when_db_write_fails():
    """M6: при ошибке записи причина сотрудника остаётся в чате."""
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    sender = {"id": 100, "username": "tester", "first_name": "Tester"}
    pending = {
        "direction": "offline",
        "robot_number": "3783",
        "type_problem": "Other",
        "prompt_message_id": 555,
    }

    original = (
        robot_status.change_robot_status,
        bot.send_card_via_hook,
        bot.DELETE_USER_MESSAGES,
    )

    bot.DELETE_USER_MESSAGES = True
    bot.send_card_via_hook = lambda url, card: {"code": 0, "msg": "success"}
    DELETED.clear()

    chat = {"id": -500, "type": "supergroup"}

    try:
        robot_status.change_robot_status = lambda *a, **k: None
        bot.set_pending_status(-500, 100, dict(pending))
        bot._handle_text_message(-500, sender, "сломан ролик", 7201, chat)

        check(
            "status M6: текст сотрудника не удалён",
            (-500, 7201) not in DELETED,
            DELETED,
        )
        check(
            "status M6: флоу не потерян, причину можно повторить",
            bot.peek_pending_status(-500, 100) is not None,
        )

        robot_status.change_robot_status = lambda *a, **k: {
            "robot": ROBOTS["3783"],
            "old_status": robot_status.ONLINE,
            "new_status": robot_status.OFFLINE,
            "type_problem": "Other",
            "problem_note": "сломан ролик",
            "changed_at": "2026-03-08T10:00:00+00:00",
            "history_saved": True,
        }

        bot.set_pending_status(-500, 100, dict(pending, prompt_message_id=556))
        bot._handle_text_message(-500, sender, "сломан ролик", 7202, chat)

        check("status M6: при успехе текст удаляем", (-500, 7202) in DELETED, DELETED)
        check("status M6: флоу закрыт", bot.peek_pending_status(-500, 100) is None)
    finally:
        (
            robot_status.change_robot_status,
            bot.send_card_via_hook,
            bot.DELETE_USER_MESSAGES,
        ) = original
        ROBOTS.clear()


def test_command_replies_do_not_reply_to_deleted_message():
    """L1: команду бот удаляет, поэтому ответ не должен ссылаться на неё."""
    LINKS[100] = "Ivan Petrenko"
    original_flag = bot.DELETE_USER_MESSAGES
    bot.DELETE_USER_MESSAGES = True

    try:
        sent = run(make_update(text="/whoami", message_id=7301, thread_id=2))
    finally:
        bot.DELETE_USER_MESSAGES = original_flag

    check("commands L1: команда удалена", (-500, 7301) in DELETED, DELETED)
    check(
        "commands L1: ответ не привязан к удалённому сообщению",
        bool(sent) and all(item.get("reply_to") is None for item in sent),
        sent,
    )


def test_short_link_negative_cache_and_lru():
    """M3: /p/ не должен бесконечно подписывать несуществующие имена."""
    import supabase_storage as storage

    calls = []
    original = storage.create_signed_url

    storage._signed_cache.clear()
    storage._signed_missing.clear()
    storage.create_signed_url = lambda object_name, expires_in=None, strict=False: (
        calls.append(object_name), None
    )[1]

    try:
        first = storage.resolve_photo_url("missing-1.jpg")
        second = storage.resolve_photo_url("missing-1.jpg")
    finally:
        storage.create_signed_url = original
        storage._signed_missing.clear()

    check("short link M3: промах -> None", first is None and second is None, (first, second))
    check("short link M3: повторный промах берётся из кэша", calls == ["missing-1.jpg"], calls)

    storage._signed_cache.clear()

    for index in range(storage._SIGNED_CACHE_MAX + 20):
        storage._cache_put(
            storage._signed_cache,
            f"file-{index}.jpg",
            ("https://signed", time.time() + 3600),
        )

    check(
        "short link M3: размер кэша ограничен",
        len(storage._signed_cache) == storage._SIGNED_CACHE_MAX,
        len(storage._signed_cache),
    )
    check(
        "short link M3: самые старые записи вытеснены",
        "file-0.jpg" not in storage._signed_cache,
    )

    storage._signed_cache.clear()


def test_short_link_distinguishes_storage_failure():
    """M3: сбой Storage — это 503, а не «файла нет»."""
    import supabase_storage as storage

    original_create = storage.create_signed_url
    original_resolve = bot.resolve_photo_url

    storage._signed_cache.clear()
    storage._signed_missing.clear()

    def failing_resolve(object_name, strict=False):
        raise storage.StorageUnavailable("network down")

    def boom(object_name, expires_in=None, strict=False):
        if strict:
            raise storage.StorageUnavailable("network down")

        return None

    try:
        raised = False

        try:
            storage.resolve_photo_url("photo.jpg", strict=True)
        except storage.StorageUnavailable:
            raised = True

        check("short link M3: strict -> StorageUnavailable", raised)

        storage.create_signed_url = boom
        storage._signed_missing.clear()

        check(
            "short link M3: без strict сбой -> None",
            storage.resolve_photo_url("photo.jpg") is None,
        )

        bot.resolve_photo_url = failing_resolve
        response = bot.app.test_client().get("/p/photo.jpg")

        check(
            "short link M3: Storage лежит -> 503",
            response.status_code == 503,
            response.status_code,
        )
    finally:
        storage.create_signed_url = original_create
        bot.resolve_photo_url = original_resolve
        storage._signed_missing.clear()
        storage._signed_cache.clear()


def test_bucket_public_flag_updates_existing_bucket():
    """M2: PUBLIC_PHOTO_URLS должен переводить существующий bucket в public."""
    import supabase_storage as storage

    class FakeResponse:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

        def json(self):
            try:
                return json.loads(self.text or "{}")
            except ValueError:
                return {}

    calls = {"post": [], "put": []}
    original_post = storage.requests.post
    original_put = storage.requests.put

    def fake_post(url, **kwargs):
        calls["post"].append((url, kwargs.get("json")))
        # Именно так отвечает боевой Supabase: HTTP 400, а «409» — в теле.
        return FakeResponse(
            400,
            '{"statusCode":"409","error":"Duplicate",'
            '"message":"The resource already exists",'
            '"code":"BucketAlreadyExists"}',
        )

    def fake_put(url, **kwargs):
        calls["put"].append((url, kwargs.get("json")))
        return FakeResponse(200, "{}")

    storage.requests.post = fake_post
    storage.requests.put = fake_put
    storage._buckets_ok.clear()

    try:
        public_ok = storage.ensure_bucket_named("bot-photos", public=True)

        storage._buckets_ok.clear()
        private_ok = storage.ensure_bucket_named("bot-photos", public=False)
    finally:
        storage.requests.post = original_post
        storage.requests.put = original_put
        storage._buckets_ok.clear()

    check(
        "bucket M2: существующий bucket переводится в public",
        public_ok is True and len(calls["put"]) == 1,
        calls,
    )
    check(
        "bucket M2: PUT несёт public=true",
        bool(calls["put"]) and calls["put"][0][1] == {"public": True},
        calls["put"],
    )
    check(
        "bucket M2: приватному bucket PUT не нужен",
        private_ok is True and len(calls["put"]) == 1,
        calls["put"],
    )

    # Настоящая ошибка запроса существующим bucket не считается
    storage._buckets_ok.clear()
    storage.requests.post = lambda url, **kwargs: FakeResponse(
        400, '{"error":"Invalid bucket name"}'
    )

    try:
        real_error = storage.ensure_bucket_named("bot-photos", public=False)
    finally:
        storage.requests.post = fake_post
        storage._buckets_ok.clear()

    check("bucket M2: настоящая 400 — это ошибка", real_error is False, real_error)


def test_table_probe_and_lease_error_caching():
    """L6: сетевой сбой — это не «таблицы нет» и он не кэшируется надолго."""
    import sendToDataBase as stdb

    class FakeResponse:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {}

    original_get = stdb.requests.get

    try:
        stdb.requests.get = lambda url, **kwargs: FakeResponse(200)
        ok_state = stdb.table_probe("telegram_users")

        stdb.requests.get = lambda url, **kwargs: FakeResponse(404, "not found")
        missing_state = stdb.table_probe("telegram_users")

        stdb.requests.get = lambda url, **kwargs: FakeResponse(503, "unavailable")
        server_state = stdb.table_probe("telegram_users")

        def boom(url, **kwargs):
            raise stdb.requests.exceptions.RequestException("no network")

        stdb.requests.get = boom
        error_state = stdb.table_probe("telegram_users")
    finally:
        stdb.requests.get = original_get

    check("probe L6: 200 -> ok", ok_state == "ok", ok_state)
    check("probe L6: 404 -> missing", missing_state == "missing", missing_state)
    check("probe L6: 5xx -> error", server_state == "error", server_state)
    check("probe L6: сеть -> error", error_state == "error", error_state)

    probes = []
    original_probe = bot_lease.table_probe
    original_state = (bot_lease._table_ok, bot_lease._table_checked_at)

    bot_lease.table_probe = lambda table: (probes.append(table), "error")[1]
    bot_lease._table_ok = None
    bot_lease._table_checked_at = 0.0

    try:
        bot_lease._has_table()
        bot_lease._has_table()
    finally:
        bot_lease.table_probe = original_probe

    check(
        "lease L6: сбой проверки не запоминается как «таблицы нет»",
        bot_lease._table_ok is None,
        bot_lease._table_ok,
    )
    check("lease L6: сбой проверки не кэшируется", probes == ["bot_leases"], probes)

    probes_missing = []
    bot_lease.table_probe = lambda table: (probes_missing.append(table), "missing")[1]
    bot_lease._table_ok = None
    bot_lease._table_checked_at = 0.0

    try:
        bot_lease._has_table()
        bot_lease._has_table()
    finally:
        bot_lease.table_probe = original_probe
        bot_lease._table_ok, bot_lease._table_checked_at = original_state

    check(
        "lease L6: реальное отсутствие таблицы кэшируется",
        probes_missing == ["bot_leases"],
        probes_missing,
    )


def test_flush_expired_photos_noop_when_disabled():
    """T4: с выключенной привязкой фото фоновый flush ничего не делает."""
    original_flag = bot.PHOTO_ATTACH_ENABLED
    original_flush = bot.flush_pending_photo
    calls = []
    key = (-500, 100)

    bot.PHOTO_ATTACH_ENABLED = False
    bot.flush_pending_photo = lambda *a, **k: calls.append(a)

    with bot._photo_lock:
        bot._pending_photo[key] = {
            "path": "/tmp/x.jpg",
            "message_id": 1,
            "expires": time.time() - 10,
        }

    try:
        bot.flush_expired_photos()

        with bot._photo_lock:
            still_there = key in bot._pending_photo
    finally:
        bot.PHOTO_ATTACH_ENABLED = original_flag
        bot.flush_pending_photo = original_flush

        with bot._photo_lock:
            bot._pending_photo.pop(key, None)

    check("photo T4: flush при выключенной привязке — no-op", calls == [], calls)
    check("photo T4: очередь ожидающих фото не тронута", still_there is True)


def test_cache_helpers_are_bounded():
    """L3: словари-кэши не растут бесконечно."""
    store = {}
    flags = set()

    for index in range(bot._CACHE_LIMIT + 5):
        bot._remember_bounded(store, index, index)
        bot._remember_flag(flags, index)

    check(
        "cache L3: словарь ограничен",
        len(store) == bot._CACHE_LIMIT,
        len(store),
    )
    check(
        "cache L3: set ограничен",
        len(flags) == bot._CACHE_LIMIT,
        len(flags),
    )
    check(
        "cache L3: самые старые записи вытеснены",
        0 not in store and 0 not in flags,
        (0 in store, 0 in flags),
    )


def test_user_messages_not_deleted_in_private_chats():
    """L7: в личной переписке чужие сообщения не удаляем."""
    original_flag = bot.DELETE_USER_MESSAGES
    bot.DELETE_USER_MESSAGES = True
    DELETED.clear()

    bot._set_route(-777, None, "private")

    try:
        deleted = bot._delete_user_message(-777, 4242)
    finally:
        bot.DELETE_USER_MESSAGES = original_flag

    check("private L7: удаление в личке не выполняется", deleted is False, deleted)
    check("private L7: deleteMessage не вызывался", (-777, 4242) not in DELETED, DELETED)

# ============================================================
# СПРИНТ 1: КАРТОЧКА РОБОТА, ПОДСКАЗКА НОМЕРА, ОЧЕРЕДЬ
# ============================================================

def test_time_utils_parse_iso():
    """Postgres отдаёт доли секунды с обрезкой нулей — парсер обязан их есть."""
    from datetime import datetime, timezone
    from time_utils import parse_iso

    six = parse_iso("2026-04-09T05:28:16.632623+00:00")
    five = parse_iso("2026-04-09T05:28:16.63262+00:00")
    one = parse_iso("2026-04-09T05:28:16.6Z")
    naive = parse_iso("2026-04-09T05:28:16")

    check(
        "time: 6 знаков после точки",
        six is not None and six.microsecond == 632623,
        six,
    )
    check(
        "time: 5 знаков (обрезанный ноль) читаются",
        five is not None and five.microsecond == 632620,
        five,
    )
    check("time: 1 знак и Z", one is not None and one.microsecond == 600000, one)
    check(
        "time: без доли секунды — считаем UTC",
        naive is not None and naive.tzinfo == timezone.utc,
        naive,
    )
    check("time: мусор -> None", parse_iso("не время") is None)
    check("time: None -> None", parse_iso(None) is None)
    check(
        "time: datetime на входе не ломает",
        parse_iso(datetime(2026, 4, 9, tzinfo=timezone.utc)) is not None,
    )


def test_lease_stale_with_short_fraction():
    """Живой-но-битый формат времени не должен навсегда «держать» лиз."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S") + ".02344+00:00"
    fresh = now.strftime("%Y-%m-%dT%H:%M:%S") + ".02344+00:00"

    check("lease: просроченный heartbeat с 5 знаками = мёртвый", bot_lease._is_stale(old, 90) is True)
    check("lease: свежий heartbeat с 5 знаками = живой", bot_lease._is_stale(fresh, 90) is False)
    check("lease: пустой heartbeat = мёртвый", bot_lease._is_stale(None, 90) is True)
    check("lease: нечитаемое время = живой (не отбираем лиз)", bot_lease._is_stale("мусор", 90) is False)


def test_queue_autoclose_does_not_mix_warehouses():
    """Заявка склада закрывается только роботом ТОГО ЖЕ склада."""
    import robot_queue

    original_get = robot_queue.rest_get
    original_patch = robot_queue.rest_patch

    calls = []

    def fake_get(table, params=None):
        if table == "robots_to_add":
            return [
                {"id": 1, "robot_number": "123", "warehouse": "GLP-C"},
                {"id": 2, "robot_number": "5016", "warehouse": "SMALL-P3"},
            ]

        # у GLP-C робота 123 нет, зато есть у SMALL-P3
        calls.append(params)

        if params.get("warehouse") == "eq.GLP-C":
            return []

        if params.get("warehouse") == "eq.SMALL-P3":
            return [{"robot_number": 5016}]

        return []

    patches = []

    def fake_patch(table, params, payload):
        patches.append(params)

        return []

    robot_queue.rest_get = fake_get
    robot_queue.rest_patch = fake_patch

    try:
        robot_queue.close_queued_robots()
    finally:
        robot_queue.rest_get = original_get
        robot_queue.rest_patch = original_patch

    check(
        "queue: справочник ищется по складу заявки",
        sorted(params.get("warehouse") for params in calls)
        == ["eq.GLP-C", "eq.SMALL-P3"],
        calls,
    )
    check(
        "queue: чужой склад заявку не закрывает",
        patches and all(params.get("warehouse") == "eq.SMALL-P3" for params in patches),
        patches,
    )
    check(
        "queue: PATCH не трогает заявку GLP-C",
        all("123" not in params.get("robot_number", "") for params in patches),
        patches,
    )


def test_queue_autoclose_closes_known_robots():
    """Очередь роботов: заявки, которые уже есть в справочнике, закрываются."""
    import robot_queue

    original_get = robot_queue.rest_get
    original_patch = robot_queue.rest_patch

    calls = {"get": [], "patch": []}

    def fake_get(table, params=None):
        calls["get"].append((table, params))

        if table == "robots_to_add":
            return queue_rows

        if table == "robots_maintenance_list":
            return [{"robot_number": 3458}]

        return []

    def fake_patch(table, params, payload):
        calls["patch"].append((table, params, payload))

        # закрываем только те номера, что реально просили
        listed = params["robot_number"].split("in.(")[1].rstrip(")").split(",")

        return [
            {"id": 1} for number in listed if number == "3458"
        ]

    queue_rows = [
        {"id": 1, "robot_number": "3458", "warehouse": "GLP-C"},
        {"id": 2, "robot_number": "9999", "warehouse": "GLP-C"},
        {"id": 3, "robot_number": "abc", "warehouse": "GLP-C"},
        {"id": 4, "robot_number": "3458", "warehouse": "GLP-C"},
    ]

    robot_queue.rest_get = fake_get
    robot_queue.rest_patch = fake_patch

    try:
        result = robot_queue.close_queued_robots()
    finally:
        robot_queue.rest_get = original_get
        robot_queue.rest_patch = original_patch

    check(
        "queue: проверено 2 уникальных номера, закрыта 1 заявка",
        result == {"checked": 2, "closed": 1, "error": False},
        result,
    )

    patch = calls["patch"][0] if calls["patch"] else ({}, {}, {})
    check(
        "queue: PATCH фильтрует по открытым заявкам и номеру",
        patch[1].get("status") == "is.false" and "3458" in patch[1].get("robot_number", ""),
        patch[1],
    )
    check(
        "queue: closing не трогает ненайденные номера",
        "9999" not in patch[1].get("robot_number", ""),
        patch[1],
    )
    check(
        "queue: мусорные номера не уходят в справочник",
        all(
            "abc" not in str(params)
            for table, params in calls["get"]
            if table == "robots_maintenance_list"
        ),
        calls["get"],
    )


def test_queue_autoclose_db_error_does_not_patch():
    """Сбой чтения — не «нечего закрывать»: PATCH не делаем."""
    import robot_queue

    original_get = robot_queue.rest_get
    original_patch = robot_queue.rest_patch

    patched = []

    robot_queue.rest_get = lambda table, params=None: (
        [{"id": 1, "robot_number": "3458"}]
        if table == "robots_to_add"
        else None
    )
    robot_queue.rest_patch = lambda table, params, payload: patched.append(table) or []

    try:
        lookup_error = robot_queue.close_queued_robots()
    finally:
        robot_queue.rest_get = original_get
        robot_queue.rest_patch = original_patch

    check(
        "queue: сбой чтения справочника -> error, без правок",
        lookup_error["error"] is True and patched == [],
        (lookup_error, patched),
    )

    patched.clear()
    robot_queue.rest_get = lambda table, params=None: None
    robot_queue.rest_patch = lambda table, params, payload: patched.append(table) or []

    try:
        queue_error = robot_queue.close_queued_robots()
    finally:
        robot_queue.rest_get = original_get
        robot_queue.rest_patch = original_patch

    check(
        "queue: сбой чтения очереди -> error, без правок",
        queue_error["error"] is True and patched == [],
        (queue_error, patched),
    )


def test_robot_card_command():
    """/robot: карточка, подсказки, валидация аргумента."""
    LINKS[100] = "Ivan Petrenko"

    original_card = bot.robot_card.robot_card
    original_flag = bot.DELETE_USER_MESSAGES

    bot.DELETE_USER_MESSAGES = True

    card = {
        "found": True,
        "number": "3680",
        "robot": {
            "robot_number": 3680,
            "robot_type": "RT_KUBOT_MINI_HAIFLEX",
            "status": "在线 | Online",
            "warehouse": "GLP-C",
        },
        "errors_available": True,
        "errors": 4,
        "reporters": [("Vladyslav Kovalenko", 2)],
        "last_issues": [{
            "at": "23.09 10:53",
            "issue_type": "Unable to drive",
            "by": "Dmytro Kolomiiets",
        }],
        "history": [{
            "at": "23.09 18:47",
            "old": "离线 | Offline",
            "new": "在线 | Online",
            "type_problem": "Solved without changing",
            "note": "test",
            "by": "Dmytro Kolomiiets",
        }],
    }

    try:
        bot.robot_card.robot_card = lambda number, warehouse=None: card
        sent = run(make_update(text="/robot 3680", message_id=9101, thread_id=2))
        text = sent[0]["text"] if sent else ""

        check(
            "robot card: карточка отправлена",
            "Robot 3680" in text and "RT_KUBOT_MINI_HAIFLEX" in text,
            text,
        )
        check(
            "robot card: ошибки, авторы и история на месте",
            "Issues (7 days): 4" in text
            and "Vladyslav Kovalenko (2)" in text
            and "Unable to drive" in text
            and "History:" in text,
            text,
        )
        check("robot card: команда удалена", (-500, 9101) in DELETED, DELETED)

        bot.robot_card.robot_card = lambda number, warehouse=None: {
            "found": False, "number": number, "suggestions": ["882"],
        }
        sent = run(make_update(text="/robot 3882", thread_id=2))
        check(
            "robot card: предложены похожие номера",
            sent and "882" in sent[0]["text"] and "/robot 882" in sent[0]["text"],
            sent,
        )
    finally:
        bot.robot_card.robot_card = original_card
        bot.DELETE_USER_MESSAGES = original_flag

    sent = run(make_update(text="/robot", thread_id=2))
    check(
        "robot card: без номера — подсказка по формату",
        sent and "Usage: /robot" in sent[0]["text"],
        sent,
    )

    sent = run(make_update(text="/robot abc", thread_id=2))
    check(
        "robot card: буквы отвергаются",
        sent and "must be digits" in sent[0]["text"],
        sent,
    )

    bot.robot_card.robot_card = lambda number, warehouse=None: None

    try:
        sent = run(make_update(text="/robot 3680", thread_id=2))
    finally:
        bot.robot_card.robot_card = original_card

    check(
        "robot card: сбой БД не выдаётся за «робота нет»",
        sent and "Can't read the database" in sent[0]["text"],
        sent,
    )


def _robot_fix_env(candidates=("882",)):
    """Общая обвязка для тестов подсказки номера."""
    original = (
        bot.send_to_data_base,
        bot.queue_missing_robot,
        bot.robot_card.suggest_robot_numbers,
    )

    state = {"saved": [], "queued": []}

    def fake_save(parsed, data_obj, chat_id, defer_missing=False, warehouse=None):
        if defer_missing:
            return {
                "robot_missing": True,
                "robot": parsed["robot"],
                "employee_card_id": 60072001,
            }

        state["saved"].append(parsed["robot"])

        return {
            "status": "saved",
            "glpc_id": 1,
            "exception_id": 2,
            "robot": parsed["robot"],
        }

    bot.send_to_data_base = fake_save
    bot.queue_missing_robot = lambda *args, **kwargs: (
        state["queued"].append(args[0] if args else kwargs.get("robot_number")), True
    )[1]
    bot.robot_card.suggest_robot_numbers = (
        lambda number, limit=None, cutoff=None: list(candidates)
    )

    return state, original


def _robot_fix_restore(original):
    (
        bot.send_to_data_base,
        bot.queue_missing_robot,
        bot.robot_card.suggest_robot_numbers,
    ) = original


def test_robot_fix_suggestion_then_correction():
    """Опечатка в номере: подсказка -> кнопка -> запись с правильным номером."""
    LINKS[100] = "Ivan Petrenko"
    state, original = _robot_fix_env()

    try:
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3882",
            message_id=9201,
            thread_id=2,
        ))
        prompt = sent[0] if sent else {}
        pending = bot.peek_pending_robot_fix(-500, 100)

        SENT.clear()
        FORWARDED.clear()
        QUEUED_ROBOTS.clear()

        run(make_callback("rf:882", message_id=prompt.get("message_id"), thread_id=2))
    finally:
        _robot_fix_restore(original)

    markup = json.dumps(prompt.get("reply_markup") or {}, ensure_ascii=False)

    check(
        "fix: показаны кнопки с похожим номером и отказом",
        "rf:882" in markup and "rf:no" in markup,
        markup,
    )
    check("fix: бот ждёт ответа сотрудника", pending is not None, pending)
    check("fix: сохранён исправленный номер", state["saved"] == ["882"], state["saved"])
    check("fix: очередь не пополнялась", state["queued"] == [], state["queued"])
    flat = json.dumps(FORWARDED, ensure_ascii=False)

    check(
        "fix: в Lark ушла одна карточка с исправленным номером",
        len(FORWARDED) == 1 and "882" in flat and "3882" not in flat,
        FORWARDED,
    )


def test_robot_fix_declined_keeps_number():
    """Сотрудник подтвердил номер: очередь + Lark, как раньше."""
    LINKS[100] = "Ivan Petrenko"
    state, original = _robot_fix_env()

    try:
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3882",
            message_id=9301,
            thread_id=2,
        ))
        prompt_id = sent[0]["message_id"] if sent else None

        FORWARDED.clear()
        run(make_callback("rf:no", message_id=prompt_id, thread_id=2))
    finally:
        _robot_fix_restore(original)

    flat = json.dumps(FORWARDED, ensure_ascii=False)

    check(
        "fix: отказ -> номер ушёл в очередь",
        state["queued"] == ["3882"],
        state["queued"],
    )
    check("fix: отказ -> ошибка ушла в Lark", "3882" in flat, flat)
    check("fix: отказ -> записи в базу нет", state["saved"] == [], state["saved"])


def test_robot_fix_expiry_flushes_as_is():
    """Молчание сотрудника: по TTL ошибка уходит как есть, не теряется."""
    LINKS[100] = "Ivan Petrenko"
    state, original = _robot_fix_env()

    try:
        run(make_update(
            text="Unable to drive: Security module failure. 3882",
            message_id=9401,
            thread_id=2,
        ))

        with bot._pending_robot_fix_lock:
            for key in list(bot._pending_robot_fix):
                bot._pending_robot_fix[key]["expires"] = time.time() - 1

        FORWARDED.clear()
        flushed = bot.flush_expired_robot_fixes()
    finally:
        _robot_fix_restore(original)

    flat = json.dumps(FORWARDED, ensure_ascii=False)

    check("fix: просроченная подсказка дослана", flushed == 1, flushed)
    check(
        "fix: по TTL номер ушёл в очередь как есть",
        state["queued"] == ["3882"],
        state["queued"],
    )
    check("fix: по TTL ошибка ушла в Lark", "3882" in flat, flat)
    check(
        "fix: состояние очищено",
        bot.peek_pending_robot_fix(-500, 100) is None,
    )

# ============================================================
# СПРИНТ 2: ПРОСТОЙ В ОТЧЁТЕ И ДАЙДЖЕСТ ОБСЛУЖИВАНИЯ
# ============================================================

def _history_rows(events):
    """events: [(created_at, robot, new_status, type_problem)] -> строки журнала."""
    return [
        {
            "created_at": moment,
            "robot_number": robot,
            "new_status": status,
            "type_problem": type_problem,
        }
        for moment, robot, status, type_problem in events
    ]


def test_downtime_metrics_from_history():
    """MTTR считается склейкой Offline→Online из журнала смен статуса."""
    import shift_report as sr

    original_get = sr.rest_get

    # Дневная смена 23.09: 06:00–18:00 по Варшаве = 04:00–16:00 UTC.
    events = [
        # робот 1: 2 часа простоя, вернулся внутри смены
        ("2026-09-23T05:00:00+00:00", 1, sr.robot_status.OFFLINE, "Other"),
        ("2026-09-23T07:00:00+00:00", 1, sr.robot_status.ONLINE, "Solved without changing"),
        # робот 2: 4 часа простоя, вернулся внутри смены
        ("2026-09-23T08:00:00+00:00", 2, sr.robot_status.OFFLINE, "Abnormal walking"),
        ("2026-09-23T12:00:00+00:00", 2, sr.robot_status.ONLINE, "Solved without changing"),
        # робот 3: вернулся ВНЕ смены (ночью) — в MTTR смены не попадает
        ("2026-09-22T20:00:00+00:00", 3, sr.robot_status.OFFLINE, "Other"),
        ("2026-09-23T19:00:00+00:00", 3, sr.robot_status.ONLINE, "Other"),
        # робот 4: до сих пор в офлайне — интервал не закрыт
        ("2026-09-23T09:00:00+00:00", 4, sr.robot_status.OFFLINE, "Other"),
    ]

    sr.rest_get = lambda table, params=None: _history_rows(events)

    try:
        metrics = sr.downtime_metrics("2026-09-23", "day")
    finally:
        sr.rest_get = original_get

    check(
        "downtime: посчитаны только закрытые внутри смены интервалы",
        metrics["available"] and metrics["count"] == 2,
        metrics,
    )
    check(
        "downtime: MTTR = 3 часа ((2+4)/2)",
        metrics["mttr_seconds"] == 3 * 3600,
        metrics,
    )
    check(
        "downtime: самый долгий — робот 2 (4 часа, причина из Offline)",
        metrics["longest"]["robot"] == 2
        and metrics["longest"]["seconds"] == 4 * 3600
        and metrics["longest"]["type_problem"] == "Abnormal walking",
        metrics["longest"],
    )

    line = sr.downtime_line({"downtime": metrics})
    check(
        "downtime: строка отчёта человекочитаемая",
        line and "MTTR 3h 00m" in line and "#2" in line,
        line,
    )

    # нет закрытых интервалов
    sr.rest_get = lambda table, params=None: _history_rows([])

    try:
        empty = sr.downtime_metrics("2026-09-23", "day")
    finally:
        sr.rest_get = original_get

    check(
        "downtime: без ремонтов count=0",
        empty["available"] and empty["count"] == 0,
        empty,
    )
    check(
        "downtime: строка без ремонтов не врёт про MTTR",
        "no robot came back online" in (sr.downtime_line({"downtime": empty}) or ""),
        sr.downtime_line({"downtime": empty}),
    )

    # сбой БД
    sr.rest_get = lambda table, params=None: None

    try:
        broken = sr.downtime_metrics("2026-09-23", "day")
    finally:
        sr.rest_get = original_get

    check(
        "downtime: сбой чтения -> available=False (без выдуманного MTTR)",
        broken["available"] is False,
        broken,
    )
    check(
        "downtime: при сбое строки в отчёте нет",
        sr.downtime_line({"downtime": broken}) is None,
    )


def test_shift_window_bounds():
    """Границы смены: день 06–18, ночь 18–06 следующего дня (Варшава)."""
    import shift_report as sr

    day = sr.shift_window("2026-09-23", "day")
    night = sr.shift_window("2026-09-23", "night")

    check(
        "shift window: день — 04:00–16:00 UTC летом",
        day and day[0].hour == 4 and day[1].hour == 16,
        day,
    )
    check(
        "shift window: ночь — 16:00 UTC → 04:00 UTC следующего дня",
        night and night[0].hour == 16 and night[1].hour == 4 and night[1].day == 24,
        night,
    )
    check("shift window: мусорная дата -> None", sr.shift_window("23.09", "day") is None)


def test_digest_build_stale_and_queue():
    """Дайджест: залипшие в офлайне + открытые заявки."""
    import digests
    import robot_queue

    original_stale = digests.stale_offline_robots
    original_stats = robot_queue.queue_stats

    stale = [
        {"robot": 135, "hours": 2800.0, "days": 117, "problem": "其他/other",
         "updated_at": None},
        {"robot": 123, "hours": 601.0, "days": 25, "problem": "Photoelectric",
         "updated_at": None},
    ]

    # дайджест по одному складу — как в ежедневной рассылке
    text = None

    digests.stale_offline_robots = lambda hours=None, warehouse=None: stale
    robot_queue.queue_stats = lambda days=1, warehouse=None: {
        "open": 133,
        "new": 4,
        "rows": [
            {"robot_number": "3706", "created_at": "2026-09-23T01:24:21+00:00"},
            {"robot_number": "3498", "created_at": "2026-09-23T00:43:00+00:00"},
        ],
    }

    try:
        text = digests.build_digest(warehouse="GLP-C")
    finally:
        digests.stale_offline_robots = original_stale
        robot_queue.queue_stats = original_stats

    check(
        "digest: офлайн-роботы с простоем и причиной",
        "#135 · 117d · 其他/other" in text and "#123 · 25d" in text,
        text,
    )
    check(
        "digest: очередь заявок с числом и новыми за сутки",
        "Open robot-add requests: 133" in text and "new in 24h: 4" in text,
        text,
    )
    check(
        "digest: последние заявки перечислены",
        "#3706" in text and "#3498" in text,
        text,
    )

    # нечего сообщать — не шлём
    digests.stale_offline_robots = lambda hours=None, warehouse=None: []

    try:
        robot_queue.queue_stats = lambda days=1, warehouse=None: {"open": 0, "new": 0, "rows": []}
        empty = digests.build_digest(warehouse="GLP-C")
    finally:
        digests.stale_offline_robots = original_stale
        robot_queue.queue_stats = original_stats

    check("digest: пустой дайджест не отправляется", empty is None, empty)

    # сбой базы — тоже молчим, но не врём «всё хорошо»
    digests.stale_offline_robots = lambda hours=None, warehouse=None: None

    try:
        broken = digests.build_digest(warehouse="GLP-C")
    finally:
        digests.stale_offline_robots = original_stale

    check("digest: сбой чтения -> не отправляем", broken is None, broken)


def test_digest_send_and_marker():
    """Рассылка: получатели, маркер дня, отсутствие получателей."""
    import digests

    original = (
        digests._sender,
        digests.build_digest,
        digests.was_sent,
        digests.mark_sent,
        digests.recipients,
    )

    sent = []

    digests._sender = lambda chat_id, text: sent.append((chat_id, text)) or True
    digests.build_digest = lambda now=None, warehouse=None: "digest text"
    digests.was_sent = lambda now: False
    digests.mark_sent = lambda now: True
    digests.recipients = lambda: [111, 222]

    try:
        result = digests.send_digest()
    finally:
        (
            digests._sender,
            digests.build_digest,
            digests.was_sent,
            digests.mark_sent,
            digests.recipients,
        ) = original

    check(
        "digest: ушёл всем получателям",
        result["sent"] == 2 and [chat for chat, _ in sent] == [111, 222],
        (result, sent),
    )

    # уже отправляли сегодня — второй раз не шлём
    digests.build_digest = lambda now=None, warehouse=None: "digest text"
    digests.was_sent = lambda now: True
    digests.recipients = lambda: [111]
    digests._sender = lambda chat_id, text: sent.append((chat_id, text)) or True

    try:
        sent.clear()
        again = digests.send_digest()
    finally:
        (
            digests._sender,
            digests.build_digest,
            digests.was_sent,
            digests.mark_sent,
            digests.recipients,
        ) = original

    check(
        "digest: повторная отправка в тот же день заблокирована",
        again["reason"] == "already sent" and sent == [],
        (again, sent),
    )

    # нет получателей
    digests._sender = lambda chat_id, text: True
    digests.build_digest = lambda now=None, warehouse=None: "digest text"
    digests.was_sent = lambda now: False
    digests.recipients = lambda: []

    try:
        nobody = digests.send_digest()
    finally:
        (
            digests._sender,
            digests.build_digest,
            digests.was_sent,
            digests.mark_sent,
            digests.recipients,
        ) = original

    check(
        "digest: без получателей — не падаем и не помечаем день",
        nobody["reason"] == "no recipients",
        nobody,
    )


def test_digest_due_and_command():
    """Расписание дайджеста и команда /digest."""
    import digests

    original_was_sent = digests.was_sent
    original_last = digests._last_sent_date
    original_build = digests.build_digest

    from datetime import datetime
    from zoneinfo import ZoneInfo

    warsaw = ZoneInfo("Europe/Warsaw")

    digests.was_sent = lambda now: False
    digests._last_sent_date = None

    try:
        early = digests.digest_due(datetime(2026, 9, 24, digests.DIGEST_HOUR - 1, tzinfo=warsaw))
        ready = digests.digest_due(datetime(2026, 9, 24, digests.DIGEST_HOUR, tzinfo=warsaw))

        digests._last_sent_date = "2026-09-24"
        same_day = digests.digest_due(datetime(2026, 9, 24, digests.DIGEST_HOUR + 2, tzinfo=warsaw))

        digests._last_sent_date = None
        digests.was_sent = lambda now: True
        sent_before_restart = digests.digest_due(
            datetime(2026, 9, 24, digests.DIGEST_HOUR, tzinfo=warsaw)
        )
    finally:
        digests.was_sent = original_was_sent
        digests._last_sent_date = original_last

    check("digest: до часа отправки не шлём", early is False)
    check("digest: после часа отправки шлём", ready is True)
    check("digest: второй раз в тот же день не шлём", same_day is False)
    check(
        "digest: маркер в Storage отменяет отправку после перезапуска",
        sent_before_restart is False,
    )

    original_build = digests.build_digest
    digests.build_digest = lambda now=None, warehouse=None: "🗂 Maintenance digest · 24.09.2026"

    try:
        sent = run(make_update(text="/digest", message_id=9601, thread_id=2))
    finally:
        digests.build_digest = original_build

    check(
        "digest: команда /digest присылает сводку",
        sent and "Maintenance digest" in sent[0]["text"],
        sent,
    )

    digests.build_digest = lambda now=None, warehouse=None: None

    try:
        sent = run(make_update(text="/digest", message_id=9602, thread_id=2))
    finally:
        digests.build_digest = original_build

    check(
        "digest: нечего сообщать — честный ответ, а не пустое сообщение",
        sent and "Nothing to report" in sent[0]["text"],
        sent,
    )

# ============================================================
# СПРИНТ 3: АНАЛИТИКА (/top, /downtime, /week)
# ============================================================

def test_analytics_top_report():
    """Топ типов проблем и роботов за период."""
    import analytics

    original = analytics.rest_get_all

    rows = [
        {"error_robot": 3750, "issue_type": "Unable to drive"},
        {"error_robot": 3750, "issue_type": "Unable to drive"},
        {"error_robot": 3750, "issue_type": "Obstacle avoidance"},
        {"error_robot": 3421, "issue_type": "Unable to drive"},
        {"error_robot": None, "issue_type": None},
    ]

    analytics.rest_get_all = lambda table, params=None, page=1000, max_pages=10: rows

    try:
        week = analytics.top_report("week")
        month = analytics.top_report("month")
    finally:
        analytics.rest_get_all = original

    check(
        "top: считает типы и роботов",
        week["total"] == 5
        and week["issues"][0] == ("Unable to drive", 3)
        and week["robots"][0] == ("3750", 3),
        week,
    )
    check(
        "top: пустой тип не теряется",
        ("unknown", 1) in week["issues"],
        week["issues"],
    )
    check("top: период влияет на окно", month["days"] == 30, month["days"])

    text = analytics.format_top_report(week)
    check(
        "top: текст с итогом и топами",
        "Top for the last 7 day(s)" in text
        and "Total: 5" in text
        and "Unable to drive" in text
        and "#3750" in text,
        text,
    )
    check(
        "top: пустой период подписан честно",
        "No exceptions in this period." in analytics.format_top_report(
            {"period": "day", "days": 1, "since": None, "total": 0,
             "issues": [], "robots": []}
        ),
    )
    check(
        "top: неизвестный период -> подсказка",
        "Usage: /top" in analytics.format_top_report(None),
    )
    check("top: неизвестный период не считается", analytics.top_report("year") is None)

    analytics.rest_get_all = lambda table, params=None, page=1000, max_pages=10: None

    try:
        broken = analytics.top_report("week")
    finally:
        analytics.rest_get_all = original

    check("top: сбой чтения -> None, а не пустой отчёт", broken is None, broken)


def test_analytics_downtime_report():
    """Простой: MTTR по ремонтам периода + отдельно «старые» заявки."""
    import analytics

    from datetime import datetime, timedelta, timezone

    original = analytics.downtime_intervals
    now = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)

    intervals = [
        # починен в периоде, начался в периоде: 2 часа
        {"robot": 1, "start": now - timedelta(days=2),
         "end": now - timedelta(days=2) + timedelta(hours=2), "seconds": 7200.0,
         "type_problem": "Other"},
        # починен в периоде, начался в периоде: 6 часов
        {"robot": 2, "start": now - timedelta(days=3),
         "end": now - timedelta(days=3) + timedelta(hours=6), "seconds": 21600.0,
         "type_problem": "Other"},
        # закрыт в периоде, но открыт 40 дней назад (старая заявка)
        {"robot": 3, "start": now - timedelta(days=40),
         "end": now - timedelta(days=1), "seconds": 40 * 86400.0,
         "type_problem": "Other"},
        # закрылся до периода — не считается
        {"robot": 4, "start": now - timedelta(days=30),
         "end": now - timedelta(days=20), "seconds": 10 * 86400.0,
         "type_problem": "Other"},
    ]

    analytics.downtime_intervals = lambda warehouse=None: intervals

    try:
        report = analytics.downtime_report(7, now)
    finally:
        analytics.downtime_intervals = original

    check(
        "downtime: MTTR только по ремонтам, начатым в периоде",
        report["repairs"] == 2 and report["mttr_seconds"] == 4 * 3600,
        report,
    )
    check(
        "downtime: старая заявка считается отдельно",
        report["legacy"] == 1 and report["legacy_longest"]["robot"] == 3,
        report,
    )
    check(
        "downtime: топ не раздувается старой заявкой",
        [robot for robot, _ in report["top"]] == [2, 1],
        report["top"],
    )

    text = analytics.format_downtime_report(report, 7)
    check(
        "downtime: текст с MTTR, старой заявкой и топом",
        "Repairs started: 2" in text
        and "MTTR 4h 00m" in text
        and "Also closed: 1 older repair(s)" in text
        and "#2" in text,
        text,
    )

    analytics.downtime_intervals = lambda warehouse=None: None

    try:
        broken = analytics.downtime_report(7, now)
    finally:
        analytics.downtime_intervals = original

    check("downtime: сбой чтения -> None", broken is None, broken)
    check(
        "downtime: подсказка по формату при отсутствии данных",
        "Usage: /downtime" in analytics.format_downtime_report(None),
    )


def test_analytics_weekly_and_schedule():
    """Недельный отчёт: текст, отправка, маркер и расписание."""
    import analytics

    from datetime import datetime
    from zoneinfo import ZoneInfo

    warsaw = ZoneInfo("Europe/Warsaw")
    monday = datetime(2026, 9, 28, 9, 0, tzinfo=warsaw)

    check("weekly: тест опирается на понедельник", monday.weekday() == 0)

    original = (
        analytics.top_report,
        analytics.downtime_report,
        analytics.retirement_candidates,
        analytics.send_text_via_hook,
        analytics.was_sent,
        analytics.mark_sent,
        analytics.WEEKLY_REPORT_ENABLED,
    )

    analytics.top_report = lambda period="week", now=None, warehouse=None: {
        "period": "week", "days": 7, "since": None, "total": 464,
        "issues": [("Unable to drive", 311)], "robots": [("3750", 13)],
    }
    analytics.downtime_report = lambda days=7, now=None, warehouse=None: {
        "days": days, "repairs": 18, "mttr_seconds": 85200,
        "top": [(3432, {"seconds": 306300.0, "count": 1})], "legacy": 27,
        "legacy_longest": {"robot": 132, "seconds": 7815000.0},
    }
    analytics.retirement_candidates = lambda days=30, min_offlines=None, now=None, warehouse=None: [
        (97, 3, 262402.0),
    ]

    try:
        text = analytics.weekly_text(now=monday)
    finally:
        (
            analytics.top_report,
            analytics.downtime_report,
            analytics.retirement_candidates,
            analytics.send_text_via_hook,
            analytics.was_sent,
            analytics.mark_sent,
            analytics.WEEKLY_REPORT_ENABLED,
        ) = original

    check(
        "weekly: есть итоги, простой и кандидаты",
        "Weekly report" in text
        and "Exceptions: 464" in text
        and "Repairs started: 18" in text
        and "also closed: 27" in text
        and "#97" in text,
        text,
    )

    # отправка + маркер
    hooks, marked = [], []

    analytics.top_report = lambda period="week", now=None, warehouse=None: {
        "period": "week", "days": 7, "since": None, "total": 1,
        "issues": [], "robots": [],
    }
    analytics.downtime_report = lambda days=7, now=None, warehouse=None: None
    analytics.retirement_candidates = lambda days=30, min_offlines=None, now=None, warehouse=None: []
    analytics.send_text_via_hook = lambda url, text: hooks.append(text) or {"code": 0}
    analytics.was_sent = lambda now: False
    analytics.mark_sent = lambda now: marked.append(now.strftime("%Y-%m-%d")) or True

    try:
        result = analytics.send_weekly_report(monday)
    finally:
        (
            analytics.top_report,
            analytics.downtime_report,
            analytics.retirement_candidates,
            analytics.send_text_via_hook,
            analytics.was_sent,
            analytics.mark_sent,
            analytics.WEEKLY_REPORT_ENABLED,
        ) = original

    check(
        "weekly: отчёт ушёл в Lark и день помечен",
        result["sent"] is True and len(hooks) == 1 and marked == ["2026-09-28"],
        (result, marked),
    )

    # флаг выключен -> никогда не пора
    analytics.WEEKLY_REPORT_ENABLED = False
    analytics.was_sent = lambda now: False

    try:
        off = analytics.weekly_due(monday)
    finally:
        analytics.WEEKLY_REPORT_ENABLED = original[6]
        analytics.was_sent = original[4]

    check("weekly: по умолчанию выключен", off is False)

    # включён, но не время
    analytics.WEEKLY_REPORT_ENABLED = True
    analytics.was_sent = lambda now: False
    analytics._last_sent_monday = None

    try:
        tuesday = monday.replace(day=29)
        early = analytics.weekly_due(monday.replace(hour=analytics.WEEKLY_REPORT_HOUR - 1))
        not_monday = analytics.weekly_due(tuesday)
        ready = analytics.weekly_due(monday)

        analytics._last_sent_monday = "2026-09-28"
        repeated = analytics.weekly_due(monday)

        analytics._last_sent_monday = None
        analytics.was_sent = lambda now: True
        after_restart = analytics.weekly_due(monday)
    finally:
        analytics.WEEKLY_REPORT_ENABLED = original[6]
        analytics.was_sent = original[4]
        analytics._last_sent_monday = None

    check("weekly: до часа отправки не шлём", early is False)
    check("weekly: не понедельник — не шлём", not_monday is False)
    check("weekly: в понедельник после часа — шлём", ready is True)
    check("weekly: второй раз в тот же день не шлём", repeated is False)
    check("weekly: маркер отменяет повтор после перезапуска", after_restart is False)


def test_analytics_commands():
    """/top, /downtime, /week в Telegram."""
    import analytics

    original_top = analytics.top_report
    original_downtime = analytics.downtime_report
    original_week = analytics.weekly_text

    analytics.top_report = lambda period="week", now=None, warehouse=None: {
        "period": period, "days": analytics.period_days(period), "since": None,
        "total": 3, "issues": [("Unable to drive", 3)], "robots": [("3750", 2)],
    }
    analytics.downtime_report = lambda days=7, now=None, warehouse=None: {
        "days": days, "repairs": 1, "mttr_seconds": 3600,
        "top": [(1, {"seconds": 3600.0, "count": 1})], "legacy": 0,
        "legacy_longest": None,
    }
    analytics.weekly_text = lambda days=7, now=None, warehouse=None: "📊 Weekly report · GLP-C"

    try:
        week = run(make_update(text="/top", message_id=9701, thread_id=2))
        month = run(make_update(text="/top month", message_id=9702, thread_id=2))
        bad = run(make_update(text="/top year", message_id=9703, thread_id=2))
        downtime = run(make_update(text="/downtime 7", message_id=9704, thread_id=2))
        bad_days = run(make_update(text="/downtime abc", message_id=9705, thread_id=2))
        weekly = run(make_update(text="/week", message_id=9706, thread_id=2))
    finally:
        analytics.top_report = original_top
        analytics.downtime_report = original_downtime
        analytics.weekly_text = original_week

    check(
        "analytics: /top по умолчанию неделя",
        week and "Top for the last 7 day(s)" in week[0]["text"],
        week,
    )
    check(
        "analytics: /top month переключает период",
        month and "Top for the last 30 day(s)" in month[0]["text"],
        month,
    )
    check(
        "analytics: /top с мусором — подсказка",
        bad and "Usage: /top" in bad[0]["text"],
        bad,
    )
    check(
        "analytics: /downtime показывает MTTR",
        downtime and "MTTR 1h 00m" in downtime[0]["text"],
        downtime,
    )
    check(
        "analytics: /downtime с мусором — подсказка",
        bad_days and "Usage: /downtime" in bad_days[0]["text"],
        bad_days,
    )
    check(
        "analytics: /week присылает сводку",
        weekly and "Weekly report" in weekly[0]["text"],
        weekly,
    )

    analytics.weekly_text = lambda days=7, now=None, warehouse=None: ""

    try:
        empty = run(make_update(text="/week", message_id=9707, thread_id=2))
    finally:
        analytics.weekly_text = original_week

    check(
        "analytics: /week без данных отвечает честно",
        empty and "No data" in empty[0]["text"],
        empty,
    )

# ============================================================
# ТОПИКИ: РАЗДЕЛЕНИЕ ПО СМЫСЛУ
# ============================================================

def test_topic_map_and_resolution():
    """Как бот понимает, какой топик за что отвечает."""
    original = (
        dict(bot.TOPIC_RAW),
        bot.TELEGRAM_TOPIC_ID,
        bot.TELEGRAM_TOPIC_NAME,
    )
    original_names = dict(bot._topic_names)

    try:
        bot.TOPIC_RAW = {"error": "2", "status": "14", "stats": "15", "service": "16"}
        bot.TELEGRAM_TOPIC_ID = 2
        bot.TELEGRAM_TOPIC_NAME = ""

        check(
            "topics: id из конфига",
            bot.topic_thread("error") == 2
            and bot.topic_thread("status") == 14
            and bot.topic_thread("stats") == 15
            and bot.topic_thread("service") == 16,
            bot.topic_map(),
        )

        bot._topic_names[(-500, 21)] = "Robot status"
        bot.TOPIC_RAW = {**bot.TOPIC_RAW, "status": "Robot status"}

        check(
            "topics: имя ищется среди выученных",
            bot.topic_thread("status", -500) == 21,
            bot.topic_thread("status", -500),
        )

        bot.TOPIC_RAW = {**bot.TOPIC_RAW, "status": "No such topic"}

        check(
            "topics: неизвестное имя -> None (ответим как раньше)",
            bot.topic_thread("status", -500) is None,
        )

        bot.TOPIC_RAW = {"error": "", "status": "", "stats": "", "service": ""}
        bot.TELEGRAM_TOPIC_ID = None

        check(
            "topics: ничего не настроено -> None",
            all(value is None for value in bot.topic_map().values()),
            bot.topic_map(),
        )

        check("topics: неизвестный вид -> None", bot.topic_thread("nonsense") is None)
    finally:
        (
            bot.TOPIC_RAW,
            bot.TELEGRAM_TOPIC_ID,
            bot.TELEGRAM_TOPIC_NAME,
        ) = original
        bot._topic_names.clear()
        bot._topic_names.update(original_names)


def test_answers_stay_in_origin_topic():
    """Бот отвечает только в том топике, откуда пришло сообщение."""
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3783"] = _robot()

    original = (
        dict(bot.TOPIC_RAW),
        bot.TELEGRAM_TOPIC_ID,
        bot.DELETE_USER_MESSAGES,
        bot.shift_metrics,
    )
    original_names = dict(bot._topic_names)

    bot.TOPIC_RAW = {
        "error": "2",
        "error:SMALL-P3": "318",
        "status": "319",
        "stats": "320",
        "service": "321",
    }
    bot.TELEGRAM_TOPIC_ID = 2
    bot.DELETE_USER_MESSAGES = True
    bot.shift_metrics = lambda shift_date, shift_name, warehouse=None: None

    def only_topics(sent, expected):
        return sent and all(item["thread_id"] == expected for item in sent)

    try:
        # команда из топика ошибок — ответ там же
        sent = run(make_update(text="/stats", message_id=9801, thread_id=2))

        check(
            "origin: /stats из топика ошибок отвечает там же",
            only_topics(sent, 2),
            [item["thread_id"] for item in sent],
        )
        check(
            "origin: подсказок про другой топик нет",
            not any("The answer is in the" in item["text"] for item in sent),
            sent,
        )

        # команда из топика статистики — там же
        sent = run(make_update(text="/stats", message_id=9802, thread_id=320))

        check(
            "origin: /stats из топика статистики отвечает там же",
            only_topics(sent, 320),
            [item["thread_id"] for item in sent],
        )

        # команда из служебного — там же
        sent = run(make_update(text="/help", message_id=9803, thread_id=321))

        check(
            "origin: /help отвечает в служебном топике",
            only_topics(sent, 321),
            [item["thread_id"] for item in sent],
        )

        # /offline из топика статусов — весь флоу там же
        sent = run(make_update(text="/offline 3783", message_id=9804, thread_id=319))
        prompt = [item for item in sent if item.get("reply_markup")]

        check(
            "origin: кнопки причин в топике запроса",
            prompt and prompt[0]["thread_id"] == 319,
            [(item["thread_id"], bool(item.get("reply_markup"))) for item in sent],
        )
        check(
            "origin: ни одного сообщения в другие топики",
            only_topics(sent, 319),
            [item["thread_id"] for item in sent],
        )

        # ошибка из топика ошибок — там же
        sent = run(make_update(
            text="Unable to drive: Security module failure. 3780",
            message_id=9805,
            thread_id=2,
        ))

        check(
            "origin: ошибка обрабатывается в своём топике",
            only_topics(sent, 2),
            [item["thread_id"] for item in sent],
        )

        # сообщение из топика ошибок SP3 — ответ там же
        sent = run(make_update(
            text="Unable to drive: Security module failure. 5016",
            message_id=9806,
            thread_id=318,
        ))

        check(
            "origin: топик ошибок SP3 отвечает у себя",
            only_topics(sent, 318),
            [item["thread_id"] for item in sent],
        )
    finally:
        (
            bot.TOPIC_RAW,
            bot.TELEGRAM_TOPIC_ID,
            bot.DELETE_USER_MESSAGES,
            bot.shift_metrics,
        ) = original
        bot._topic_names.clear()
        bot._topic_names.update(original_names)
        ROBOTS.clear()


def test_topics_command():
    """/topics показывает карту топиков."""
    original = (dict(bot.TOPIC_RAW), bot.TELEGRAM_TOPIC_ID)
    original_names = dict(bot._topic_names)

    bot.TOPIC_RAW = {"error": "2", "status": "", "stats": "15", "service": ""}
    bot.TELEGRAM_TOPIC_ID = 2
    bot._topic_names[(-500, 2)] = "Ex GLPC"
    original_seen = dict(bot._topic_seen)
    bot._topic_seen.clear()

    try:
        # бот «видел» сообщения в двух топиках: ошибки и статистика
        run(make_update(text="/help", message_id=9901, thread_id=2))
        sent = run(make_update(text="/topics", message_id=9902, thread_id=15))
    finally:
        (bot.TOPIC_RAW, bot.TELEGRAM_TOPIC_ID) = original
        bot._topic_names.clear()
        bot._topic_names.update(original_names)
        bot._topic_seen.clear()
        bot._topic_seen.update(original_seen)

    text = sent[0]["text"] if sent else ""

    check(
        "topics: команда показывает id и имя",
        "GLP-C: id 2" in text and "'Ex GLPC'" in text,
        text,
    )
    check(
        "topics: видно, что не настроено",
        "Robot status: NOT configured" in text,
        text,
    )
    check(
        "topics: видно, где читаются ошибки",
        "Errors are read from these topics:" in text,
        text,
    )
    check(
        "topics: сказано, что ответы уходят в топик-источник",
        "Answers always go to the topic the message came from." in text,
        text,
    )
    check(
        "topics: перечислены топики, где бот видел сообщения",
        "Topics the bot has seen messages in:" in text
        and "id 2" in text
        and "id 15" in text,
        text,
    )

# ============================================================
# ДВА СКЛАДА: GLP-C и SMALL-P3
# ============================================================

def test_warehouses_config_and_args():
    """Склады бота и разбор склада в аргументах команды."""
    from warehouses import WAREHOUSES, warehouse_from_args, warehouse_key

    check(
        "warehouses: по умолчанию два склада",
        WAREHOUSES.get("glpc") == "GLP-C" and WAREHOUSES.get("sp3") == "SMALL-P3",
        WAREHOUSES,
    )
    check(
        "warehouses: /stats sp3 day",
        warehouse_from_args("sp3 day") == ("SMALL-P3", "day"),
        warehouse_from_args("sp3 day"),
    )
    check(
        "warehouses: /stats GLP-C 2026-09-23 day",
        warehouse_from_args("GLP-C 2026-09-23 day")
        == ("GLP-C", "2026-09-23 day"),
        warehouse_from_args("GLP-C 2026-09-23 day"),
    )
    check(
        "warehouses: без склада аргументы не трогаем",
        warehouse_from_args("2026-09-23 day") == (None, "2026-09-23 day"),
        warehouse_from_args("2026-09-23 day"),
    )
    check("warehouses: ключ по названию", warehouse_key("SMALL-P3") == "sp3")


def test_two_warehouses_strict_lookup():
    """Ошибки пишутся складом топика, робот ищется ТОЛЬКО на своём складе."""
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()

    original = (
        dict(bot.TOPIC_RAW),
        bot.TELEGRAM_TOPIC_ID,
    )
    original_names = dict(bot._topic_names)
    original_find = robot_status.find_robot
    original_find_by_id = robot_status.find_robot_by_id
    original_change = robot_status.change_robot_status
    original_card = bot.send_card_via_hook

    bot.TOPIC_RAW = {
        "error": "2",
        "error:SMALL-P3": "318",
        "status": "319",
        "stats": "320",
        "service": "321",
    }
    bot.TELEGRAM_TOPIC_ID = 2
    bot._topic_names.clear()

    # #123 есть на ДВУХ складах — бот обязан брать робота своего склада.
    robots_by_warehouse = {
        "GLP-C": {
            "123": {**_robot(123, 101), "warehouse": "GLP-C"},
            "3780": _robot(3780, 102),
        },
        "SMALL-P3": {
            "123": {**_robot(123, 201), "warehouse": "SMALL-P3"},
            "5016": {**_robot(5016, 202), "warehouse": "SMALL-P3"},
        },
    }

    def fake_find(number, warehouse=None, strict=False):
        return robots_by_warehouse.get(warehouse, {}).get(
            str(number).strip().lstrip("#")
        )

    changed, cards = [], []

    def fake_change(robot, *args, **kwargs):
        changed.append(robot.get("warehouse"))

        return {
            "robot": robot,
            "old_status": robot_status.ONLINE,
            "new_status": robot_status.OFFLINE,
            "type_problem": "Other",
            "problem_note": "test",
            "history_saved": True,
        }

    def fake_find_by_id(robot_id, strict=False):
        for rows in robots_by_warehouse.values():
            for robot in rows.values():
                if str(robot.get("id")) == str(robot_id):
                    return robot

        return None

    robot_status.find_robot = fake_find
    robot_status.find_robot_by_id = fake_find_by_id
    robot_status.change_robot_status = fake_change
    bot.send_card_via_hook = lambda url, card: (cards.append(url), {"code": 0})[1]

    try:
        # 1) ошибка из топика SP3 пишется складом SMALL-P3
        run(make_update(
            text="Unable to drive: Security module failure. 5016",
            message_id=10001,
            thread_id=318,
        ))

        check(
            "warehouse: ошибка из топика SP3 пишется складом SMALL-P3",
            DB_CALLS and DB_CALLS[-1]["warehouse"] == "SMALL-P3",
            DB_CALLS[-1] if DB_CALLS else None,
        )

        # 2) ошибка из топика GLP-C — складом GLP-C
        run(make_update(
            text="Unable to drive: Security module failure. 3780",
            message_id=10002,
            thread_id=2,
        ))

        check(
            "warehouse: ошибка из топика GLP-C пишется складом GLP-C",
            DB_CALLS and DB_CALLS[-1]["warehouse"] == "GLP-C",
            DB_CALLS[-1] if DB_CALLS else None,
        )

        # 3) робот есть только на SP3 — находится и работает без лишних вопросов
        sent = run(make_update(text="/offline 5016", message_id=10003, thread_id=319))
        prompt = [item for item in sent if item.get("reply_markup")]
        markup = json.dumps(prompt[0]["reply_markup"], ensure_ascii=False) if prompt else ""

        check(
            "warehouse: единственный склад — сразу кнопки причин",
            prompt and "5016" in prompt[0]["text"] and "SMALL-P3" in prompt[0]["text"],
            prompt,
        )
        check(
            "warehouse: выбора склада нет, когда он один",
            "w:offline" not in markup,
            markup[:120],
        )

        # 4) явный склад работает как раньше
        sent = run(make_update(text="/offline sp3 5016", message_id=10004, thread_id=319))
        prompt = [item for item in sent if item.get("reply_markup")]

        check(
            "warehouse: /offline sp3 находит робота SP3",
            prompt and "5016" in prompt[0]["text"] and "SMALL-P3" in prompt[0]["text"],
            prompt,
        )

        # 5) номер есть на двух складах — бот спрашивает кнопками
        sent = run(make_update(text="/offline 123", message_id=10005, thread_id=319))
        ask = [item for item in sent if item.get("reply_markup")]

        check(
            "warehouse: дубль номера — вопрос с кнопками",
            ask and "2 warehouses" in ask[0]["text"],
            sent,
        )
        markup = json.dumps(ask[0]["reply_markup"], ensure_ascii=False) if ask else ""

        check(
            "warehouse: в кнопках оба склада",
            "w:offline:glpc:123" in markup and "w:offline:sp3:123" in markup,
            markup,
        )

        # выбор GLP-C -> кнопки причин для робота GLP-C
        SENT.clear()
        run(make_callback("w:offline:glpc:123", message_id=1, thread_id=319))
        chosen = [item for item in SENT if item.get("reply_markup")]

        check(
            "warehouse: после выбора GLP-C показаны причины",
            chosen and "GLP-C" in chosen[0]["text"]
            and "st:offline:101:" in json.dumps(chosen[0]["reply_markup"]),
            chosen,
        )

        # выбор SP3 -> робот SP3
        SENT.clear()
        run(make_callback("w:offline:sp3:123", message_id=1, thread_id=319))
        chosen = [item for item in SENT if item.get("reply_markup")]

        check(
            "warehouse: после выбора SMALL-P3 показаны причины",
            chosen and "SMALL-P3" in chosen[0]["text"]
            and "st:offline:201:" in json.dumps(chosen[0]["reply_markup"]),
            chosen,
        )

        # 6) робота нет нигде — подсказка про другой склад
        sent = run(make_update(text="/offline 99999", message_id=10009, thread_id=319))

        check(
            "warehouse: ненайденный робот перечисляет склады",
            sent and "not found in GLP-C or SMALL-P3" in sent[0]["text"],
            sent,
        )

        # 6) флоу помнит склад: причина приходит в общий топик (где склад GLP-C)
        sent = run(make_update(text="/offline sp3 5016", message_id=10007, thread_id=319))
        prompt_id = sent[0]["message_id"] if sent else None

        run(make_callback("st:offline:202:other", message_id=prompt_id, thread_id=319))
        pending = bot.peek_pending_status(-500, 100)

        check(
            "warehouse: склад робота сохранён во флоу",
            pending and pending.get("warehouse") == "SMALL-P3",
            pending,
        )

        changed.clear()
        cards.clear()
        run(make_update(text="сломался ролик", message_id=10008, thread_id=319))

        check(
            "warehouse: статус меняется у робота своего склада",
            changed == ["SMALL-P3"],
            changed,
        )
        check(
            "warehouse: карточка статуса ушла в вебхук SP3",
            cards and cards[-1] == os.environ.get(
                "LARK_HOOK_STATUS_SP3", cards[-1]
            ) if os.environ.get("LARK_HOOK_STATUS_SP3") else bool(cards),
            cards,
        )
    finally:
        (bot.TOPIC_RAW, bot.TELEGRAM_TOPIC_ID) = original
        robot_status.find_robot = original_find
        robot_status.find_robot_by_id = original_find_by_id
        robot_status.change_robot_status = original_change
        bot.send_card_via_hook = original_card
        bot._topic_names.clear()
        bot._topic_names.update(original_names)
        ROBOTS.clear()


def test_warehouse_argument_in_commands():
    """/stats и /top принимают склад аргументом, иначе берут склад топика."""
    import analytics

    original_metrics = bot.shift_metrics
    original_top = analytics.top_report

    captured = []

    def fake_metrics(shift_date, shift_name, warehouse=None):
        captured.append(warehouse)

        return {
            "total": 0,
            "robots": {},
            "employees": {},
            "downtime_minutes": 0,
            "previous": {"date": None, "shift": None, "total": None},
            "delta": None,
            "maintenance": [],
            "types": {},
            "warehouse": warehouse,
        }

    top_args = []

    def fake_top(period="week", now=None, warehouse=None):
        top_args.append((period, warehouse))

        return {
            "period": period,
            "days": analytics.period_days(period),
            "since": None,
            "warehouse": warehouse,
            "total": 0,
            "issues": [],
            "robots": [],
        }

    bot.shift_metrics = fake_metrics
    analytics.top_report = fake_top

    try:
        run(make_update(text="/stats sp3", message_id=10101, thread_id=320))
        run(make_update(text="/stats", message_id=10102, thread_id=320))
        run(make_update(text="/top sp3 day", message_id=10103, thread_id=320))
        run(make_update(text="/top", message_id=10104, thread_id=320))
    finally:
        bot.shift_metrics = original_metrics
        analytics.top_report = original_top

    check(
        "warehouse: /stats sp3 считает SP3, /stats — склад топика",
        captured == ["SMALL-P3", "GLP-C"],
        captured,
    )
    check(
        "warehouse: /top sp3 day и /top по умолчанию",
        top_args == [("day", "SMALL-P3"), ("week", "GLP-C")],
        top_args,
    )


def test_topic_name_learned_from_service_message():
    """Имя топика учим даже из сервисного сообщения, отправленного ботом."""
    original_names = dict(bot._topic_names)

    try:
        update = make_update(text=None, message_id=10201, thread_id=319)
        update["message"]["forum_topic_created"] = {"name": "Robot status"}
        update["message"]["from"] = {"id": 8732612039, "is_bot": True}

        run(update)

        check(
            "topics: имя выучено из сервисного сообщения",
            bot.topic_name(-500, 319) == "Robot status",
            bot.topic_name(-500, 319),
        )
        check(
            "topics: сервисное сообщение не уходит в обработку как текст",
            SENT == [],
            SENT,
        )
    finally:
        bot._topic_names.clear()
        bot._topic_names.update(original_names)

# ============================================================
# ВЕБХУКИ LARK ПО СКЛАДАМ И ВИДАМ СООБЩЕНИЙ
# ============================================================

def test_lark_hooks_resolve():
    """Ошибки и статусы уходят в свои вебхуки, у каждого склада — свой."""
    import lark_hooks

    names = (
        "LARK_HOOK_ERROR_SP3",
        "LARK_HOOK_STATUS_SP3",
        "LARK_HOOK_STATUS_GLPC",
    )
    original = {name: os.environ.get(name) for name in names}

    os.environ["LARK_HOOK_ERROR_SP3"] = "https://hook/sp3-errors"
    os.environ["LARK_HOOK_STATUS_SP3"] = "https://hook/sp3-status"
    os.environ["LARK_HOOK_STATUS_GLPC"] = "https://hook/glpc-status"

    try:
        check(
            "hooks: ошибки SP3 — свой вебхук",
            lark_hooks.error_hook("SMALL-P3") == "https://hook/sp3-errors",
            lark_hooks.error_hook("SMALL-P3"),
        )
        check(
            "hooks: статусы SP3 — свой вебхук",
            lark_hooks.status_hook("SMALL-P3") == "https://hook/sp3-status",
            lark_hooks.status_hook("SMALL-P3"),
        )
        check(
            "hooks: статусы GLP-C — свой вебхук",
            lark_hooks.status_hook("GLP-C") == "https://hook/glpc-status",
            lark_hooks.status_hook("GLP-C"),
        )
        check(
            "hooks: ошибки GLP-C — общий вебхук (переменной нет)",
            lark_hooks.error_hook("GLP-C") == lark_hooks.TARGET_HOOK_URL,
            lark_hooks.error_hook("GLP-C"),
        )
        check(
            "hooks: неизвестный склад — общий вебхук",
            lark_hooks.error_hook("NO-SUCH") == lark_hooks.TARGET_HOOK_URL,
        )
        check(
            "hooks: карта вебхуков по складам и видам",
            set(lark_hooks.hook_map()) == {"error", "status"}
            and set(lark_hooks.hook_map()["status"]) == {"GLP-C", "SMALL-P3"},
            lark_hooks.hook_map(),
        )
        check(
            "hooks: в лог печатается только хвост ссылки",
            lark_hooks.short("https://open.larksuite.com/open-apis/bot/v2/hook/abcdef12-3456")
            == "abcdef12",
            lark_hooks.short("https://x/hook/abcdef12-3456"),
        )
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_error_and_status_hooks_by_warehouse():
    """Ошибка SP3 и смена статуса SP3 уходят каждый в свой вебхук."""
    import lark_hooks
    import pending_photos

    names = ("LARK_HOOK_ERROR_SP3", "LARK_HOOK_STATUS_SP3")
    original_env = {name: os.environ.get(name) for name in names}

    os.environ["LARK_HOOK_ERROR_SP3"] = "https://hook/sp3-errors"
    os.environ["LARK_HOOK_STATUS_SP3"] = "https://hook/sp3-status"

    original_send_text = pending_photos.send_text_via_hook
    original_card = bot.send_card_via_hook

    sent_text, sent_cards = [], []

    pending_photos.send_text_via_hook = lambda url, text: (
        sent_text.append((url, text)), {"code": 0}
    )[1]
    bot.send_card_via_hook = lambda url, card: (
        sent_cards.append((url, card)), {"code": 0, "msg": "success"}
    )[1]

    try:
        pending_photos.forward_error(
            {
                "error_type": "Unable to drive",
                "error_text": "Security module failure",
                "robot": "5016",
            },
            [("🤖 Robot", "5016")],
            "SMALL-P3",
        )

        check(
            "hooks: ошибка SP3 ушла в свой вебхук",
            sent_text and sent_text[0][0] == "https://hook/sp3-errors",
            sent_text,
        )

        bot._notify_lark_status(
            "offline",
            {
                "robot": {
                    "robot_number": 5016,
                    "robot_type": "RT",
                    "warehouse": "SMALL-P3",
                },
                "old_status": "在线 | Online",
                "new_status": "离线 | Offline",
                "type_problem": "Other",
                "problem_note": "test",
            },
            "Ivan Petrenko",
        )

        check(
            "hooks: статус SP3 ушёл в свой вебхук",
            sent_cards and sent_cards[0][0] == "https://hook/sp3-status",
            [url for url, _ in sent_cards],
        )
        check(
            "hooks: в карточке статуса есть робот",
            sent_cards
            and "5016" in json.dumps(sent_cards[0][1], ensure_ascii=False),
            sent_cards,
        )
    finally:
        pending_photos.send_text_via_hook = original_send_text
        bot.send_card_via_hook = original_card

        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

def test_no_spam_for_non_text_messages():
    """Стикеры, войсы, сервисные сообщения — молча, без ответов и подсказок."""
    LINKS[100] = "Ivan Petrenko"
    ROBOTS.clear()
    ROBOTS["3780"] = _robot(3780, 101)

    def silent(update, label):
        sent = run(update)

        check(
            f"spam: {label} — бот молчит",
            sent == [],
            sent,
        )

    # медиа без подписи
    for field, value in (
        ("sticker", {"file_id": "s", "emoji": "🙂"}),
        ("voice", {"file_id": "v", "duration": 3}),
        ("video", {"file_id": "m", "duration": 5}),
        ("video_note", {"file_id": "n", "duration": 5}),
        ("animation", {"file_id": "a"}),
        ("audio", {"file_id": "au", "duration": 5}),
        ("contact", {"phone_number": "+48", "first_name": "Ivan"}),
        ("location", {"latitude": 51.0, "longitude": 17.0}),
        ("poll", {"id": "1", "question": "?"}),
    ):
        update = make_update(text=None, message_id=MESSAGE_SEQ[0] + 1, thread_id=2)
        update["message"][field] = value
        silent(update, field)

    # сервисные сообщения форума
    for field in ("pinned_message", "new_chat_members", "forum_topic_edited"):
        update = make_update(text=None, message_id=MESSAGE_SEQ[0] + 1, thread_id=2)
        update["message"][field] = {"name": "x"} if "topic" in field else {}
        silent(update, field)

    # вложение с подписью, в которой нет ошибки
    update = make_update(caption="смотрите какое видео", message_id=10301, thread_id=2)
    update["message"]["video"] = {"file_id": "m", "duration": 5}
    silent(update, "вложение с посторонней подписью")

    # вложение с ошибкой в подписи — сохраняем как обычный текст
    DB_CALLS.clear()
    FORWARDED.clear()

    update = make_update(
        caption="Unable to drive: Security module failure. 3780",
        message_id=10302,
        thread_id=2,
    )
    update["message"]["document"] = {"file_id": "d", "file_name": "video.mp4"}

    sent = run(update)

    check(
        "spam: ошибка в подписи к вложению сохраняется",
        DB_CALLS and DB_CALLS[-1]["parsed"]["robot"] == "3780",
        DB_CALLS,
    )
    check(
        "spam: карточка ушла в Lark",
        FORWARDED and "3780" in json.dumps(FORWARDED, ensure_ascii=False),
        FORWARDED,
    )
    check(
        "spam: сотруднику пришло подтверждение, а не подсказка формата",
        sent and "Saved" in sent[0]["text"],
        sent,
    )

def test_background_flush_keeps_topic():
    """Фоновые досылки (фото, подсказка по номеру) отвечают в свой топик."""
    LINKS[100] = "Ivan Petrenko"

    import sendToDataBase as stdb

    def fake_queue_notifies(robot_number, employee_card_id=None, chat_id=None,
                            notify=True, warehouse=None):
        """Как настоящий queue_missing_robot: ставит в очередь и уведомляет."""
        if notify and chat_id is not None:
            stdb.notify_user(
                chat_id,
                f"⚠️ Robot #{robot_number} is not in the system.",
            )

        return True

    # Нотификатор как в main(): ответы пользователю идут через _send в Telegram.
    stdb.set_notifier(lambda chat_id, text: bot._send(chat_id, text))

    # Другие тесты подменяют send_message своими заглушками и восстанавливают
    # «как было» — возвращаем общий фейк явно.
    original_tg_send = tg.send_message
    tg.send_message = fake_send_message

    original_send = bot.send_photo
    original_queue = bot.queue_missing_robot
    original_raw = dict(bot.TOPIC_RAW)
    original_names = dict(bot._topic_names)
    t_notifier = stdb._notifier

    bot.TOPIC_RAW = {
        "error": "2",
        "error:SMALL-P3": "318",
        "status": "319",
        "stats": "320",
        "service": "321",
    }

    bot.send_photo = lambda path, caption=None, console=None, warehouse=None: {
        "mode": "lark",
        "url": None,
    }

    try:
        # фото пришло в топик SP3 (318), ждёт текст, но текст не пришёл
        bot.set_origin_thread(-500, 318)
        bot.put_pending_photo(
            -500, 100, "x.jpg", 1, "cap",
            warehouse="SMALL-P3",
            thread_id=bot._reply_thread(-500),
        )

        with bot._photo_lock:
            stored = dict(bot._pending_photo.get((-500, 100)) or {})

        check(
            "origin: фото запомнило топик и склад",
            stored.get("thread_id") == 318 and stored.get("warehouse") == "SMALL-P3",
            stored,
        )

        SENT.clear()

        with bot._photo_lock:
            bot._pending_photo.pop((-500, 100), None)

        bot.flush_pending_photo(-500, 100, stored)

        check(
            "origin: досыл фото ушёл в топик SP3",
            SENT and all(item["thread_id"] == 318 for item in SENT),
            [item["thread_id"] for item in SENT],
        )

        # подсказка по номеру из топика SP3, ответа нет — досылаем по TTL
        bot.set_origin_thread(-500, 318)
        bot._ask_robot_fix(
            -500,
            {"id": 100, "username": "tester", "first_name": "Tester"},
            "Ivan Petrenko",
            {"robot": "5016", "error_text": "Security module failure",
             "error_type": "Unable to drive"},
            None,
            ["5160"],
            60072001,
        )

        with bot._pending_robot_fix_lock:
            for key in list(bot._pending_robot_fix):
                bot._pending_robot_fix[key]["expires"] = time.time() - 1

        SENT.clear()
        bot.queue_missing_robot = fake_queue_notifies
        bot.flush_expired_robot_fixes()

        check(
            "origin: досыл по номеру ушёл в топик SP3",
            SENT and all(item["thread_id"] == 318 for item in SENT),
            [item["thread_id"] for item in SENT],
        )
    finally:
        tg.send_message = original_tg_send
        stdb.set_notifier(t_notifier)
        bot.send_photo = original_send
        bot.queue_missing_robot = original_queue
        bot.TOPIC_RAW = original_raw
        bot._topic_names.clear()
        bot._topic_names.update(original_names)
        bot._pending_robot_fix.clear()

        with bot._photo_lock:
            bot._pending_photo.pop((-500, 100), None)


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
        test_db_error_is_not_reported_as_unregistered,
        test_send_to_data_base_db_error_paths,
        test_shutdown_saves_offset_and_releases_lease,
        test_user_commands_are_deleted,
        test_status_note_deleted_and_change_message_kept,
        test_short_photo_link_and_redirect,
        test_photo_forwarded_when_attachment_disabled,
        test_photo_with_error_caption_creates_one_record,
        test_photo_waits_for_text_then_combines,
        test_photo_attaches_to_recent_error,
        test_flush_pending_photo_modes,
        test_photo_fallback_logic,
        test_ignored_cases,
        test_commands,
        test_supabase_notifier,
        test_lark_hook_post_never_raises,
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
        test_failed_update_is_retried_then_skipped,
        test_polling_persists_offset,
        test_report_sent_ok_semantics,
        test_health_reports_degraded,
        test_rest_post_ignores_conflict,
        test_parser_and_count_guards,
        test_rest_count_parses_content_range,
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
        test_offset_saved_for_every_confirmed_update,
        test_failed_update_never_confirms_offset,
        test_status_note_kept_when_db_write_fails,
        test_command_replies_do_not_reply_to_deleted_message,
        test_user_messages_not_deleted_in_private_chats,
        test_short_link_negative_cache_and_lru,
        test_short_link_distinguishes_storage_failure,
        test_bucket_public_flag_updates_existing_bucket,
        test_table_probe_and_lease_error_caching,
        test_flush_expired_photos_noop_when_disabled,
        test_cache_helpers_are_bounded,
        test_time_utils_parse_iso,
        test_lease_stale_with_short_fraction,
        test_queue_autoclose_does_not_mix_warehouses,
        test_queue_autoclose_closes_known_robots,
        test_queue_autoclose_db_error_does_not_patch,
        test_robot_card_command,
        test_robot_fix_suggestion_then_correction,
        test_robot_fix_declined_keeps_number,
        test_robot_fix_expiry_flushes_as_is,
        test_downtime_metrics_from_history,
        test_shift_window_bounds,
        test_digest_build_stale_and_queue,
        test_digest_send_and_marker,
        test_digest_due_and_command,
        test_analytics_top_report,
        test_analytics_downtime_report,
        test_analytics_weekly_and_schedule,
        test_analytics_commands,
        test_topic_map_and_resolution,
        test_answers_stay_in_origin_topic,
        test_background_flush_keeps_topic,
        test_topics_command,
        test_warehouses_config_and_args,
        test_two_warehouses_strict_lookup,
        test_warehouse_argument_in_commands,
        test_topic_name_learned_from_service_message,
        test_lark_hooks_resolve,
        test_error_and_status_hooks_by_warehouse,
        test_no_spam_for_non_text_messages,
    ]

    # T6: ручной список легко забыть обновить — проверяем это явно.
    registered = {test.__name__ for test in tests}
    defined = {
        name
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    }
    unregistered = sorted(defined - registered)

    if unregistered:
        print("НЕ ЗАРЕГИСТРИРОВАНЫ В main():", ", ".join(unregistered))
        return 1

    for test in tests:
        print(f"\n--- {test.__name__} ---")

        # T5: состояние одного теста не должно влиять на другой — иначе
        # «дубль» из старого теста глушит сообщение в новом.
        with bot._seen_lock:
            bot._seen_message_ids.clear()

        bot._pending_robot_fix.clear()
        bot._pending_photo.clear()
        bot._last_error.clear()

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
