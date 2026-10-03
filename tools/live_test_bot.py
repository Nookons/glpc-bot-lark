"""
Живой прогон бота через Telegram без риска для прода.

Что делает запускатель:

1. Читает секреты тестового бота из `.env.livetest` (файл в .gitignore).
2. Fail-closed проверяет, что все адреса указывают на локальную заглушку:
   Supabase, Storage и Lark-вебхуки обязаны быть на 127.0.0.1. Любая
   попытка уйти в прод — отказ старта, а не «тихая запись».
3. Поднимает `tools/live_stub.py` (PostgREST + Storage + Lark в памяти).
4. Патчит сеть процесса бота: наружу разрешён ровно один хост —
   `api.telegram.org` (иначе бот не получит сообщения). Supabase, Storage,
   Lark Open Platform и вебхуки блокируются на уровне `requests`.
5. Опрашивает Telegram только для чатов из `TELEGRAM_ALLOWED_CHAT_IDS`.

Режимы:

    python3 tools/live_test_bot.py check      # проверить конфиг, без запуска
    python3 tools/live_test_bot.py discover   # узнать chat_id тестовой группы
    python3 tools/live_test_bot.py dry-run    # живой ответ, ноль записей
    python3 tools/live_test_bot.py live       # полный прогон на стенд
    python3 tools/live_test_bot.py selftest   # проверка стенда, без сети

Продовый бот (`@tk_servie_bot`) и продовая база не участвуют вообще.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Единственный внешний хост, который боту действительно нужен.
TELEGRAM_HOST = "api.telegram.org"

# Бот, которым предполагается гонять живой тест.
EXPECTED_TEST_BOT = "test_tk_1_bot"

DEFAULT_PORT = 8799

PRODUCTION_SUPABASE_HOST = "ljkugtpeboomboobodom.supabase.co"

# Всё, что бот обязан видеть локально (иначе это выход в прод).
LOCAL_ENDPOINTS = (
    "SUPABASE_URL",
    "LARK_TARGET_HOOK_URL",
    "LARK_HOOK_ERROR_GLPC",
    "LARK_HOOK_ERROR_SP3",
    "LARK_HOOK_STATUS_GLPC",
    "LARK_HOOK_STATUS_SP3",
    "PHOTO_LINK_BASE",
)

# Переменные окружения, которые нельзя наследовать из shell/продового .env.
INHERITED_PREFIXES = (
    "SUPABASE_",
    "LARK_",
    "TELEGRAM_",
    "PHOTO_",
    "BOT_LEASE_",
    "PUBLIC_PHOTO_URLS",
    "IMAGES_DIR",
    "WAREHOUSE_",
    "EQUIPMENT_INTAKE_TREE_SOURCE",
    "PORT",
)


class LiveTestError(SystemExit):
    """Отказ старта: живой тест не стартует «наполовину безопасным»."""

    def __init__(self, message: str):
        super().__init__(f"LIVE TEST NOT STARTED: {message}")


def _fail(message: str) -> None:
    raise LiveTestError(message)


# ============================================================
# КОНФИГ
# ============================================================

def load_config() -> dict:
    """Читает .env.livetest и проверяет обязательные значения."""
    path = ROOT / ".env.livetest"

    if not path.exists():
        _fail(
            "нет .env.livetest — скопируйте .env.livetest.example и вставьте "
            "токен ТЕСТОВОГО бота (см. LIVE_TEST.md)"
        )

    raw = dotenv_values(path)

    token = str(raw.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = str(raw.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    port = int(str(raw.get("LIVE_TEST_PORT") or DEFAULT_PORT))
    mode = str(raw.get("LIVE_TEST_MODE") or "live").strip().lower()

    if not token:
        _fail("TELEGRAM_BOT_TOKEN не задан в .env.livetest")

    if ":" not in token:
        _fail("TELEGRAM_BOT_TOKEN не похож на токен BotFather")

    if not chat_ids:
        _fail(
            "TELEGRAM_ALLOWED_CHAT_IDS пуст: без белого списка бот примет "
            "сообщения из любой группы. Узнайте id: "
            "python3 tools/live_test_bot.py discover"
        )

    for part in chat_ids.replace(" ", "").split(","):
        if part and not part.lstrip("-").isdigit():
            _fail(f"TELEGRAM_ALLOWED_CHAT_IDS: {part!r} не является id")

    if mode not in ("live", "dry-run"):
        _fail(f"LIVE_TEST_MODE={mode!r}; допустимо live или dry-run")

    return {
        "token": token,
        "chat_ids": chat_ids,
        "port": port,
        "mode": mode,
        "listen_topics": str(raw.get("TELEGRAM_LISTEN_TOPICS") or "").strip(),
    }


def environment(port: int, config: dict, mode: str) -> dict:
    """Окружение бота целиком: всё внешнее — на локальный стенд."""
    base = f"http://127.0.0.1:{port}"
    hook = f"{base}/hook/livetest"

    return {
        # --- Telegram ---
        "TELEGRAM_BOT_TOKEN": config["token"],
        "TELEGRAM_ALLOWED_CHAT_IDS": config["chat_ids"],
        "TELEGRAM_LISTEN_TOPICS": config["listen_topics"],
        # Пусто: маршрутизацию задаёт белый список топиков.
        "TELEGRAM_TOPIC_ID": "",
        "TELEGRAM_TOPIC_NAME": "",
        "TELEGRAM_TOPIC_ERROR_GLPC": "",
        "TELEGRAM_TOPIC_ERROR_SP3": "",
        "TELEGRAM_TOPIC_STATUS": "",
        "TELEGRAM_TOPIC_STATS": "",
        "TELEGRAM_TOPIC_SERVICE": "",
        "TELEGRAM_TEST_REPLY_PREFIX": "[LIVE-TEST]",
        # --- Supabase: только стенд ---
        "SUPABASE_URL": base,
        "SUPABASE_SERVICE_KEY": "live-test-key",
        "SUPABASE_PHOTO_BUCKET": "live-test-photos",
        "SUPABASE_STATE_BUCKET": "live-test-state",
        "SUPABASE_PHOTO_BUCKET_PUBLIC": "false",
        "PUBLIC_PHOTO_URLS": "false",
        "PHOTO_LINK_BASE": base,
        # --- Lark: только стенд ---
        "LARK_TARGET_HOOK_URL": hook,
        "LARK_HOOK_ERROR_GLPC": hook,
        "LARK_HOOK_ERROR_SP3": hook,
        "LARK_HOOK_STATUS_GLPC": hook,
        "LARK_HOOK_STATUS_SP3": hook,
        "LARK_HOOK_SECRET": "",
        # Креды Lark Open Platform пусты: загрузка фото (квота) недоступна,
        # фото уходит ссылкой на локальный стенд.
        "LARK_APP_ID": "",
        "LARK_APP_SECRET": "",
        # --- Прочее ---
        "TELEGRAM_DRY_RUN": "1" if mode == "dry-run" else "",
        "BOT_LEASE_NAME": "live-test-lease",
        "TELEGRAM_CONFIRM": "true",
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(ROOT / "logs"),
        "IMAGES_DIR": str(ROOT / "logs" / "live-test-images"),
        # Планировщики в живом тесте не нужны: отчёты по расписанию смазали
        # бы картину, а дайджест разослал бы лишние сообщения.
        "DIGEST_ENABLED": "false",
        "WEEKLY_REPORT_ENABLED": "false",
        "TELEGRAM_CONFIRM_TTL": "0",
    }


# ============================================================
# ПРОВЕРКИ БЕЗОПАСНОСТИ
# ============================================================

def assert_local_only(env: dict) -> list:
    """
    Fail-closed проверка: все внешние адреса указывают на локальный стенд.

    Возвращает список проверенных переменных (для отчёта).
    """
    checked = []

    for name in LOCAL_ENDPOINTS:
        value = str(env.get(name) or "").strip()

        if not value:
            _fail(f"{name} пуст — живой тест обязан знать, куда пишет")

        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()

        if host not in LOCAL_HOSTS:
            _fail(
                f"{name}={value} смотрит не на локальный стенд "
                f"(хост {host!r}). Живой тест пишет только в память процесса."
            )

        if PRODUCTION_SUPABASE_HOST in value:
            _fail(f"{name} содержит адрес продовой Supabase")

        checked.append(name)

    for name in ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_HOOK_SECRET"):
        if str(env.get(name) or "").strip():
            _fail(
                f"{name} задан: это дало бы доступ к Lark Open Platform "
                f"(квота 10 000/мес). Живой тест работает без него."
            )

    return checked


def production_reference_values() -> dict:
    """
    Продовые значения из .env и окружения — только для сравнения.

    Значения нигде не печатаются и не копируются.
    """
    values = {}
    env_file = ROOT / ".env"
    production = dotenv_values(env_file) if env_file.exists() else {}

    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "LARK_TARGET_HOOK_URL",
        "TELEGRAM_BOT_TOKEN",
    ):
        candidates = {
            str(production.get(name) or "").strip(),
            str(os.environ.get(name) or "").strip(),
        }
        candidates.discard("")
        values[name] = candidates

    return values


def assert_not_production(env: dict, config: dict) -> None:
    """Ни один секрет теста не должен совпадать с продовым."""
    production = production_reference_values()

    if config["token"] in production["TELEGRAM_BOT_TOKEN"]:
        _fail(
            "токен в .env.livetest совпадает с продовым токеном бота. "
            "Живой тест обязан работать отдельным ботом (см. LIVE_TEST.md)."
        )

    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if str(env[name]) in production[name]:
            _fail(f"{name} совпадает с продовым значением")


def check_token_identity(token: str) -> str:
    """Проверяет токен через getMe и возвращает username."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"https://{TELEGRAM_HOST}/bot{token}/getMe", timeout=15
        ) as response:
            payload = json.load(response)
    except Exception as error:
        _fail(f"getMe недоступен: {error}")

    if not payload.get("ok"):
        _fail(f"getMe отклонил токен: {payload.get('description')}")

    return str((payload.get("result") or {}).get("username") or "")


