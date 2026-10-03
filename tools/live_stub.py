"""
Локальный «стенд» для живого теста бота через Telegram.

Одна HTTP-заглушка заменяет сразу три боевых сервиса, чтобы живой прогон
не тронул ни продовую базу Supabase, ни Lark-вебхуки (лимит 10 000/мес):

    /rest/v1/*      — PostgREST (таблицы бота) в памяти процесса;
    /storage/v1/*   — Supabase Storage (фото, offset, маркеры отчётов);
    /hook/*         — заглушка Lark-вебхука (карточки печатаются в консоль);
    /__dump         — то, что бот записал (для проверки после прогона);
    /__fault        — управление сбоями: read-only, 500, недоступность.

Всё живёт только в памяти: после остановки процесса стенда данных не
остаётся нигде. Ни одного запроса наружу отсюда не уходит.

Запуск (обычно это делает tools/live_test_bot.py сам):

    python3 tools/live_stub.py --port 8799 --log logs/live-stub.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:  # Python 3.9 + zoneinfo: на slim-образах базы часовых поясов нет.
    from zoneinfo import ZoneInfo

    WARSAW = ZoneInfo("Europe/Warsaw")
except Exception:  # pragma: no cover - на Mac база часовых поясов есть
    WARSAW = timezone.utc


# ============================================================
# ХРАНИЛИЩЕ
# ============================================================

MAX_ROWS = 1000


def _defaults() -> dict:
    """
    Пустая, но рабочая база тестового склада.

    Значения подобраны так, чтобы прошли оба пути приёма: свободный текст
    ошибки (`<тип>: <описание>. <номер>`) и дерево решений с фото.
    """
    return {
        # Справочники для свободного текста.
        "issue_templates": [
            {
                "id": 1,
                "employee_title": "Security module failure",
                "issue_type": "Drive",
                "issue_sub_type": "Security module failure",
                "issue_description": "Unable to drive: security module failure",
                "recovery_title": "Restart the safety module",
                "solving_time": 15,
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "employee_title": "Lift module error",
                "issue_type": "Lifting",
                "issue_sub_type": "Lift module",
                "issue_description": "Lifting module reports an error",
                "recovery_title": "Reseat the tray",
                "solving_time": 20,
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 3,
                "employee_title": "Robot stopped unexpectedly",
                "issue_type": "Chassis",
                "issue_sub_type": "Chassis",
                "issue_description": "Robot stopped unexpectedly",
                "recovery_title": "Check the chassis",
                "solving_time": 10,
                "created_at": "2026-01-01T00:00:00Z",
            },
        ],
        "warehouses": [
            {"id": 1, "title": "GLP-C"},
            {"id": 2, "title": "SMALL-P3"},
        ],
        "robots_maintenance_list": [
            {
                "id": 1,
                "robot_number": "3780",
                "robot_type": "A42T",
                "status": "online",
                "type_problem": None,
                "problem_note": None,
                "warehouse": "GLP-C",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "robot_number": "3490",
                "robot_type": "A42T",
                "status": "online",
                "type_problem": None,
                "problem_note": None,
                "warehouse": "SMALL-P3",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        ],
        "employees": [
            {
                "id": 1,
                "user_name": "Test Worker",
                "card_id": "CARD-1",
                "home_warehouse": "GLP-C",
                "is_leader": True,
            }
        ],
        "equipment": [],
        "equipment_types": [],
        "telegram_users": [],
        "change_status_robots": [],
        "robots_to_add": [],
        "exceptions": [],
        "exceptions_glpc": [],
        "telegram_equipment_reports": [],
        "telegram_devices_to_add": [],
        "telegram_intake_options": [],
        "bot_leases": [],
    }


class Store:
    """Таблицы в памяти + счётчики id и уникальные ключи (как в SQL)."""

    # Уникальные ограничения из sql/: повторная вставка даёт 409.
    UNIQUE = {
        "exceptions_glpc": ("uniq_key",),
        "telegram_equipment_reports": ("telegram_chat_id", "source_message_id"),
        "telegram_devices_to_add": (
            "category",
            "device_type",
            "device_number",
            "warehouse",
        ),
        "telegram_intake_options": ("node_id", "option_id"),
        "telegram_users": ("telegram_id",),
        "bot_leases": ("name",),
    }

    def __init__(self, seed: dict = None):
        self.lock = threading.Lock()
        self.tables = _defaults()

        if seed:
            for name, rows in seed.items():
                self.tables[name] = rows

        self.writes = []          # журнал всего, что бот записал
        self.lark = []            # журнал карточек/текста, ушедших «в Lark»
        self.storage = {}         # bucket/object -> bytes (в памяти)
        self.buckets = set()
        self.fault = {"read_only": False, "status": None}
        self._ids = {}

    # ---------- журнал ----------

    def record(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.writes.append(
                {
                    "at": _now(),
                    "kind": kind,
                    **payload,
                }
            )

    def record_lark(self, path: str, payload: dict) -> None:
        with self.lock:
            self.lark.append({"at": _now(), "path": path, "payload": payload})

    # ---------- строки ----------

    def next_id(self, table: str) -> int:
        with self.lock:
            existing = [
                int(row.get("id") or 0) for row in self.tables.get(table, [])
            ]
            current = max([self._ids.get(table, 0)] + existing) + 1
            self._ids[table] = current

            return current

    def reset_logs(self) -> None:
        """Очищает журналы, не трогая записанные строки."""
        with self.lock:
            self.writes = []
            self.lark = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "tables": {
                    name: rows for name, rows in self.tables.items() if rows
                },
                "writes": list(self.writes),
                "lark": list(self.lark),
                "storage_objects": sorted(self.storage),
                "buckets": sorted(self.buckets),
                "fault": dict(self.fault),
            }


def _now() -> str:
    return datetime.now(WARSAW).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# ФИЛЬТРЫ PostgREST
# ============================================================

_OPERATORS = ("eq.", "neq.", "gt.", "gte.", "lt.", "lte.", "like.", "ilike.", "in.", "is.")


def _row_matches(row: dict, filters: dict) -> bool:
    """Поддерживает операторы, которые реально использует код бота."""
    for column, raw in filters.items():
        if column in ("select", "order", "limit", "offset", "on_conflict"):
            continue

        value = row.get(column)

        for operator in _OPERATORS:
            if not str(raw).startswith(operator):
                continue

            wanted = str(raw)[len(operator):]

            if operator == "eq.":
                if str(value) != wanted and str(value) != wanted.strip():
                    return False
            elif operator == "neq.":
                if str(value) == wanted:
                    return False
            elif operator in ("gt.", "gte.", "lt.", "lte."):
                try:
                    left, right = float(value), float(wanted)
                except (TypeError, ValueError):
                    return False
                if operator == "gt." and not left > right:
                    return False
                if operator == "gte." and not left >= right:
                    return False
                if operator == "lt." and not left < right:
                    return False
                if operator == "lte." and not left <= right:
                    return False
            elif operator == "is.":
                # PostgREST: is.null / is.true / is.false
                if wanted == "null" and value is not None:
                    return False
                if wanted == "true" and value is not True:
                    return False
                if wanted == "false" and value is not False:
                    return False
            elif operator in ("like.", "ilike."):
                pattern = "^" + re.escape(wanted).replace(r"\%", ".*").replace("%", ".*") + "$"
                flags = re.IGNORECASE if operator == "ilike." else 0
                if not re.match(pattern, str(value or ""), flags):
                    return False
            elif operator == "in.":
                wanted_items = [part.strip().strip('"') for part in wanted.strip("()").split(",")]
                if str(value) not in wanted_items:
                    return False

            break
        else:
            # Без оператора PostgREST сравнивает на равенство.
            if str(value) != str(raw):
                return False

    return True


def _apply_order(rows: list, order: str) -> list:
    if not order:
        return rows

    parts = [part.strip() for part in str(order).split(",") if part.strip()]
    result = list(rows)

    for part in reversed(parts):
        column, _, direction = part.partition(".")
        descending = direction.strip().lower().startswith("desc")

        def sort_key(row, column=column):
            value = row.get(column)
            return (value is None, str(value))

        result.sort(key=sort_key, reverse=descending)

    return result


def _project(row: dict, select: str) -> dict:
    if not select or select.strip() == "*":
        return dict(row)

    wanted = [part.strip() for part in select.split(",") if part.strip()]

    return {key: row.get(key) for key in wanted}


# ============================================================
# HTTP
# ============================================================

CONTROL_ACTIONS = {"read_only", "status", "clear", "seed", "fault_off"}


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    log_path: str = None
    state = {"requests": 0}

    # ---------- утилиты ----------

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)

        return self.rfile.read(length) if length else b""

    def _json_body(self):
        raw = self._body()

        if not raw:
            return None

        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _send(self, status: int, payload, headers: dict = None) -> None:
        if payload is None:
            body = b""
        elif isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(status)

        if body and not isinstance(payload, (bytes, bytearray)):
            self.send_header("Content-Type", "application/json")

        self.send_header("Content-Length", str(len(body)))

        for name, value in (headers or {}).items():
            self.send_header(name, value)

        self.end_headers()

        if body:
            self.wfile.write(body)

    def _log_line(self, entry: dict) -> None:
        if not self.log_path:
            return

        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _fault_response(self, method: str = None):
        """
        Сбой, который надо отдать на этот запрос.

        `status=500` валит всё. Режим read_only валит только изменяющие
        методы: иначе проверка «база недоступна на записи» превратилась бы
        в «база недоступна вообще» — это другой сценарий.
        """
        if self.store.fault.get("status"):
            return int(self.store.fault["status"])

        if self.store.fault.get("read_only") and method in (
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
        ):
            return 403

        return None

    def _split_path(self):
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]

        return parsed, parts, parse_qs(parsed.query)

    # ---------- маршруты ----------

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_PATCH(self):  # noqa: N802
        self._route("PATCH")

    def do_PUT(self):  # noqa: N802
        self._route("PUT")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")

    def _route(self, method: str) -> None:
        parsed, parts, query = self._split_path()
        self.state["requests"] += 1

        try:
            if parts[:1] == ["rest"] and parts[1:2] == ["v1"]:
                self._rest(method, parts[2:], query)
            elif parts[:1] == ["storage"] and parts[1:2] == ["v1"]:
                self._storage(method, parts[2:], query)
            elif parts[:1] == ["hook"]:
                self._hook(method, parts[1:])
            elif parts[:1] == ["__dump"]:
                self._send(200, self.store.snapshot())
            elif parts[:1] == ["__fault"]:
                self._fault(method)
            elif parts[:1] == ["__health"]:
                self._send(200, {"ok": True, "requests": self.state["requests"]})
            else:
                self._send(404, {"error": "unknown path", "path": parsed.path})
        except BrokenPipeError:
            pass
        except Exception as error:  # pragma: no cover - защитная сетка
            self._send(500, {"error": f"{type(error).__name__}: {error}"})

    # ---------- PostgREST ----------

    def _rest(self, method: str, parts: list, query: dict) -> None:
        if not parts:
            return self._send(404, {"error": "no table"})

        table = parts[0]
        row_id = parts[1] if len(parts) > 1 else None
        fault = self._fault_response(method)

        if fault:
            return self._send(fault, {"message": "injected fault"})

        store = self.store
        filters = {key: values[-1] for key, values in query.items()}
        select = filters.pop("select", "*")
        order = filters.pop("order", "")
        limit = int(filters.pop("limit", MAX_ROWS) or MAX_ROWS)
        offset = int(filters.pop("offset", 0) or 0)

        with store.lock:
            rows = list(store.tables.setdefault(table, []))

        if method in ("GET", "HEAD"):
            matched = [row for row in rows if _row_matches(row, filters)]

            if row_id:
                matched = [row for row in matched if str(row.get("id")) == str(row_id)]

            matched = _apply_order(matched, order)
            total = len(matched)
            page = matched[offset: offset + limit]
            content_range = f"{offset}-{offset + max(len(page) - 1, 0)}/{total}"

            return self._send(
                200,
                [_project(row, select) for row in page],
                {"Content-Range": content_range},
            )

        if method == "POST":
            payload = self._json_body()

            if payload is None:
                return self._send(400, {"message": "empty body"})

            items = payload if isinstance(payload, list) else [payload]
            prefer = str(self.headers.get("Prefer") or "")
            ignore_duplicates = "ignore-duplicates" in prefer
            merge = "merge-duplicates" in prefer
            created = []

            for item in items:
                row = dict(item)

                if row_id and method == "POST":
                    row.setdefault("id", row_id)
                elif "id" not in row:
                    row["id"] = store.next_id(table)

                # Уникальные ограничения — как в боевой схеме.
                clash = None
                for columns in [store.UNIQUE.get(table, ())]:
                    if not columns or not all(row.get(c) is not None for c in columns):
                        continue
                    with store.lock:
                        for existing in store.tables.get(table, []):
                            if all(str(existing.get(c)) == str(row.get(c)) for c in columns):
                                clash = existing
                                break

                if clash is not None:
                    if ignore_duplicates:
                        store.record(
                            "conflict-ignored",
                            {"table": table, "row": row},
                        )
                        return self._send(409, {"message": "duplicate key value"})
                    if merge:
                        with store.lock:
                            clash.update(row)
                        created.append(clash)
                        continue

                with store.lock:
                    store.tables.setdefault(table, []).append(row)

                store.record("insert", {"table": table, "row": row})
                created.append(row)

            return self._send(
                201,
                [_project(row, select) for row in created],
                {"Preference-Applied": "return=representation"},
            )

        if method == "PATCH":
            payload = self._json_body() or {}
            updated = []

            with store.lock:
                for row in store.tables.setdefault(table, []):
                    if row_id and str(row.get("id")) != str(row_id):
                        continue
                    if not _row_matches(row, filters):
                        continue
                    row.update(payload)
                    updated.append(dict(row))

            store.record("patch", {"table": table, "params": filters, "payload": payload})

            return self._send(200, [_project(row, select) for row in updated])

        if method == "DELETE":
            removed = []

            with store.lock:
                for row in list(store.tables.setdefault(table, [])):
                    if row_id and str(row.get("id")) != str(row_id):
                        continue
                    if not _row_matches(row, filters):
                        continue
                    store.tables[table].remove(row)
                    removed.append(dict(row))

            store.record("delete", {"table": table, "params": filters, "removed": removed})

            return self._send(204, None)

        return self._send(405, {"error": f"method {method} not supported"})

    # ---------- Storage ----------

    def _storage(self, method: str, parts: list, query: dict) -> None:
        store = self.store
        fault = self._fault_response(method)

        if fault:
            return self._send(fault, {"message": "injected fault"})

        # /storage/v1/bucket, /storage/v1/object/<bucket>/<name>, /object/sign/...
        if parts[:1] == ["bucket"]:
            if method == "POST":
                payload = self._json_body() or {}
                name = str(payload.get("name") or payload.get("id") or "")
                if name in store.buckets:
                    return self._send(
                        400,
                        {
                            "statusCode": "409",
                            "error": "Duplicate",
                            "message": "The resource already exists",
                            "code": "BucketAlreadyExists",
                        },
                    )
                store.buckets.add(name)
                store.record("bucket-create", {"bucket": name, "payload": payload})
                return self._send(200, {"name": name})

            if method == "PUT":
                name = parts[1] if len(parts) > 1 else ""
                store.buckets.add(name)
                store.record("bucket-update", {"bucket": name})
                return self._send(200, {})

            if method == "GET":
                return self._send(
                    200, [{"name": name} for name in sorted(store.buckets)]
                )

        if parts[:1] == ["object"]:
            # object/sign/<bucket>/<name> | object/public/... | object/<bucket>/<name>
            rest = parts[1:]

            if rest[:1] == ["sign"]:
                bucket, name = _bucket_and_name(rest[1:])
                key = f"{bucket}/{name}"
                if key not in store.storage:
                    return self._send(
                        400,
                        {"error": "not_found", "message": "Object not found"},
                    )
                return self._send(
                    200,
                    {
                        "signedURL": (
                            f"/object/sign/{bucket}/{name}?token=live-test-token"
                        )
                    },
                )

            if rest[:1] == ["public"]:
                bucket, name = _bucket_and_name(rest[1:])
                key = f"{bucket}/{name}"
                if key not in store.storage:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, store.storage[key])

            bucket, name = _bucket_and_name(rest)

            if method == "POST":
                content = self._body()
                store.storage[f"{bucket}/{name}"] = content
                store.record(
                    "storage-upload",
                    {"bucket": bucket, "name": name, "bytes": len(content)},
                )
                return self._send(200, {"Key": f"{bucket}/{name}"})

            if method == "GET":
                key = f"{bucket}/{name}"
                if key not in store.storage:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, store.storage[key])

            if method == "DELETE":
                store.storage.pop(f"{bucket}/{name}", None)
                return self._send(200, {})

        return self._send(404, {"error": "unknown storage path", "parts": parts})

    # ---------- Lark ----------

    def _hook(self, method: str, parts: list) -> None:
        if method != "POST":
            return self._send(405, {"error": "POST only"})

        payload = self._json_body() or {}
        target = parts[0] if parts else "default"
        self.store.record_lark(target, payload)

        kind = payload.get("msg_type")

        if kind == "text":
            preview = (payload.get("content") or {}).get("text", "")
        elif kind == "interactive":
            header = ((payload.get("card") or {}).get("header") or {})
            preview = (header.get("title") or {}).get("content", "")
        else:
            preview = json.dumps(payload, ensure_ascii=False)

        print(
            f"\n[LARK-STUB] -> /hook/{target}  {kind}\n"
            f"  {' '.join(str(preview).split())[:400]}",
            flush=True,
        )
        self._log_line({"kind": "lark", "target": target, "payload": payload})

        # Именно «code: 0» считает успехом lark_media.hook_ok.
        return self._send(200, {"code": 0, "msg": "success"})

    # ---------- Управление сбоями ----------

    def _fault(self, method: str) -> None:
        if method == "GET":
            return self._send(200, self.store.snapshot()["fault"])

        payload = self._json_body() or {}
        action = str(payload.get("action") or "")

        if action == "read_only":
            # Отдельный флаг, не общий status: иначе режим «только чтение»
            # заблокировал бы и чтение.
            self.store.fault["read_only"] = bool(payload.get("value", True))
        elif action == "status":
            value = payload.get("value")
            self.store.fault["status"] = int(value) if value else None
        elif action == "fault_off":
            self.store.fault = {"read_only": False, "status": None}
        elif action == "clear":
            self.store.reset_logs()
        else:
            return self._send(400, {"error": f"unknown action {action!r}"})

        return self._send(200, self.store.snapshot()["fault"])

    def log_message(self, *args):
        """Стандартный лог заглушки не нужен — он забивает вывод."""


def _bucket_and_name(parts: list):
    bucket = parts[0] if parts else ""
    name = "/".join(parts[1:]) if len(parts) > 1 else ""

    return bucket, name


def serve(port: int, seed: dict = None, log_path: str = None) -> ThreadingHTTPServer:
    """Поднимает стенд и возвращает сервер (не блокирует)."""
    handler = type(
        "LiveStubHandler",
        (Handler,),
        {"store": Store(seed), "log_path": log_path},
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный стенд для живого теста бота")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--log", default=None, help="JSONL-журнал запросов")
    parser.add_argument("--seed", default=None, help="JSON-файл с данными таблиц")
    parser.add_argument(
        "--print-dump-on-exit",
        action="store_true",
        help="Печатать дамп записанного при остановке",
    )
    args = parser.parse_args()

    seed = None

    if args.seed:
        with open(args.seed, encoding="utf-8") as handle:
            seed = json.load(handle)

    if args.log:
        try:
            open(args.log, "w", encoding="utf-8").close()
        except OSError as error:
            print(f"Не удалось открыть журнал {args.log}: {error}", file=sys.stderr)

    server = serve(args.port, seed, args.log)
    base = f"http://127.0.0.1:{args.port}"

    print(f"Стенд слушает {base}")
    print(f"  PostgREST: {base}/rest/v1/<таблица>")
    print(f"  Storage:   {base}/storage/v1/...")
    print(f"  Lark:      {base}/hook/<имя>  (карточки печатаются здесь)")
    print(f"  Дамп:      {base}/__dump   Сбои: {base}/__fault")
    print("Ctrl+C — остановить.")

    try:
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nОстановка стенда.")
    finally:
        server.server_close()

        if args.print_dump_on_exit:
            print(json.dumps(server.RequestHandlerClass.store.snapshot(), ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
