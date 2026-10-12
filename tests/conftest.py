# -*- coding: utf-8 -*-
"""Тестовое окружение, не зависящее от `.env` оператора и порядка файлов.

🔴 **Зачем этот файл (корень «известной проблемы порядка тестов»).**

Модули бота читают конфигурацию **в момент импорта**: `telegram_bot.TOPIC_RAW`,
`TELEGRAM_TOPIC_ID`, `TELEGRAM_TOPIC_NAME`, `ALLOWED_CHAT_IDS`,
`LISTEN_TOPIC_IDS`, `IGNORE_TOPIC_IDS`. При этом `config.load_dotenv()` /
`telegram_api.load_dotenv()` подмешивают рабочий `.env` **на первом импорте**
любого модуля бота.

`pytest` импортирует **все** тестовые модули на этапе сбора в алфавитном
порядке. Первым `telegram_bot` тянет `test_equipment_intake_production.py`
(идёт по алфавиту раньше), и модуль навсегда фиксирует **боевые** топики из
`.env`: `TOPIC_RAW = {'error': '2', 'error:GLPC': '2', 'error:SMALL-P3': '318'}`.
`test_telegram_offline.py` обнуляет переменные окружения **позже**, уже после
этого импорта, поэтому карта топиков остаётся боевой.

**Как это выглядело.** `_allowed_output_topics()` пересчитывается от
`TOPIC_RAW` и даёт `{2, 318}`; отправка в тестовый топик отклоняется
(«Отправка отклонена: топик None не обслуживается (chat=-500, allowed=[2,
318])»), бот молчит, и тесты падают с `IndexError: list index out of range` на
`sent[0]`. Каждый файл по отдельности проходил, поэтому падение списывали на
«проблему порядка тестов» — и тем самым **скрывали**, что команды `/offline`,
`/online` и флоу статуса в этих тестах **не проверялись вообще**.

**Решение — корневое.** `conftest.py` исполняется `pytest` **до сбора** (значит,
и до импорта любого тестового модуля). Здесь выставляется детерминированное
окружение теста, поэтому ни один модуль бота не успевает зафиксировать значения
из `.env`. `load_dotenv()` по умолчанию `override=False`, поэтому уже заданные
(пустые) переменные файлом не перетираются.

**Вторая утечка — перезагрузка модулей.** `test_live_test_harness.py` поднимает
`BotEnvIsolation`, которая перезагружает модули бота
(`bot_config_isolation.reload_bot_modules`). Перезагрузка возвращает настоящие
функции и стирает заглушки, поставленные в `test_telegram_offline.py` один раз
на импорте. Поэтому фикстура ниже перед каждым тестом заново ставит заглушки
через `_install_test_stubs()` самого тестового модуля.
"""

from __future__ import annotations

import os

import pytest


#: Детерминированное окружение тестов. Значения выставляются ДО импорта
#: тестовых модулей, поэтому модули бота читают именно их.
TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "000000:TEST-TOKEN",
    # Топики: основной, по складам и служебные ленты. Боевые значения
    # (`TELEGRAM_TOPIC_ID=2`, `TELEGRAM_TOPIC_ERROR_SP3=318`, ...) делать
    # топиками ошибок и отправки в тестах нельзя.
    "TELEGRAM_TOPIC_ID": "",
    "TELEGRAM_TOPIC_NAME": "",
    "TELEGRAM_TOPIC_ERROR_GLPC": "",
    "TELEGRAM_TOPIC_ERROR_SP3": "",
    "TELEGRAM_TOPIC_STATUS": "",
    "TELEGRAM_TOPIC_STATS": "",
    "TELEGRAM_TOPIC_SERVICE": "",
    # Белый список чатов и списки топиков: «фильтра нет».
    "TELEGRAM_ALLOWED_CHAT_IDS": "",
    "TELEGRAM_LISTEN_TOPICS": "",
    "TELEGRAM_IGNORE_TOPICS": "",
    # Подсказка «чужой топик» по умолчанию выключена; тесты включают её сами.
    "TELEGRAM_WRONG_TOPIC_HINT": "",
}


def _apply_test_env() -> None:
    """Выставляет окружение теста. Идемпотентно."""
    os.environ.update(TEST_ENV)


# Применяем **на импорте conftest**, то есть раньше любого тестового модуля.
_apply_test_env()


@pytest.fixture(autouse=True)
def _reset_offline_bot_state(request):
    """Готовит детерминированное состояние офлайн-набора перед каждым тестом.

    **Зачем.** `test_live_test_harness` поднимает `BotEnvIsolation`, которая
    перезагружает модули бота (`bot_config_isolation.reload_bot_modules`).
    Перезагрузка выполняется в том же объекте модуля и заново исполняет код,
    поэтому:

      * все функции возвращаются к **настоящим** (`tg.send_message`,
        `bot.get_employee_name`, `bot.send_to_data_base`) — заглушки,
        поставленные офлайн-набором один раз на импорте, оказывались
        **стёртыми**; бот не отправлял ничего, и тесты падали с
        `IndexError: list index out of range`;
      * `telegram_bot.TOPIC_RAW` перечитывается из окружения, а импорт
        `test_equipment_intake_production` (по алфавиту раньше) зафиксировал
        **боевые** топики `.env` (`[2, 318]`), из-за чего отправка в тестовый
        топик отклонялась.

    Модуль, которому это нужно, отдаёт два установщика — `_restore_test_config`
    (конфигурация) и `_install_test_stubs` (заглушки). Они вызываются **в
    начале каждого теста**, до любых локальных подмен самого теста, поэтому
    тест по-прежнему может переопределить заглушку или топик под себя.
    Для остальных модулей шаг пропускается.
    """
    restore = getattr(request.module, "_restore_test_config", None)

    if restore is not None:
        restore()

    installer = getattr(request.module, "_install_test_stubs", None)

    if installer is not None:
        installer()

    yield