def assert_single_poller(token: str, bot_username: str) -> None:
    """
    Ищет, не опрашивает ли бота кто-то ещё.

    Важная тонкость, проверенная на практике: короткий `getUpdates`
    (timeout=0) у живого соседа проходит успешно и возвращает пустой
    результат — потому что сосед как раз подтвердил апдейты, ридер пуст, а
    Telegram иногда не мешает двум одновременным запросам. Полагаться на
    такой признак нельзя: он даёт ложное «свободен».

    Гарантированный признак — длинный опрос: если параллельно опрашивает
    другой процесс, Telegram отвечает 409 «terminated by other getUpdates».
    Поэтому здесь запускаются два длинных опроса с перекрытием: `A` (владелец
    окна) и `B`, который стартует позже — именно он и должен получить 409.

    Побочный эффект: пара подтверждённых апдейтов теряется для того бота,
    который жил на этом токене. Поэтому запускатель прямо говорит владельцу
    остановить чужого опрашивающего, а не «перехватывает» его.
    """
    import threading
    import urllib.error
    import urllib.request

    url = f"https://{TELEGRAM_HOST}/bot{token}/getUpdates"
    results = {}

    def long_poll(label: str, timeout: int) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {"timeout": timeout, "limit": 1, "allowed_updates": ["message"]}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout + 20) as response:
                payload = json.load(response)
            results[label] = {
                "ok": bool(payload.get("ok")),
                "description": str(payload.get("description") or ""),
            }
        except urllib.error.HTTPError as error:
            body = error.read()

            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}

            results[label] = {
                "ok": False,
                "description": str(parsed.get("description") or f"HTTP {error.code}"),
            }
        except Exception as error:  # сеть: это тоже отказ старта
            results[label] = {"ok": False, "description": f"network: {error}"}

    owner = threading.Thread(target=long_poll, args=("A", 8))
    owner.start()
    time.sleep(2)

    overlap = threading.Thread(target=long_poll, args=("B", 0))
    overlap.start()
    owner.join(timeout=35)
    overlap.join(timeout=20)

    outcome = results.get("B") or results.get("A")

    if not outcome:
        _fail(f"не удалось проверить занятость бота @{bot_username}")

    description = outcome["description"]
    lowered = description.lower()

    if not outcome["ok"] and ("terminated by other" in lowered or "conflict" in lowered):
        _fail(
            f"бот @{bot_username} уже опрашивается другим процессом "
            "(Telegram отвечает 409). Это ожидаемо для @test_tk_1_bot: его "
            "держит LaunchAgent com.local.telegramphotobot. Остановите его "
            "(launchctl unload ~/Library/LaunchAgents/com.local.telegramphotobot.plist) "
            "или заведите отдельного тестового бота."
        )

    if "network:" in description:
        _fail(
            f"нет связи с Telegram при проверке бота @{bot_username}: {description}"
        )


