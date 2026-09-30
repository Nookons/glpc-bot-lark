"""
Локальный «Lark» для тестов: принимает вебхуки и печатает их в консоль.

Зачем: если в .env.test оставить LARK_HOOK_* пустыми, `lark_hooks` подставит
БОЕВОЙ вебхук из DEFAULT_TARGET_HOOK_URL — и тестовые ошибки уйдут в рабочую
Lark-группу. Поэтому в тестах все вебхуки указывают сюда:

    LARK_TARGET_HOOK_URL=http://127.0.0.1:8899/hook/test
    LARK_HOOK_ERROR_GLPC=http://127.0.0.1:8899/hook/glpc-errors
    ...

Ответ {"code": 0} — именно его `lark_media.hook_ok` считает успехом,
поэтому бот ведёт себя как при настоящей отправке.

Запуск (в отдельном окне терминала):

    python3 tests/lark_sink.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899


def _summary(payload: dict) -> str:
    """Короткое человекочитаемое описание того, что прислал бот."""
    kind = payload.get("msg_type")

    if kind == "text":
        return str((payload.get("content") or {}).get("text", ""))
    if kind == "interactive":
        card = payload.get("card") or {}
        header = card.get("header") or {}
        title = (header.get("title") or {}).get("content", "")
        lines = []
        for element in card.get("elements") or []:
            text = (element.get("text") or {}).get("content")
            if text:
                lines.append(text)
        return f"[card] {title}\n" + "\n".join(lines)
    if kind in ("image", "post"):
        return f"[{kind}] {(payload.get('content') or {})}"

    return json.dumps(payload, ensure_ascii=False)[:300]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"_raw": raw.decode("utf-8", "replace")}

        stamp = datetime.now().strftime("%H:%M:%S")
        target = self.path.rstrip("/").split("/")[-1]

        print(f"\n[{stamp}] -> {target}")
        print(_summary(payload), flush=True)

        body = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Глушим стандартный лог — он забивает вывод вебхуков."""


if __name__ == "__main__":
    print(f"Lark-заглушка слушает http://127.0.0.1:{PORT}/hook/<имя>")
    print("Все вебхуки из .env.test должны указывать сюда. Ctrl+C — выход.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
