# -*- coding: utf-8 -*-
"""
Локальный запуск ТОЛЬКО HTTP-ручек бота — без опроса Telegram.

**Зачем отдельный файл.** `telegram_bot.main()` делает три вещи: берёт лиз,
запускает супервизор поллера и поднимает Flask. Для локальной проверки дерева
приёма (`GET /intake/tree`) опрос Telegram **не нужен и вреден**: в
`LarkBot/.env` лежит **боевой** `TELEGRAM_BOT_TOKEN`, и второй поллер начал бы
отбирать апдейты у прод-бота — операторы перестали бы получать ответы.

Поэтому здесь импортируется модуль и вызывается **только** `app.run()`, минуя
`main()`. Лиз и поллер не трогаются вовсе.

Запуск:
    cd ~/Desktop/LarkBot && python3 scripts/serve_http_only.py [порт]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PORT", "7777"))

    # Явная проверка: ручка должна быть включена токеном из `.env`, иначе
    # сервер поднимется, но `/intake/tree` будет отвечать 404 — и это будет
    # выглядеть как дефект кода, а не как незаданная переменная.
    if not telegram_bot.INTAKE_VIEW_TOKEN:
        print("ВНИМАНИЕ: INTAKE_VIEW_TOKEN пуст — /intake/tree будет отвечать 404")

    print(f"HTTP-only сервер бота на http://127.0.0.1:{port} (без опроса Telegram)")
    # `use_reloader=False`: перезагрузчик запускает второй процесс и дублировал бы
    # сервер; нам это не нужно.
    telegram_bot.app.run(host="127.0.0.1", port=port, use_reloader=False)