def assert_ready_to_poll(config: dict) -> str:
    """Полная предстартовая последовательность для режимов с Telegram."""
    purge_inherited_env()
    env = environment(config["port"], config, config["mode"])
    assert_local_only(env)
    assert_not_production(env, config)

    username = check_token_identity(config["token"])
    assert_single_poller(config["token"], username)

    return username


# ============================================================
# СТЕНД
# ============================================================

def purge_inherited_env() -> list:
    """Снимает с окружения всё продовое до импорта модулей бота."""
    removed = []

    for name in list(os.environ):
        if name.startswith(INHERITED_PREFIXES):
            os.environ.pop(name, None)
            removed.append(name)

    return removed


def apply_environment(port: int, config: dict, mode: str) -> dict:
    """Проверяет и ставит окружение бота. Возвращает проверенные имена."""
    env = environment(port, config, mode)
    checked = assert_local_only(env)
    assert_not_production(env, config)
    os.environ.update(env)

    return {"env": env, "checked": checked}


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)

        return probe.connect_ex(("127.0.0.1", port)) != 0


def start_stub(port: int, log_path: Path):
    """Поднимает стенд отдельным процессом (его легко погасить)."""
    import subprocess

    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "live_stub.py"),
            "--port",
            str(port),
            "--log",
            str(log_path),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    deadline = time.time() + 15

    while time.time() < deadline:
        if not port_free(port):
            return process

        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            _fail(f"стенд не поднялся:\n{output}")

        time.sleep(0.2)

    process.terminate()
    _fail(f"стенд не занял порт {port} за 15 с")


