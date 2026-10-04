# -*- coding: utf-8 -*-
"""Изоляция конфигурации бота для тестов, не зависящая от порядка запуска.

Зачем это нужно
===============

Модули бота читают переменные окружения **в момент импорта** (например,
`sendToDataBase.SUPABASE_URL`, `bot_lease.LEASE_NAME`, `telegram_api.
TELEGRAM_BOT_TOKEN`, `telegram_bot.ALLOWED_CHAT_IDS`). Поэтому достаточно
одного импорта с «неправильным» окружением, чтобы модуль навсегда (до конца
процесса) нёс старую конфигурацию.

`unittest discover` импортирует **все** тестовые модули до запуска тестов, в
алфавитном порядке. Модуль `test_equipment_intake_production` (идёт раньше
harness) импортирует `telegram_bot` на уровне модуля — и модуль бота
фиксирует боевой конфиг из `.env`. Далее `test_live_test_harness` в
`setUpClass` выставляет свои `TEST_ENV`, но `import telegram_bot` возвращает
уже закэшированный модуль со старым конфигом. Отсюда падения: лиз называется
`glpc-bot-telegram`, а бот не отвечает в тестовом чате.

Почему не `tests/__init__.py`
=============================

`unittest discover` при указании каталога (`-s tests` или `cd tests`) не
импортирует `tests/__init__.py` как пакет — проверено. Значит, выставить
окружение «до любого импорта» через инициализатор пакета нельзя: он не
выполняется. `setUpModule`/`setUpClass` тоже срабатывают уже после импорта
всех модулей.

Почему не выбрасывание модулей из `sys.modules`
===============================================

Такой подход уже пробовали — он делает хуже. `test_report_writer` держит
ссылку на модуль `equipment_intake.report_writer` и на функции
`sendToDataBase`, полученные при импорте. После удаления модулей из
`sys.modules` повторный импорт создаёт **другой** объект модуля, и старые
ссылки указывают на «мёртвую» копию: часть наборов перестаёт получать нужные
модули (18 ошибок вместо 5).

Выбранное решение — перезагрузка на месте (`importlib.reload`)
=============================================================

`importlib.reload(module)` заново выполняет код модуля **в том же объекте** и
в той же записи `sys.modules`. Поэтому:

  * константы перечитываются из нового окружения;
  * идентичность модулей сохраняется — все, кто держит `import x as x`,
    видят обновлённые значения;
  * `from x import y` внутри перезагружаемых модулей пересобирается, если
    перезагружать в топологическом порядке (зависимости раньше зависимых).

Порядок важен: `shift_report` делает `from pending_photos import
TARGET_HOOK_URL` на импорте, `analytics` — `from shift_report import ...`,
`telegram_bot` — из всех них. Поэтому список ниже отсортирован так, чтобы
каждый модуль перезагружался после своих внутренних зависимостей.

`logging_config` намеренно НЕ перезагружается: у него есть глобальный флаг
`_configured`, и повторное выполнение навесило бы на корневой логгер вторые
хендлеры (дублирование строк и лишние файловые дескрипторы).

Модули `equipment_intake.*` тоже не перезагружаются: они читают окружение
лениво, внутри функций (`editor_role`, `load_tree`, `storage`), поэтому
подхватывают свежие значения сами.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Mapping


#: Модули бота, читающие конфигурацию на импорте, в топологическом порядке.
#: Зависимости идут раньше зависимых.
BOT_MODULES = (
    "config",
    "getToken",
    "lark_media",
    "lark_send",
    "sendToDataBase",
    "warehouses",
    "lark_hooks",
    "supabase_storage",
    "pending_photos",
    "robot_status",
    "shift_report",
    "analytics",
    "bot_lease",
    "robot_queue",
    "digests",
    "telegram_api",
    "telegram_store",
    "robot_card",
    "telegram_bot",
)


def reload_bot_modules() -> list:
    """
    Перечитывает конфигурацию модулей бота из текущего окружения.

    Возвращает имена перезагруженных модулей (для диагностики). Модуль,
    который ещё не импортировался, сначала импортируется — уже с текущим
    окружением, — а затем перезагружается; результат в обоих случаях один.

    Подмена модулей в `sys.modules`. Набор `test_editor_access` кладёт в
    `sys.modules["telegram_store"]` **самодельный** модуль (`types.ModuleType`)
    с одной функцией и без `__spec__`, и в конце он там остаётся. Такой объект
    нельзя ни перезагрузить (`ModuleNotFoundError: spec not found`), ни
    оставить: `telegram_bot` при перезагрузке делает `from telegram_store
    import StoreUnavailable, ...` и упал бы на неполной подмене. Поэтому
    подмена вытесняется, а настоящий модуль импортируется заново из файла.
    Для самого `test_editor_access` это безопасно: его проверки уже
    завершились и работали с подменой синхронно, а `editor.py` импортирует
    `telegram_store` лениво, внутри функции.
    """
    reloaded = []

    for name in BOT_MODULES:
        existing = sys.modules.get(name)

        if existing is not None and getattr(existing, "__spec__", None) is None:
            # Самодельная подмена — вытесняем и берём настоящий модуль.
            del sys.modules[name]

        module = importlib.import_module(name)
        importlib.reload(module)
        reloaded.append(name)

    return reloaded


class BotEnvIsolation:
    """
    Контекст «окружение стенда + перезагрузка модулей бота».

    `start()` запоминает текущие значения переменных, ставит тестовые и
    перезагружает модули бота; `stop()` возвращает переменные как было и
    снова перезагружает модули. Симметрия важна: следующий набор тестов
    должен увидеть ту же конфигурацию, что и до нас, — тогда результат не
    зависит от порядка запуска.

    Вложенность. В одном наборе может быть несколько классов со своими
    `setUpClass`/`tearDownClass` (в harness их два). Если каждый класс
    запоминал бы окружение «как есть», второй класс запомнил бы уже
    **тестовые** значения и на выходе оставил бы их — симметрия сломалась бы.
    Поэтому счётчик вложенности общий на процесс: истинные значения
    запоминаются при первом `start()`, а восстанавливаются при последнем
    `stop()`.
    """

    #: Глубина вложенности и исходное окружение — общие на процесс.
    _depth = 0
    _originals = None

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = {name: str(value) for name, value in env.items()}
        self._started = False

    def start(self) -> list:
        cls = type(self)
        cls._depth += 1
        self._started = True

        if cls._originals is None:
            cls._originals = {name: os.environ.get(name) for name in self._env}

        os.environ.update(self._env)

        return reload_bot_modules()

    def stop(self) -> list:
        if not self._started:
            return []

        self._started = False
        cls = type(self)
        cls._depth = max(0, cls._depth - 1)

        if cls._depth > 0:
            # Внутренний уровень: внешний контекст ещё держит окружение стенда.
            return []

        originals = cls._originals or {}
        cls._originals = None

        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

        return reload_bot_modules()
