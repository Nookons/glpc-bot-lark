"""
Offline-проверки живого тестового стенда (tools/live_stub.py + live_test_bot.py).

Ничего не ходит в сеть: сам стенд поднимается на 127.0.0.1, а модули бота
подключаются к нему. Так проверяется ровно то, чего не видит обычный
offline-набор: что «живой» прогон действительно уходит на локальный стенд,
что продовые адреса отсекаются, и что реальный поток обработки сообщения
доходит до записи в локальную базу и до Lark-заглушки.

Запуск:

    python3 tests/test_live_test_harness.py
"""

import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Окружение стенда обязано применяться к модулям бота, даже если они уже
# импортированы другим набором (discover импортирует все модули заранее).
# Подробности и обоснование — в tests/bot_config_isolation.py.
from bot_config_isolation import BotEnvIsolation  # noqa: E402


def _load(name: str, path: Path):
    """Загружает модуль tools/ по пути (там нет __init__.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


stub = _load("live_stub_under_test", PROJECT_ROOT / "tools" / "live_stub.py")
runner = _load("live_runner_under_test", PROJECT_ROOT / "tools" / "live_test_bot.py")

PORT = 8877
BASE = f"http://127.0.0.1:{PORT}"

# Реальная конфигурация бота подменяется на локальный стенд ДО импорта.
TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:LIVE-TEST-TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS": "-100200",
    "TELEGRAM_LISTEN_TOPICS": "",
    "TELEGRAM_TOPIC_ID": "",
    "TELEGRAM_TOPIC_NAME": "",
    "TELEGRAM_TOPIC_ERROR_GLPC": "",
    "TELEGRAM_TOPIC_ERROR_SP3": "",
    "TELEGRAM_TOPIC_STATUS": "",
    "TELEGRAM_TOPIC_STATS": "",
    "TELEGRAM_TOPIC_SERVICE": "",
    "SUPABASE_URL": BASE,
    "SUPABASE_SERVICE_KEY": "live-test-key",
    "SUPABASE_PHOTO_BUCKET": "live-test-photos",
    "SUPABASE_STATE_BUCKET": "live-test-state",
    "SUPABASE_PHOTO_BUCKET_PUBLIC": "false",
    "PUBLIC_PHOTO_URLS": "false",
    "PHOTO_LINK_BASE": BASE,
    "LARK_TARGET_HOOK_URL": f"{BASE}/hook/livetest",
    "LARK_HOOK_ERROR_GLPC": f"{BASE}/hook/livetest",
    "LARK_HOOK_ERROR_SP3": f"{BASE}/hook/livetest",
    "LARK_HOOK_STATUS_GLPC": f"{BASE}/hook/livetest",
    "LARK_HOOK_STATUS_SP3": f"{BASE}/hook/livetest",
    "LARK_HOOK_SECRET": "",
    "LARK_APP_ID": "",
    "LARK_APP_SECRET": "",
    "BOT_LEASE_NAME": "live-test-lease",
    "IMAGES_DIR": "/tmp/live-test-images",
    "LOG_DIR": "/tmp/live-test-logs",
    "TELEGRAM_DRY_RUN": "",
}


def _request(method, path, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers=headers or {"Content-Type": "application/json"},
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()

            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read()

        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None

        return error.code, parsed


def _start_isolation():
    """
    Ставит окружение стенда и перечитывает им конфигурацию модулей бота.

    Вызывается из `setUpClass` каждого класса: `discover` импортирует модули
    бота заранее (и `test_equipment_intake_production` делает это с боевым
    `.env`), поэтому одного `os.environ.update` недостаточно — надо ещё
    перезагрузить модули (см. `tests/bot_config_isolation.py`).
    """
    isolation = BotEnvIsolation(TEST_ENV)
    isolation.start()

    return isolation


class HarnessTestCase(unittest.TestCase):
    """Один стенд на весь класс: поднимается в setUpClass, гасится в конце."""

    @classmethod
    def setUpClass(cls):
        handler = type(
            "TestStubHandler",
            (stub.Handler,),
            {"store": stub.Store(), "log_path": None},
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Окружение стенда применяется к уже импортированным модулям бота.
        cls.isolation = _start_isolation()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

        # Возвращаем окружение и конфигурацию модулей как было: следующий
        # набор должен увидеть то же, что и до нас (независимость от порядка).
        cls.isolation.stop()

    def setUp(self):
        _request("POST", "/__fault", {"action": "fault_off"})
        request_clean = _request("GET", "/__dump")[1]
        # Между тестами чистим журналы, чтобы счётчики были предсказуемы.
        self.store = self.server.RequestHandlerClass.store
        self.store.reset_logs()

    # ---------- стенд ----------

    def test_stub_serves_seeded_tables(self):
        status, rows = _request("GET", "/rest/v1/robots_maintenance_list?select=*")

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["warehouse"], "GLP-C")

    def test_stub_supports_eq_and_order(self):
        status, rows = _request(
            "GET",
            "/rest/v1/robots_maintenance_list?select=*&warehouse=eq.SMALL-P3&order=updated_at.desc&limit=1",
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["robot_number"], "3490")

    def test_stub_enforces_unique_key(self):
        _request("POST", "/rest/v1/exceptions_glpc", {"uniq_key": "k1"})
        status, _ = _request(
            "POST",
            "/rest/v1/exceptions_glpc",
            {"uniq_key": "k1"},
            {"Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates"},
        )

        self.assertEqual(status, 409)

    def test_stub_patch_and_delete(self):
        _request("POST", "/rest/v1/telegram_users", {"telegram_id": 5, "employee_name": "A"})
        status, rows = _request(
            "PATCH", "/rest/v1/telegram_users?telegram_id=eq.5", {"employee_name": "B"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(rows[0]["employee_name"], "B")

        status, _ = _request("DELETE", "/rest/v1/telegram_users?telegram_id=eq.5")
        self.assertEqual(status, 204)
        _, rows = _request("GET", "/rest/v1/telegram_users?select=*")
        self.assertEqual(rows, [])

    def test_stub_counts_with_content_range(self):
        _request("POST", "/rest/v1/exceptions", {"a": 1})
        _request("POST", "/rest/v1/exceptions", {"a": 2})

        request = urllib.request.Request(
            BASE + "/rest/v1/exceptions?select=id&limit=0",
            headers={"Prefer": "count=exact"},
            method="GET",
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.headers.get("Content-Range"), "0-0/2")

    def test_stub_storage_roundtrip_and_signed_url(self):
        status, _ = _request(
            "POST", "/storage/v1/object/live-test-photos/photo.jpg", {"x": 1}
        )
        self.assertEqual(status, 200)

        status, body = _request(
            "POST", "/storage/v1/object/sign/live-test-photos/photo.jpg", {"expiresIn": 60}
        )
        self.assertEqual(status, 200)
        self.assertIn("signedURL", body)

        status, _ = _request(
            "POST", "/storage/v1/object/sign/live-test-photos/missing.jpg", {"expiresIn": 60}
        )
        self.assertEqual(status, 400)

    def test_stub_lark_hook_records_and_returns_code_zero(self):
        status, body = _request(
            "POST",
            "/hook/livetest",
            {"msg_type": "interactive", "card": {"header": {"title": {"content": "T"}}}},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["code"], 0)

        _, dump = _request("GET", "/__dump")
        self.assertEqual(len(dump["lark"]), 1)
        self.assertEqual(dump["lark"][0]["payload"]["msg_type"], "interactive")

    def test_read_only_fault_blocks_writes_but_keeps_reads(self):
        """read_only — это «нельзя писать», а не «база лежит»."""
        _request("POST", "/__fault", {"action": "read_only", "value": True})

        try:
            status, _ = _request("GET", "/rest/v1/issue_templates")
            self.assertEqual(status, 200)

            status, _ = _request("POST", "/rest/v1/exceptions", {"a": 1})
            self.assertEqual(status, 403)
            self.assertEqual(self.store.tables["exceptions"], [])
        finally:
            _request("POST", "/__fault", {"action": "fault_off"})

    def test_fault_injection_affects_rest_only(self):
        _request("POST", "/__fault", {"action": "status", "value": 500})
        status, _ = _request("GET", "/rest/v1/issue_templates")
        self.assertEqual(status, 500)

        status, _ = _request("GET", "/__dump")
        self.assertEqual(status, 200)

        _request("POST", "/__fault", {"action": "fault_off"})

    # ---------- предстартовые проверки запускателя ----------

    def test_local_only_accepts_local_endpoints(self):
        env = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")

        self.assertEqual(
            set(runner.assert_local_only(env)),
            set(runner.LOCAL_ENDPOINTS),
        )

    def test_local_only_rejects_production_supabase(self):
        env = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")
        env["SUPABASE_URL"] = "https://ljkugtpeboomboobodom.supabase.co"

        with self.assertRaises(SystemExit) as caught:
            runner.assert_local_only(env)

        message = str(caught.exception)
        self.assertIn("LIVE TEST NOT STARTED", message)
        self.assertIn("ljkugtpeboomboobodom.supabase.co", message)

    def test_local_only_rejects_remote_webhook(self):
        env = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")
        env["LARK_HOOK_ERROR_GLPC"] = "https://open.larksuite.com/open-apis/bot/v2/hook/deadbeef"

        with self.assertRaises(SystemExit) as caught:
            runner.assert_local_only(env)

        self.assertIn("не на локальный стенд", str(caught.exception))

    def test_local_only_rejects_lark_credentials(self):
        env = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")
        env["LARK_APP_ID"] = "cli_x"

        with self.assertRaises(SystemExit) as caught:
            runner.assert_local_only(env)

        self.assertIn("LARK_APP_ID", str(caught.exception))

    def test_environment_disables_quota_and_schedulers(self):
        env = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")

        self.assertEqual(env["LARK_APP_ID"], "")
        self.assertEqual(env["LARK_APP_SECRET"], "")
        self.assertEqual(env["DIGEST_ENABLED"], "false")
        self.assertEqual(env["WEEKLY_REPORT_ENABLED"], "false")
        # Отдельное имя лиза: локальный прогон не отбирает лиз у прода.
        self.assertEqual(env["BOT_LEASE_NAME"], "live-test-lease")
        self.assertEqual(env["PUBLIC_PHOTO_URLS"], "false")

    def test_environment_sets_dry_run_only_in_dry_run_mode(self):
        live = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "live")
        dry = runner.environment(PORT, {"token": "1:2", "chat_ids": "-1", "listen_topics": ""}, "dry-run")

        self.assertEqual(live["TELEGRAM_DRY_RUN"], "")
        self.assertEqual(dry["TELEGRAM_DRY_RUN"], "1")

    def test_purge_removes_inherited_production_variables(self):
        os.environ["SUPABASE_URL"] = "https://production.example"
        os.environ["LARK_TARGET_HOOK_URL"] = "https://production.example/hook"
        os.environ["UNRELATED_SETTING"] = "keep"

        try:
            removed = runner.purge_inherited_env()

            self.assertIn("SUPABASE_URL", removed)
            self.assertIn("LARK_TARGET_HOOK_URL", removed)
            self.assertNotIn("SUPABASE_URL", os.environ)
            self.assertEqual(os.environ.get("UNRELATED_SETTING"), "keep")
        finally:
            os.environ.pop("UNRELATED_SETTING", None)

    def test_tree_source_url_cannot_bypass_the_guard(self):
        """
        `equipment_intake` умеет тянуть дерево по URL через urllib, минуя
        `requests`. Такой источник должен сниматься с окружения, иначе
        живой тест мог бы сходить наружу.
        """
        os.environ["EQUIPMENT_INTAKE_TREE_SOURCE"] = "https://example.com/tree.json"

        removed = runner.purge_inherited_env()

        self.assertIn("EQUIPMENT_INTAKE_TREE_SOURCE", removed)
        self.assertNotIn("EQUIPMENT_INTAKE_TREE_SOURCE", os.environ)

    def test_network_guard_blocks_non_allowlisted_hosts(self):
        runner.BLOCKED_CALLS.clear()
        original = runner.install_network_guard(PORT)

        import requests

        try:
            with self.assertRaises(RuntimeError) as caught:
                requests.get("https://open.larksuite.com/open-apis/bot/v2/hook/x")

            self.assertIn("LIVE-TEST BLOCKED", str(caught.exception))
            self.assertEqual(len(original), 1)
        finally:
            import importlib

            importlib.reload(requests.sessions)

    def test_isolation_redirects_import_bound_hook_references(self):
        """
        Регрессия: shift_report/analytics берут TARGET_HOOK_URL из
        pending_photos на импорте. Без перезаписи этих ссылок отчёты по
        смене ушли бы в БОЕВОЙ вебхук (lark_hooks.DEFAULT_TARGET_HOOK_URL),
        хотя адреса в окружении локальные.
        """
        os.environ.update(TEST_ENV)
        runner.patch_bot_isolation()

        import analytics
        import lark_hooks
        import lark_media
        import pending_photos
        import shift_report

        hook = f"{BASE}/hook/livetest"

        for module in (lark_hooks, pending_photos, shift_report, analytics):
            value = getattr(module, "TARGET_HOOK_URL", hook)
            self.assertEqual(value, hook, f"{module.__name__} ходит не на стенд")

        self.assertEqual(lark_hooks.DEFAULT_TARGET_HOOK_URL, hook)
        self.assertNotIn("larksuite", lark_hooks.DEFAULT_TARGET_HOOK_URL)
        self.assertEqual(pending_photos.upload_image.__qualname__, "patch_bot_isolation.<locals>.no_lark_upload")
        self.assertEqual(lark_media.upload_image.__qualname__, "patch_bot_isolation.<locals>.no_lark_upload")

        with self.assertRaises(RuntimeError):
            pending_photos.upload_image("/tmp/whatever.jpg")

    def test_lease_is_renewed_not_on_every_check(self):
        """Heartbeat лиза продлевается по интервалу, а не на каждый вызов.

        Найдено по логам **живого прода**: `check()` вызывается на каждой
        итерации опроса, и heartbeat уходил в базу каждые ~7 секунд при TTL 90
        (91 строка `PATCH bot_leases` за 8 минут). Это и шум в логах, и лишние
        запросы; продлевать достаточно примерно втрое чаще TTL.

        Проверяется число запросов, а не текст лога.
        """
        import time

        import bot_lease

        patches = {"n": 0}
        saved_get, saved_patch = bot_lease.rest_get, bot_lease.rest_patch
        saved_ok, saved_renew = bot_lease._table_ok, bot_lease._last_renew_at
        self.addCleanup(
            lambda: (
                setattr(bot_lease, "rest_get", saved_get),
                setattr(bot_lease, "rest_patch", saved_patch),
                setattr(bot_lease, "_table_ok", saved_ok),
                setattr(bot_lease, "_last_renew_at", saved_renew),
            )
        )

        bot_lease.rest_get = lambda *a, **k: [
            {
                "name": bot_lease.LEASE_NAME,
                "holder": bot_lease.holder_id(),
                "heartbeat_at": bot_lease._now_iso(),
            }
        ]

        def rest_patch(*args, **kwargs):
            patches["n"] += 1
            return [{"ok": True}]

        bot_lease.rest_patch = rest_patch
        bot_lease._table_ok = True
        bot_lease._last_renew_at = 0.0

        for _ in range(20):
            state = bot_lease.check()

        self.assertEqual(state, "ok")
        self.assertEqual(patches["n"], 1, "heartbeat продлевается на каждый вызов check()")

        # По истечении интервала продление обязано произойти: иначе лиз
        # просрочится и его заберёт сосед.
        bot_lease._last_renew_at = time.time() - bot_lease.LEASE_RENEW_INTERVAL_SECONDS - 1
        bot_lease.check()

        self.assertEqual(patches["n"], 2, "продление не произошло после интервала")

    def test_lease_renew_interval_leaves_margin(self):
        """Интервал продления обязан быть заметно меньше TTL.

        Иначе при задержке одного продления лиз просрочится, и работу заберёт
        другой инстанс — то есть появятся дубли сообщений.
        """
        import bot_lease

        self.assertLess(
            bot_lease.LEASE_RENEW_INTERVAL_SECONDS * 2,
            bot_lease.LEASE_TTL_SECONDS,
            "интервал продления слишком близок к TTL — нет запаса",
        )

    def test_lease_name_is_not_the_production_one(self):
        import bot_lease

        self.assertNotEqual(bot_lease.LEASE_NAME, "glpc-bot-telegram")
        self.assertEqual(bot_lease.TABLE, "bot_leases")


class BotFlowTestCase(unittest.TestCase):
    """
    Реальный поток бота на локальном стенде.

    Telegram-отправка подменяется перехватчиком (сеть наружу здесь не
    нужна), всё остальное — настоящий код бота: маршрутизация, парсер,
    запись в Supabase-стенд и отправка карточки в Lark-заглушку.
    """

    @classmethod
    def setUpClass(cls):
        handler = type(
            "FlowStubHandler",
            (stub.Handler,),
            {"store": stub.Store(), "log_path": None},
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # Окружение стенда применяется к уже импортированным модулям бота.
        cls.isolation = _start_isolation()

        import telegram_api as tg
        import telegram_bot as bot
        import equipment_intake.flow as flow
        import equipment_intake.editor as editor

        cls.tg = tg
        cls.bot = bot
        cls.flow = flow
        cls.editor = editor
        cls.sent = []
        cls.original_send = tg.send_message
        cls.original_send_photo = tg.send_photo

        tg.send_message = lambda chat_id, text, **kwargs: (
            cls.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            or {"message_id": len(cls.sent), "chat": {"id": chat_id}}
        )
        tg.send_photo = lambda chat_id, path, **kwargs: (
            cls.sent.append({"chat_id": chat_id, "photo": path, **kwargs})
            or {"message_id": len(cls.sent), "chat": {"id": chat_id}}
        )
        cls.bot.set_notifier(lambda chat_id, text: cls.bot._send(chat_id, text))
        cls.bot.BOT_USERNAME = "test_tk_1_bot"

    @classmethod
    def tearDownClass(cls):
        cls.tg.send_message = cls.original_send
        cls.tg.send_photo = cls.original_send_photo
        cls.server.shutdown()
        cls.server.server_close()

        # Возвращаем окружение и конфигурацию модулей как было: следующий
        # набор должен увидеть то же, что и до нас (независимость от порядка).
        cls.isolation.stop()

    def setUp(self):
        self.server.RequestHandlerClass.store.reset_logs()
        _request("POST", "/__fault", {"action": "fault_off"})
        self.sent.clear()
        # База между тестами: строки, которые пишет бот, накапливались бы.
        self.server.RequestHandlerClass.store.tables["exceptions"] = []
        self.server.RequestHandlerClass.store.tables["exceptions_glpc"] = []
        self.server.RequestHandlerClass.store.tables["robots_to_add"] = []

    # Сквозной счётчик: дедупликация бота ключуется (chat, message_id) и
    # живёт в памяти процесса, поэтому в каждом вызове id должен быть новым.
    _next_message_id = [100]

    def _message(self, text=None, chat_id=-100200, thread_id=None, user_id=777):
        self._next_message_id[0] += 1

        return {
            "update_id": self._next_message_id[0],
            "message": {
                "message_id": self._next_message_id[0],
                "date": 1800000000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "Live Test"},
                "from": {"id": user_id, "username": "live-tester", "is_bot": False},
                "text": text,
                **({"message_thread_id": thread_id} if thread_id else {}),
            },
        }

    def _run(self, text, thread_id=None, chat_id=-100200):
        self.bot.handle_update(
            self._message(text, chat_id=chat_id, thread_id=thread_id),
            "test_tk_1_bot",
        )

    def test_chat_outside_allowlist_is_ignored(self):
        """Чужой чат: обычное сообщение не обрабатывается.

        /id и /help — исключения (BOOTSTRAP_COMMANDS), они отвечают всегда:
        иначе chat_id тестовой группы было бы не узнать.
        """
        self._run("Unable to drive: Security module failure. 3780", chat_id=-555)

        self.assertEqual(len(self.sent), 0, self.sent)

    def test_unknown_command_gets_hint(self):
        self._run("/nope")

        self.assertTrue(self.sent, self.sent)
        self.assertIn("Unknown command", self.sent[0]["text"])

    def test_error_message_is_written_to_local_stub_and_lark(self):
        # /reg недоступен без сотрудника, поэтому связываем пользователя
        # напрямую через локальную таблицу стенда (как это сделал бы /reg).
        _request(
            "POST",
            "/rest/v1/telegram_users",
            {"telegram_id": 777, "employee_name": "Test Worker", "telegram_username": "live-tester"},
        )

        self._run("Unable to drive: Security module failure. 3780")

        _, dump = _request("GET", "/__dump")
        glpc = dump["tables"].get("exceptions_glpc") or []

        self.assertTrue(
            glpc,
            f"бот не записал ошибку в локальный стенд. Отправлено: {self.sent}",
        )
        self.assertEqual(glpc[0]["error_robot"], "3780")
        self.assertEqual(glpc[0]["warehouse"], "GLP-C")
        # Карточка ушла в Lark-заглушку, а не в боевой вебхук.
        self.assertTrue(dump["lark"], "карточка не дошла до Lark-заглушки")

    def test_unknown_robot_goes_to_local_queue(self):
        _request(
            "POST",
            "/rest/v1/telegram_users",
            {"telegram_id": 777, "employee_name": "Test Worker", "telegram_username": "live-tester"},
        )

        self._run("Unable to drive: Security module failure. 99999")

        _, dump = _request("GET", "/__dump")
        queue = dump["tables"].get("robots_to_add") or []

        self.assertTrue(queue, f"неизвестный робот не попал в очередь: {self.sent}")
        self.assertEqual(str(queue[0]["robot_number"]), "99999")

    def test_stub_failure_does_not_crash_dispatch(self):
        """Сбой базы — это «нет данных», а не падение бота (согласованность)."""
        _request("POST", "/__fault", {"action": "status", "value": 500})

        try:
            self._run("Unable to drive: Security module failure. 3780")
        except Exception as error:  # pragma: no cover - падение = провал теста
            self.fail(f"обработка упала при сбое базы: {error!r}")
        finally:
            _request("POST", "/__fault", {"action": "fault_off"})

    def test_intake_tree_command_is_available(self):
        self._run("/tree")

        self.assertTrue(self.sent, "нет ответа на /tree")

    # ---------- точность стенда ----------

    def test_stub_rejects_unknown_column_like_postgrest(self):
        """Стенд обязан ругаться на неизвестную колонку, как живой PostgREST.

        Проверено на живой базе: `select=is_builtin` до применения
        `sql/intake_editor_v2.sql` даёт **400 / 42703**. Стенд раньше отвечал
        200, то есть был мягче реальности — и живой прогон показывал бы полное
        редактирование дерева, которого в проде нет. Молчаливое расхождение в
        опасную сторону.
        """
        status, body = _request(
            "GET", "/rest/v1/telegram_intake_options?select=is_builtin&limit=1"
        )

        self.assertEqual(status, 400, "стенд принял колонку, которой нет в схеме")
        self.assertEqual(body.get("code"), "42703")
        self.assertIn("is_builtin", body.get("message", ""))

    def test_stub_accepts_known_column(self):
        """Известная колонка проходит: мягкость не должна стать отказом везде."""
        status, _ = _request(
            "GET", "/rest/v1/telegram_intake_options?select=node_id&limit=1"
        )

        self.assertEqual(status, 200)

    def test_stub_apply_v2_enables_columns(self):
        """`apply_v2` включает схему миграции — как после её применения."""
        store = self.server.RequestHandlerClass.store
        saved = {k: list(v) for k, v in store.schema.items()}

        def restore():
            store.schema = saved
            store.v2_applied = False

        self.addCleanup(restore)

        status, _ = _request(
            "GET", "/rest/v1/telegram_intake_options?select=sort_order&limit=1"
        )
        self.assertEqual(status, 400, "до apply_v2 колонки быть не должно")

        _request("POST", "/__fault", {"action": "apply_v2"})

        status, _ = _request(
            "GET", "/rest/v1/telegram_intake_options?select=sort_order&limit=1"
        )
        self.assertEqual(status, 200, "после apply_v2 колонка обязана появиться")




if __name__ == "__main__":
    unittest.main(verbosity=2)