def stub_snapshot(port: int) -> dict:
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/__dump", timeout=10
    ) as response:
        return json.load(response)


# Перехваченные попытки выйти наружу: заполняет сторож сети.
BLOCKED_CALLS = []


def install_network_guard(port: int) -> list:
    """
    Разрешает процессу бота ровно два адреса: стенд и api.telegram.org.

    Патчатся `requests.sessions.Session.request` и `requests.request`:
    модули бота ходят через общий Session. Всё остальное (продовая
    Supabase, Storage, Lark Open Platform, любые вебхуки) падает с
    понятной ошибкой — это и есть гарантия, что прод не тронут.
    """
    import requests

    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", TELEGRAM_HOST}
    original_request = requests.sessions.Session.request

    def check(url):
        parsed = urlparse(str(url))
        netloc = (parsed.netloc or "").lower()

        if netloc in allowed:
            return

        BLOCKED_CALLS.append(str(url))
        raise RuntimeError(
            f"LIVE-TEST BLOCKED network call: {url} "
            f"(разрешены только {sorted(allowed)})"
        )

    def guarded_request(self, method, url, *args, **kwargs):
        check(url)

        return original_request(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = guarded_request
    requests.request = lambda method, url, **kwargs: guarded_request(
        requests.sessions.Session(), method, url, **kwargs
    )

    return BLOCKED_CALLS


def patch_bot_isolation() -> None:
    """
    Отключает всё, что выходит наружу помимо `requests`.

    Часть модулей связывает `upload_image`/`TARGET_HOOK_URL` на импорте
    (`pending_photos.py`), поэтому мало подменить один `lark_media`: надо
    обойти уже импортированные ссылки. Дополнительно глушится выдача
    tenant-токена — по ней видно, что квота Lark не тратится даже при
    неожиданном пути вызова.
    """
    import lark_hooks
    import lark_media

    base = os.environ.get("SUPABASE_URL", "")
    hook = f"{base}/hook/livetest" if base else ""

    def no_lark_upload(_path):
        raise RuntimeError(
            "Lark im/v1/images отключён в живом тесте: это единственный "
            "вызов, расходующий квоту 10 000/мес."
        )

    lark_media.upload_image = no_lark_upload

    if hook:
        lark_hooks.TARGET_HOOK_URL = hook
        lark_hooks.DEFAULT_TARGET_HOOK_URL = hook

        import pending_photos

        pending_photos.upload_image = no_lark_upload
        pending_photos.TARGET_HOOK_URL = hook
        pending_photos.DEFAULT_TARGET_HOOK_URL = hook

        # shift_report/analytics держат ссылку на константу pending_photos.
        for module_name in ("shift_report", "analytics"):
            module = sys.modules.get(module_name)

            if module is not None and hasattr(module, "TARGET_HOOK_URL"):
                module.TARGET_HOOK_URL = hook

    import getToken

    getToken.get_tenant_access_token = lambda: None


# ============================================================
# РЕЖИМЫ
# ============================================================

def mode_check(config: dict) -> int:
    """Проверка конфига: те же отказы, что при живом старте, но без запуска."""
    username = assert_ready_to_poll(config)

    print("Проверки пройдены:")
    print(f"  токен:            @{username} (id {config['token'].split(':')[0]})")
    print(f"  чаты (allowlist): {config['chat_ids']}")
    print(f"  топики:           {config['listen_topics'] or '(весь разрешённый чат)'}")
    print(f"  режим:            {config['mode']}")
    print(f"  локальные адреса: {', '.join(LOCAL_ENDPOINTS)}")
    print("  опрашивающий:     свободен (два длинных getUpdates не конфликтуют)")

    if username != EXPECTED_TEST_BOT:
        print()
        print(
            f"  ВНИМАНИЕ: ожидался тестовый бот @{EXPECTED_TEST_BOT}, "
            f"а токен принадлежит @{username}."
        )

    return 0


def discover_updates(token: str, seconds: int = 20):
    """
    Одноразовый getUpdates напрямую через urllib.

    Намеренно без импорта `telegram_api`: тот читает токен на импорте, а
    этому режиму стенд не нужен вовсе.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://{TELEGRAM_HOST}/bot{token}/getUpdates",
        data=json.dumps(
            {"timeout": seconds, "limit": 100, "allowed_updates": ["message"]}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=seconds + 20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read()

        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"ok": False, "description": f"HTTP {error.code}"}
    except Exception as error:
        _fail(f"getUpdates не удался: {error}")

    if not payload.get("ok"):
        _fail(f"Telegram отклонил getUpdates: {payload.get('description')}")

    return payload.get("result") or []


def mode_discover(config: dict) -> int:
    """
    Одноразовый опрос Telegram ради chat_id/топиков.

    Ничего не сохраняет и не отвечает: только печатает, что видно.
    """
    print(
        "Слушаю Telegram до 20 с одним getUpdates.\n"
        "Напишите что-нибудь в тестовой группе (например /id) — покажу chat_id "
        "и message_thread_id.\n"
        "ВАЖНО: пока идёт этот режим, бот должен быть свободен от другого "
        "опрашивающего."
    )

    updates = discover_updates(config["token"], 20)

    if not updates:
        print("Апдейтов нет. Проверьте, что сообщение отправлено в нужную группу.")
        return 0

    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        print(
            json.dumps(
                {
                    "chat_id": chat.get("id"),
                    "chat_title": chat.get("title"),
                    "chat_type": chat.get("type"),
                    "message_thread_id": message.get("message_thread_id"),
                    "from_id": sender.get("id"),
                    "from_username": sender.get("username"),
                    "text": (message.get("text") or "")[:60],
                },
                ensure_ascii=False,
            )
        )

    print(
        "\nВпишите chat_id в TELEGRAM_ALLOWED_CHAT_IDS, "
        "а id топика — в TELEGRAM_LISTEN_TOPICS."
    )

    return 0


def _poll_loop(tg, bot, allowed_chat_ids, stop: threading.Event) -> None:
    """Опрос Telegram только для разрешённых чатов."""
    offset = None

    while not stop.is_set():
        try:
            updates = tg.get_updates(offset=offset, timeout=20)
        except Exception as error:
            print(f"[live-test] опрос упал: {error}", flush=True)
            time.sleep(3)
            continue

        if updates is None:
            time.sleep(2)
            continue

        for update in updates:
            update_id = update.get("update_id")

            try:
                message = update.get("message") or update.get("edited_message") or {}
                callback = update.get("callback_query") or {}
                chat_id = (message.get("chat") or {}).get("id")

                if chat_id is None:
                    chat_id = (
                        (callback.get("message") or {}).get("chat") or {}
                    ).get("id")

                if chat_id not in allowed_chat_ids:
                    print(
                        f"[live-test] пропуск апдейта из чужого чата {chat_id}",
                        flush=True,
                    )
                else:
                    bot.handle_update(update, bot.BOT_USERNAME)
            except Exception:
                import traceback

                traceback.print_exc()
            finally:
                if update_id is not None:
                    offset = update_id + 1


def mode_runner(config: dict, mode: str, port: int, username: str = None) -> int:
    """
    Живой (dry-run) или полный прогон на локальном стенде.

    Порядок важен: сначала снять продовое окружение, потом поставить своё и
    закрыть сеть, и только затем импортировать модули бота (они читают
    конфиг на импорте). `username` уже проверен `assert_ready_to_poll`.
    """
    purge_inherited_env()
    install_network_guard(port)
    apply_environment(port, config, mode)
    patch_bot_isolation()

    import telegram_api as tg
    import telegram_bot as bot

    username = username or bot.BOT_USERNAME or check_token_identity(config["token"])
    bot.BOT_USERNAME = username
    tg.set_my_commands(
        [
            {"command": "id", "description": "Show chat/topic IDs"},
            {"command": "whoami", "description": "Show the current link"},
            {"command": "help", "description": "Help"},
            {"command": "tree", "description": "Intake tree editor"},
        ]
    )

    allowed = {
        int(part) for part in config["chat_ids"].replace(" ", "").split(",") if part
    }

    print()
    print(f"Живой тест запущен: @{username}")
    print(f"  режим:        {mode}")
    print(f"  чаты:         {sorted(allowed)}")
    print(f"  топики:       {config['listen_topics'] or '(весь разрешённый чат)'}")
    print(f"  стенд:        http://127.0.0.1:{port}")
    print(f"  наружу можно: https://{TELEGRAM_HOST} (long polling)")
    print("  всё остальное блокируется и попадает в отчёт")
    print("  Ctrl+C — остановить и напечатать отчёт.")
    print()

    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    threading.Thread(
        target=_poll_loop,
        args=(tg, bot, allowed, stop),
        name="live-test-polling",
        daemon=True,
    ).start()

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        print("\nОстанавливаюсь, собираю отчёт со стенда…")

        try:
            dump = stub_snapshot(port)
        except Exception as error:
            dump = {"error": str(error)}

        report = ROOT / "logs" / "live-test-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                {
                    "bot": username,
                    "mode": mode,
                    "chat_ids": sorted(allowed),
                    "blocked_network_calls": list(BLOCKED_CALLS),
                    "stub": dump,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(f"Отчёт: {report}")
        print(render_summary(dump))

        if BLOCKED_CALLS:
            print(f"\nВНИМАНИЕ: перехвачено попыток уйти наружу — {len(BLOCKED_CALLS)}")

    return 0


def render_summary(dump: dict) -> str:
    """Человекочитаемый итог прогона вместо сырого JSON."""
    if "error" in dump:
        return f"Стенд недоступен: {dump['error']}"

    lines = ["", "── Что записал бот (на стенде, не в проде) ──"]
    tables = dump.get("tables") or {}

    for table in sorted(tables):
        rows = tables[table]

        if rows:
            lines.append(f"  {table}: {len(rows)} строк")

    lark = dump.get("lark") or []
    lines.append(f"  карточек/текста в «Lark»: {len(lark)}")
    lines.append(f"  объектов в Storage: {len(dump.get('storage_objects') or [])}")

    if lark:
        lines.append("")
        lines.append("  Последние карточки:")

        for entry in lark[-3:]:
            payload = entry.get("payload") or {}
            kind = payload.get("msg_type")
            preview = ""

            if kind == "text":
                preview = (payload.get("content") or {}).get("text", "")
            elif kind == "interactive":
                header = (payload.get("card") or {}).get("header") or {}
                preview = (header.get("title") or {}).get("content", "")

            lines.append(f"    [{kind}] {' '.join(str(preview).split())[:120]}")

    return "\n".join(lines)


# ============================================================
# SELFTEST (без сети и без токена)
# ============================================================

def mode_selftest(port: int) -> int:
    """
    Проверяет сам стенд: поднимает, пишет, читает, сбои, Lark-заглушку.

    Про Telegram ничего не знает: нужен, чтобы отличать «сломан стенд» от
    «сломан бот» во время живого прогона.
    """
    import urllib.error
    import urllib.request

    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    started = start_stub(port, ROOT / "logs" / "live-selftest.jsonl")
    base = f"http://127.0.0.1:{port}"
    checks = []

    def request(method, path, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            base + path,
            data=data,
            headers=headers or {"Content-Type": "application/json"},
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read()

                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            body = error.read()
            parsed = None

            try:
                parsed = json.loads(body) if body else None
            except ValueError:
                parsed = None

            return error.code, parsed

    try:
        status, rows = request("GET", "/rest/v1/issue_templates?select=*&limit=5")
        checks.append(
            ("GET issue_templates", status == 200 and len(rows or []) == 3, status)
        )

        status, _ = request(
            "POST",
            "/rest/v1/exceptions_glpc",
            {"uniq_key": "u1", "warehouse": "GLP-C"},
        )
        checks.append(("POST exceptions_glpc -> 201", status == 201, status))

        status, _ = request(
            "POST",
            "/rest/v1/exceptions_glpc",
            {"uniq_key": "u1", "warehouse": "GLP-C"},
            {
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates",
            },
        )
        checks.append(("POST дубль по uniq_key -> 409", status == 409, status))

        status, rows = request("GET", "/rest/v1/exceptions_glpc?select=*")
        checks.append(
            ("в таблице ровно 1 строка", len(rows or []) == 1, len(rows or []))
        )

        status, _ = request("POST", "/storage/v1/object/live-test-photos/p.jpg", None)
        checks.append(("Storage upload", status in (200, 201), status))

        status, _ = request(
            "POST",
            "/hook/livetest",
            {"msg_type": "text", "content": {"text": "hello"}},
        )
        checks.append(("Lark-заглушка отвечает 200", status == 200, status))

        status, body = request("GET", "/__dump")
        checks.append(
            (
                "дамп видит карточку в Lark",
                status == 200 and len((body or {}).get("lark") or []) == 1,
                len((body or {}).get("lark") or []),
            )
        )

        request("POST", "/__fault", {"action": "status", "value": 500})
        status, _ = request("GET", "/rest/v1/issue_templates")
        checks.append(("управляемый сбой 500", status == 500, status))

        request("POST", "/__fault", {"action": "fault_off"})
        status, _ = request("GET", "/rest/v1/issue_templates")
        checks.append(("после снятия сбоя снова 200", status == 200, status))
    finally:
        started.terminate()
        started.wait(timeout=10)

    print("Проверка стенда:")
    failed = 0

    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} — {name} ({detail})")
        failed += 0 if ok else 1

    print(f"\nИТОГО: {len(checks) - failed}/{len(checks)} проверок стенда пройдено")

    return 1 if failed else 0


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Живой тест бота через Telegram на локальном стенде",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "mode",
        choices=("check", "discover", "dry-run", "live", "selftest"),
        nargs="?",
        default="check",
    )
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    (ROOT / "logs").mkdir(parents=True, exist_ok=True)

    if args.mode == "selftest":
        return mode_selftest(args.port or DEFAULT_PORT)

    config = load_config()
    port = args.port or config["port"]

    if args.mode == "check":
        return mode_check(config)

    if args.mode == "discover":
        purge_inherited_env()
        env = environment(port, config, "dry-run")
        assert_local_only(env)
        assert_not_production(env, config)
        check_token_identity(config["token"])

        return mode_discover(config)

    # live/dry-run: сначала убеждаемся, что бот свободен, и только потом
    # поднимаем стенд — иначе владелец получил бы запущенный стенд и отказ.
    username = assert_ready_to_poll(config)

    if not port_free(port):
        _fail(f"порт {port} занят другим процессом")

    started = start_stub(port, ROOT / "logs" / f"live-{args.mode}.jsonl")

    try:
        return mode_runner(config, args.mode, port, username)
    finally:
        started.terminate()

        try:
            started.wait(timeout=10)
        except Exception:
            started.kill()

        print("Стенд остановлен.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveTestError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
