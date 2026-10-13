"""
Warehouse-aware write of a completed intake report to `exceptions`.

Why this module exists
----------------------
The legacy text flow (`sendToDataBase.send_to_data_base`) writes the journal.
The guided photo flow used to stop at `telegram_equipment_reports`. The
integration plan unifies both, so this writer feeds `exceptions` from the
guided flow. The table was renamed from `exceptions_glpc` on 12.10.2026 — see
`EXCEPTIONS_TABLE`.

Two rules are enforced here on purpose:

1. **The warehouse comes from the Telegram topic, never from a default.**
   `DEFAULT_WAREHOUSE` is GLP-C, so any code path that falls back to it would
   silently file SMALL-P3 faults under GLP-C. When the warehouse is missing or
   is not a configured one, `write_exception` raises `WarehouseError` and the
   caller tells the operator instead of guessing.

2. **`device_type` holds the canonical fleet type**, not the operator's button
   label. The column already migrated to canonical names (`RT_KUBOT`,
   `RT_KUBOT_E2`, `RT_KUBOT_MINI_HAIFLEX`), so the label the operator tapped is
   preserved in the description and the `answers` table instead.

Category matters when resolving the type: `warehouse + equipment_code` is not
unique. On SMALL-P3 the code `13` is both the robot `RT_KUBOT` and a charging
station, and 29 such collisions exist. The lookup therefore always filters by
category as well.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from equipment_intake.template_match import (  # noqa: F401  (см. ниже)
    match_template,
    resolve_with_source,
)
from shift import WARSAW_TZ, get_current_shift
from warehouses import WAREHOUSES

# `match_template` в этом модуле намеренно оставлен **реэкспортом**, хотя сам
# `report_writer` его больше не зовёт напрямую (подбор целиком живёт в
# `resolve_with_source`). На `rw.match_template` смотрят тесты как на **шов**:
# `tests/test_report_writer.py`, `tests/test_intake_validation.py` подменяют его,
# чтобы отключить подбор и проверить остальную запись. Убрать имя значило бы
# разом переписать десяток чужих тестов и потерять эту точку управления.

logger = logging.getLogger(__name__)

#: **Единый журнал инцидентов.** До 12.10.2026 здесь стояло `exceptions_glpc`:
#: существовали две таблицы, и бот писал в legacy, а читатели API — в новую.
#: Переход завершён (`tk-assist-api`, миграция `0069` + перевод читателей),
#: поэтому источник один. `exceptions_glpc` остаётся архивом только для
#: чтения и не удаляется.
EXCEPTIONS_TABLE = "exceptions"

#: Старое имя оставлено как псевдоним: на него смотрят внешние проверки и
#: документация, и молчаливое исчезновение имени сломало бы их без причины.
#: Использовать в новом коде `EXCEPTIONS_TABLE`.
GLPC_TABLE = EXCEPTIONS_TABLE

# Columns that arrive with the intake-analytics migration. They may be missing
# from the live table while the bot is already deployed, so the write must work
# without them (see `write_exception`). A missing column is an expected state
# during rollout, not a failure.
OPTIONAL_COLUMNS = (
    "module",
    "device_number",
    "object_type",
    "report_id",
    "unresolved_reason",
    # Причина ошибки. Раньше жила **только** в JSON `answers`, поэтому отчёт
    # «ошибки по причинам» был невозможен: SQL-агрегат по JSON никто не
    # напишет. Колонка добавлена миграцией `0075`.
    "cause",
)

#: **Разложенные пункты записи** (миграция `0080_error_columns_not_text.sql`).
#:
#: Требование владельца 11.10.2026: «каждый пункт записи был в отдельном
#: столбе». До этого текст ошибки, тип и модуль склеивались в
#: `issue_description` четырьмя строками.
#:
#: **Почему это ОТДЕЛЬНЫЙ набор, а не часть `OPTIONAL_COLUMNS`.** Дыра,
#: найденная 11.10.2026 на живой базе: проверка «есть ли аналитические
#: колонки» читала **все** колонки одним запросом, а PostgREST отвергает
#: запрос целиком, если отсутствует **хотя бы одна**. То есть одна не
#: приехавшая миграция (`reported_*`) снимала заодно и старые колонки, и
#: аналитика пропадала целиком: в журнале оказалось **1663** строк без
#: `cause` из 2574. Наборы приезжают разными миграциями — проверяться они
#: обязаны **по отдельности**.
SPLIT_COLUMNS = (
    "reported_error",
    "reported_type",
    "module_text",
)

#: **Пометка «на разбор»** (`needs_review` + `review_reason`).
#:
#: Решение владельца 12.10.2026: номер, которого нет в справочнике, сохраняется,
#: но запись **помечается**, «чтобы было видно, что её надо проверить».
#:
#: **Почему отдельный набор и почему он может отсутствовать.** В единой таблице
#: `exceptions` этих колонок пока **нет** (проверено на живой базе 12.10.2026 —
#: они есть у `equipment`, но не у `exceptions`). Схему `exceptions` эта работа
#: не меняет (решение владельца: колонки уже приведены в порядок миграцией
#: `0100`), поэтому писатель **проверяет их наличие** и заполняет только если
#: они приехали. Так требование выполняется без риска уронить вставку на живой
#: базе: `PostgREST` отвергает запрос целиком из-за одной неизвестной колонки.
REVIEW_COLUMNS = (
    "needs_review",
    "review_reason",
)

#: **Ссылка на карточку робота** (`robot_id`).
#:
#: 🔴 Требование владельца 13.10.2026: «на сервере не храниться robot_id на
#: записях». Проверено на живой базе: из **275** записей бота `robot_id` был
#: заполнен у **нуля** — писался только числовой `robot_number`.
#:
#: **Чем это плохо.** `robot_number` — то, что оператор назвал руками; он
#: повторяется **между складами** (`121`, `122`, `123` есть и в GLP-C, и в
#: SMALL-P3), поэтому по нему нельзя надёжно открыть карточку робота. `robot_id`
#: — внешний ключ на `robots_maintenance_list` (проверено: FK
#: `exceptions_robot_id_fkey`), и он однозначен.
#:
#: **Почему отдельный набор.** Колонка может появиться не сразу, а PostgREST
#: отвергает запрос **целиком** из-за одной неизвестной колонки. Общий набор
#: уронил бы вместе с ней и аналитику — этот дефект уже был 11.10.2026, когда
#: неприехавшая миграция `0080` унесла `cause` (1663 строки).
ROBOT_COLUMNS = ("robot_id",)

#: Наборы колонок **по отдельности**. Один источник правды для проверок: наборы
#: приезжают разными миграциями, и пробы у каждого свои. Тесты считают число
#: проб по этому списку, а не вписывают число — иначе каждый новый набор ломал
#: бы их по причине, к сути не относящейся.
ALL_ANALYTICS_COLUMN_SETS = (
    OPTIONAL_COLUMNS,
    SPLIT_COLUMNS,
    REVIEW_COLUMNS,
    ROBOT_COLUMNS,
)

#: Все наборы — для читаемости и для мест, которым нужен полный список.
ALL_ANALYTICS_COLUMNS = OPTIONAL_COLUMNS + SPLIT_COLUMNS + REVIEW_COLUMNS + ROBOT_COLUMNS

# How long the "columns are missing" answer is remembered. Short TTL, same
# reasoning as the intake editor's schema cache: after the migration is applied
# the bot picks the columns up without a restart.
_OPTIONAL_TTL_SECONDS = 120
#: Состояние набора `OPTIONAL_COLUMNS` и набора `SPLIT_COLUMNS` — **по
#: отдельности**. Одна общая память была частью дыры: раз наборы приезжают
#: разными миграциями, сбой проверки одного не должен прятать другой.
_optional_columns_state: Optional[bool] = None
_optional_columns_checked_at: float = 0.0
_split_columns_state: Optional[bool] = None
_split_columns_checked_at: float = 0.0
_review_columns_state: Optional[bool] = None
#: Состояние набора `ROBOT_COLUMNS` — своё, как и у остальных.
_robot_columns_state: Optional[bool] = None
_robot_columns_checked_at: float = 0.0
_review_columns_checked_at: float = 0.0

#: Зона по умолчанию — для складов, у которых разбиения на зоны нет.
#:
#: **Раньше это была единственная зона для всех складов**, и получалось, что у
#: SMALL-P3 в колонке стоял `C2` — зона GLP-C (проверено: 1 253 строки). Метрика
#: «ошибки по зонам» складывала два склада в одну корзину.
#:
#: Теперь зона выводится из устройства: у SMALL-P3 она лежит в `sub_warehouse`
#: робота (`D` / `E`), и находится по номеру, который оператор уже ввёл. Только
#: GLP-C (и склады без зон) получают это значение.
DEFAULT_ISSUE_WAREHOUSE = "C2"

#: Склады, у которых есть разбиение на зоны. Для них зона берётся из
#: `sub_warehouse` устройства, а не из значения по умолчанию.
#: Проверено на живой базе: `sub_warehouse` заполнен только у SMALL-P3.
WAREHOUSES_WITH_ZONES = ("SMALL-P3",)

#: Справочник оборудования роботов — источник `sub_warehouse`.
ROBOTS_TABLE = "robots_maintenance_list"

#: Кэш «(склад, номер) → строка робота». Нужен потому, что зона и `robot_id`
#: спрашиваются про одного робота, а строк в отчёте бывает много.
#: Сбрасывается `reset_robot_cache()` — в тестах и при долгой работе процесса.
_ROBOT_ROW_CACHE: Dict[Any, Any] = {}


def reset_robot_cache() -> None:
    """Сброс кэша справочника роботов — для тестов."""
    _ROBOT_ROW_CACHE.clear()

# Categories that can be resolved against the canonical equipment inventory.
INVENTORY_CATEGORIES = ("robot", "workstation", "charging")

#: **Карта владельца 11.10.2026: метка оператора → канонический тип флота.**
#:
#: ```
#: K50H    = RT_KUBOT_MINI_HAIFLEX
#: A42T    = RT_KUBOT
#: A42T E2 = RT_KUBOT_E2
#: ```
#:
#: Это **проверка ожиданий**, а не таблица подстановки: тип по-прежнему берётся
#: из справочника `equipment` (склад + категория + код), и подменять его догадкой
#: нельзя — номера роботов повторяются между складами, а на P3-DC-1 под теми же
#: метками стоят другие модели (`P1200`). Карта служит эталоном для сторожей:
#: они падают, если справочник начнёт отдавать для этих меток другой тип.
#:
#: Проверено на живой базе: код `3628` (GLP-C) → `RT_KUBOT_MINI_HAIFLEX` (id 2),
#: у `RT_KUBOT` — id 13, у `RT_KUBOT_E2` — id 12.
OWNER_LABEL_TYPES = {
    "K50H": "RT_KUBOT_MINI_HAIFLEX",
    "A42T": "RT_KUBOT",
    "A42T E2": "RT_KUBOT_E2",
}

# Справочник готовых описаний ошибок: по нему отдел ведёт журнал. Пока приём
# писал сюда сырой текст, строки нельзя было ни группировать по типу ошибки, ни
# считать время решения.
TEMPLATES_TABLE = "issue_templates"

#: Поля шаблона, нужные для подбора и заполнения колонок.
TEMPLATE_COLUMNS = (
    # `id` нужен с 12.10.2026: единая таблица хранит **ссылку** на шаблон
    # (`template_id`), а не его текст. Без `id` в выборке ссылку не проставить,
    # и строки бота остались бы без типа ошибки.
    "id",
    "employee_title",
    "issue_sub_type",
    "issue_type",
    "issue_description",
    "recovery_title",
    "solving_time",
)

# Справочник меняется редко (29 строк, правится вручную), поэтому держим его в
# памяти. TTL, а не вечное кэширование: добавленный шаблон должен заработать без
# перезапуска бота.
_TEMPLATES_TTL_SECONDS = 600
_templates_cache: Optional[list] = None
_templates_checked_at: float = 0.0


class WarehouseError(ValueError):
    """The report cannot be filed because its warehouse is unknown."""


def configured_warehouses() -> Tuple[str, ...]:
    return tuple(WAREHOUSES.values())


def validate_warehouse(warehouse: Optional[str]) -> str:
    """
    Return the warehouse title or raise `WarehouseError`.

    Empty input is rejected: falling back to `DEFAULT_WAREHOUSE` here is exactly
    the bug this guard exists to prevent.
    """
    title = str(warehouse or "").strip()

    if not title:
        raise WarehouseError(
            "Warehouse is not set for this report. Send the photo in the "
            "warehouse's own error topic."
        )

    if title not in configured_warehouses():
        raise WarehouseError(
            f"Warehouse '{title}' is not configured. Configured: "
            f"{', '.join(configured_warehouses())}."
        )

    return title


def intake_category(value: str) -> str:
    """
    Ответ оператора → канонический ключ категории.

    **`qr` → `qr_code`.** Дерево отдаёт `qr`, а колонка `object_type` и
    справочник `equipment.category` используют `qr_code`. Раньше здесь
    возвращалось `qr`, и одно понятие получало **два написания** в зависимости от
    того, каким путём пришёл отчёт: через API (`telegram_reports.py`
    канонизирует `qr` → `qr_code`) или напрямую из бота. Собственный комментарий
    API называет это недопустимым, и он прав: группировка по категориям
    разделила бы «qr» и «qr_code» на две строки.

    Проверено по живой базе: словарь `equipment.category` —
    `charging` / `qr_code` / `robot` / `workstation`, значения `qr` там нет.
    """
    return {
        "robot": "robot",
        "workstation": "workstation",
        "charging station": "charging",
        "charger": "charging",
        "qr code": "qr_code",
        "qr": "qr_code",
    }.get(str(value or "").strip().casefold(), str(value or "").strip().casefold())


def identifier(answers: Dict[str, Any]) -> str:
    """
    Equipment identity for this report.

    The guided flow may carry the richer QR shape (shelf number, or X/Y/zone)
    once the test-bot flow lands; the current tree only collects a device
    number. Both shapes are accepted so the writer does not need revisiting.
    """
    shelf = str(answers.get("shelf_number") or "").strip()

    if shelf:
        return shelf

    x = str(answers.get("qr_x") or "").strip()

    if x:
        y = str(answers.get("qr_y") or "").strip()
        zone = str(answers.get("qr_zone") or "").strip()

        return f"X={x}; Y={y}; Zone={zone}"

    return str(answers.get("device_number") or "").strip()


def canonical_type(warehouse: str, category: str, code: str) -> Optional[str]:
    """
    Canonical fleet type for a device code, or None when it cannot be resolved.

    None is not an error: an unknown or brand-new device still has to be filed,
    with the operator's label kept in the description.
    """
    if not code or category not in INVENTORY_CATEGORIES:
        return None

    from sendToDataBase import rest_get

    rows = rest_get(
        "equipment",
        {
            "select": "equipment_type_id",
            "warehouse": f"eq.{warehouse}",
            "category": f"eq.{category}",
            "equipment_code": f"eq.{code}",
            "limit": "1",
        },
    )

    if not rows:
        if rows is None:
            logger.warning(
                "Canonical type lookup failed for %s/%s/%s", warehouse, category, code
            )

        return None

    type_id = rows[0].get("equipment_type_id")

    if type_id is None:
        return None

    types = rest_get("equipment_types", {"select": "type", "id": f"eq.{type_id}", "limit": "1"})

    if not types:
        return None

    return str(types[0].get("type") or "").strip() or None


def device_zone(warehouse: str, code: str) -> Optional[str]:
    """
    Зона склада, в которой стоит устройство: `D` / `E` на SMALL-P3.

    **Откуда берётся.** У оборудования есть колонка `sub_warehouse` — это и есть
    зона. Проверено на живой базе 05.10.2026: заполнена она **только** у
    SMALL-P3, значениями `D` (40 роботов, 62 единицы оборудования, 13 зарядок,
    9 станций) и `E` (80 / 118 / 27 / 11). У GLP-C, PNT-A и P3-DC-1 пусто — там
    разбиения на зоны нет, и это нормально.

    Поэтому у GLPC зона — `C2` (значение по умолчанию), а у SMALL-P3 она
    находится по номеру устройства, который оператор уже ввёл.

    **Почему не спрашиваем оператора заново.** Он уже назвал номер робота, а
    зона из него выводится однозначно. Лишний шаг в дереве замедлял бы приём и
    давал бы второй источник правды: оператор мог бы выбрать зону, не совпадающую
    с фактическим положением робота.

    **Ловушка, из-за которой нужен склад.** Номера роботов **повторяются между
    складами**: `121`, `122`, `123` есть и в GLP-C, и в SMALL-P3. Без склада в
    условии подбор вернул бы зону с чужого склада.

    `None` — не ошибка: устройство не найдено или у склада нет зон. Тогда
    вызывающий код оставляет значение по умолчанию.
    """
    if warehouse not in WAREHOUSES_WITH_ZONES or not code:
        return None

    # **Та же строка справочника, что и для `robot_id`** (`robot_row`): зона и
    # ссылка читаются одним запросом, а не двумя. Только числовой номер:
    # `sub_warehouse` ищется по `robot_number`, а составные номера (`H108/1834`)
    # в этом справочнике не лежат.
    row = robot_row(code, warehouse)

    if not row:
        return None

    zone = str(row.get("sub_warehouse") or "").strip()

    return zone or None


#: Зоны, которые справочник `warehouse_zones` знает по складам.
#:
#: Проверено на живой базе 12.10.2026: у GLP-C (id 1) — `C2`, у SMALL-P3 (id 3)
#: — `D` и `E`. Держим карту здесь, а не спрашиваем справочник на каждый отчёт:
#: он меняется раз в жизни, а обращение к базе на каждой записи — лишняя
#: задержка приёма.
#:
#: **Зачем проверка вообще.** На `exceptions` есть составной внешний ключ
#: `(warehouse, zone)` → `warehouse_zones`. Зона, которой у склада нет,
#: **уронит вставку** — а до 05.10.2026 бот писал всем складам константу `C2`,
#: и у SMALL-P3 осталось 9 таких строк. Неизвестная пара обязана давать `NULL`:
#: приписать ошибку зоне, где её не было, хуже, чем не знать зону.
KNOWN_ZONES: Dict[str, tuple] = {
    "GLP-C": ("C2",),
    "SMALL-P3": ("D", "E"),
}


def _allowed_zone(warehouse: str, zone: Optional[str]) -> Optional[str]:
    """Зона, если пара (склад, зона) допустима; иначе `None`.

    `None` — это «зона неизвестна», а не «зоны нет»: `NULL` в составном ключе
    `MATCH SIMPLE` не проверяется, поэтому вставка проходит, а факт остаётся
    честным. Догадка (`D` или `E` для SMALL-P3) запрещена: она связала бы
    ошибку с зоной, где её не было.
    """
    allowed = KNOWN_ZONES.get(str(warehouse or "").strip())

    if not allowed or not zone:
        return None

    return zone if zone in allowed else None


def _received_at(result: Dict[str, Any]) -> datetime:
    """
    When the report arrived, as an aware datetime.

    `flow._confirm` stamps `created_at` with `time.strftime` — a *naive* local
    timestamp of the host. Reading it as UTC would shift shift boundaries on a
    non-UTC host, so the value is interpreted through the host's local zone
    (which is what `time.strftime` encodes) and then made aware. An unusable or
    missing value falls back to "now", never to a silent wrong shift.
    """
    raw = result.get("received_at") or result.get("created_at")

    if isinstance(raw, datetime):
        received = raw
    elif raw:
        try:
            received = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    else:
        return datetime.now(timezone.utc)

    if received.tzinfo is None:
        # `time.strftime` produced local wall-clock time: attach the local zone.
        received = received.astimezone()

    return received


def _robot_number(code: str) -> Optional[int]:
    """
    `robot_number` is bigint; non-numeric identities live in the description.

    Only ASCII digits count. `str.isdecimal()` also accepts non-ASCII digits
    (e.g. Arabic-Indic '١٢٣'), which would silently turn a mis-typed identifier
    into a different robot number — so the value is validated explicitly.
    """
    text = str(code or "")

    if not text or not text.isascii() or not text.isdecimal():
        return None

    value = int(text)

    return value if value <= 9_223_372_036_854_775_807 else None


def robot_row(number, warehouse: str = "") -> Optional[Dict[str, Any]]:
    """
    Строка робота из справочника: `id` и `sub_warehouse` — **одним запросом**.

    **Почему одна функция, а не две.** Зона (`device_zone`) и ссылка
    (`robot_id_for`) читают **одну и ту же** строку `robots_maintenance_list` по
    одной и той же паре (склад, номер). Два отдельных запроса — это два
    обращения к базе на каждую строку отчёта, хотя данные одни. Владелец просил
    и то, и другое, поэтому строка читается **один раз** и отдаётся обоим.

    Возвращается `None`, если робота нет или справочник недоступен: и зона, и
    ссылка тогда пустые, но отчёт записывается в любом случае.

    **Результат кэшируется на процесс.** Зона и ссылка спрашиваются про одного и
    того же робота, а отчётов за смену сотни: без кэша каждая запись делала бы
    два одинаковых чтения. Справочник роботов меняется редко (номер и склад
    робота — не то, что правят в течение дня), поэтому короткий кэш безопасен.
    """
    number = _robot_number(number)

    if number is None or not warehouse:
        return None

    key = (str(warehouse), number)

    if key in _ROBOT_ROW_CACHE:
        return _ROBOT_ROW_CACHE[key]

    from sendToDataBase import rest_get

    # `optional=True`: сбой справочника не должен ронять запись отчёта.
    rows = rest_get(
        ROBOTS_TABLE,
        {
            "select": "id,sub_warehouse",
            "warehouse": f"eq.{warehouse}",
            "robot_number": f"eq.{number}",
            "limit": "1",
        },
        optional=True,
    )

    if not rows:
        # «Не найден» тоже запоминаем: неизвестный номер иначе заставлял бы
        # ходить в базу на каждой строке отчёта.
        _ROBOT_ROW_CACHE[key] = None
        return None

    row = rows[0]

    found = row if isinstance(row, dict) else None
    _ROBOT_ROW_CACHE[key] = found

    return found


def _robot_id_from_row(row: Optional[Dict[str, Any]]) -> Optional[int]:
    """`id` робота из уже прочитанной строки справочника."""
    if not row:
        return None

    robot_id = row.get("id")

    return robot_id if isinstance(robot_id, int) else None


def robot_id_for(number, warehouse: str = "") -> Optional[int]:
    """
    Ссылка на карточку робота (`robots_maintenance_list.id`) или `None`.

    🔴 **Требование владельца 13.10.2026:** «на сервере не храниться robot_id на
    записях». Проверено на живой базе: из **275** записей бота `robot_id` был
    заполнен у **нуля** — писался только числовой `robot_number`.

    **Чем это плохо.** `robot_number` — то, что оператор назвал руками, и он
    **повторяется между складами** (`121`, `122`, `123` есть и в GLP-C, и в
    SMALL-P3). Поэтому по номеру нельзя надёжно открыть карточку робота; `robot_id`
    — внешний ключ (`exceptions_robot_id_fkey`), и он однозначен.

    🔴 **Склад обязателен.** Без него подбор вернул бы id робота с чужого склада —
    ровно та ловушка, на которой уже спотыкались при выводе зон (номера
    повторяются между складами). Склад пуст — `None`, а не догадка.

    `None` — не ошибка: робота нет в справочнике (тогда запись помечается «на
    разбор»), номер составной (`H108/1834`) или справочник недоступен. Отчёт
    обязан записаться в любом случае: ссылку не выдумываем.
    """
    return _robot_id_from_row(robot_row(number, warehouse))


def device_numbers(code: str, category: str = "") -> list[str]:
    """Номера устройств из того, что ввёл оператор.

    **`category` обязателен для QR.** Для остальных категорий он не меняет
    поведения и оставлен необязательным ради существующих вызовов.

    **Требование владельца 08.10.2026, дословно:** «номера девайсов делятся
    через `/`, если `4305/435` то это два отдельных девайса, и одна ошибка
    должна быть продублирована на сервере два раза или больше, в зависимости от
    того, сколько разделений с разными номерами девайсов».

    **Зачем.** Раньше `3561/3528` целиком уходило в `device_number`, а
    `error_robot` оставался пустым — ошибка не была связана **ни с одним** из
    двух роботов. Проверено на живой базе: **56 таких строк**, и у всех
    `error_robot is null`. Отчёт «сколько ошибок у робота 3561» их не видел.

    **Почему возвращается список, а не один номер.** Из одного сообщения
    рождается **столько строк журнала, сколько номеров**: у каждой свой
    `error_robot`, своя зона и свой `uniq_key`. Иначе пришлось бы выбирать
    «главный» номер, и второй робот молча выпал бы из статистики.

    **Что считается разделителем.** Только `/`. Дефис и пробел — нет: в номерах
    встречается `H108/1834`, а `H156` в `3452/3707/H156` — это уже третий
    элемент, и он тоже отдельное устройство.

    **Пустые части отбрасываются, повторы схлопываются.** `4305//435` — тот же
    случай, что `4305/435`; `3561/3561` (оператор повторил номер) не должен
    создать две одинаковые строки с одним `uniq_key` — вторая всё равно
    отсеклась бы уникальным индексом, но тихо, а не как осознанное решение.

    **Порядок сохраняется.** Первый номер — тот, что оператор назвал первым:
    по нему строится текст описания, и он же остаётся «основным» для читателя.
    """
    text = str(code or "").strip()

    if not text:
        return []

    # **Для QR кода слэш — ошибка оператора, а не второе устройство.**
    #
    # Решение владельца 12.10.2026: «для QR деление по `/` ЗАПРЕЩЕНО: код
    # места/полки содержит дефисы, а не слэши, и слэш там означал бы ошибку
    # оператора». Поэтому запись **не делится**, а значение остаётся как введено
    # — его разберёт человек: молча создать из опечатки вторую строку журнала
    # значило бы выдумать устройство, которого нет.
    #
    # Формат при этом проверяется на вводе (`validation.validate_format`), и до
    # записи такой код доходит только через API — там он тоже не делится.
    if str(category or "").strip().casefold() == "qr_code":
        return [text]

    parts: list[str] = []
    seen: set[str] = set()

    for chunk in text.split("/"):
        value = chunk.strip()
        if value and value not in seen:
            seen.add(value)
            parts.append(value)

    return parts


def _uniq_key(result: Dict[str, Any], report_id: str, user_id) -> str:
    """
    Stable identifier used by the unique index to make writes idempotent.

    Telegram message coordinates are preferred: one message is exactly one
    report, and the pair is stable across a redelivered update. Falling back to
    the photo filename is unsafe on its own — a report without a saved photo used
    the device number instead, so two different faults on the same robot by the
    same sender collapsed into one key and the second was dropped as a duplicate.
    """
    chat_id = result.get("chat_id")
    message_id = result.get("message_id")

    if chat_id is not None and message_id is not None:
        return f"telegram-photo-bot:{chat_id}:{message_id}"

    if chat_id is not None:
        return f"telegram-photo-bot:{chat_id}:{report_id}"

    return f"telegram-photo-bot:{user_id}:{report_id}"


def _report_id(result: Dict[str, Any], answers: Dict[str, Any]) -> str:
    """Name used for logging and as a weak fallback identity."""
    image = str(result.get("image") or "").strip()

    if image:
        name = Path(image).name.strip()

        if name:
            return name

    return identifier(answers) or "report"


def _card_id(value):
    """
    `add_by` value for the journal, or None.

    A whitespace-only card must not become a value that points nowhere, so the
    text is trimmed like every other text field here. A numeric card passes
    through unchanged — `employees.card_id` is not always a string.
    """
    if value is None:
        return None

    if isinstance(value, str):
        return value.strip() or None

    return value


def _optional_fields(
    answers: Dict[str, Any],
    category: str,
    module: str,
    report_id: str,
    unresolved_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Columns added by the intake-analytics migration.

    Kept separate so `write_exception` can retry without them: the bot is
    deployed before the migration is applied, and PostgREST answers 400 for an
    unknown column. Those four columns must never cost the report itself.
    """
    device_number = str(answers.get("device_number") or "").strip()

    # Причина — человекочитаемая подпись из дерева («Obstacle on the path»),
    # а не внутренний код: по ней строят отчёт и её читает человек.
    cause = str(answers.get("cause") or "").strip()

    return {
        "module": module or None,
        "device_number": device_number or None,
        "object_type": category or None,
        "report_id": report_id or None,
        "unresolved_reason": unresolved_reason,
        "cause": cause or None,
    }


def _review_marked(result: Dict[str, Any]) -> bool:
    """Помечена ли запись на разбор («номер не найден в справочнике»).

    Решение владельца 12.10.2026: такой отчёт **сохраняется**, но помечается,
    «чтобы было видно, что его надо проверить». Отдельный помощник, потому что
    от этого зависят и колонки пометки, и текст `unresolved_reason`.
    """
    return bool(str(result.get("review_reason") or "").strip())


def _unresolved_reason(card_id: Any, result: Dict[str, Any]) -> Optional[str]:
    """
    Чего не хватило в записи — одной строкой через запятую.

    Поле `unresolved_reason` заведено миграцией 0067, но до этой правки его
    никто не заполнял: по нему нельзя было понять, полная запись или нет,
    хотя именно для этого он и нужен. Считается здесь, а не в
    `_optional_fields`, потому что фото и карточка работника живут в `result`,
    а не в ответах дерева.

    Значения и **разделитель** совпадают с backfill'ом в 0067 (`concat_ws(', ', …)`)
    — иначе одна колонка имела бы два формата: `no_employee,no_photo` от бота и
    `no_employee, no_end_time, no_photo` от миграции. Потребитель, разбирающий
    строку по `", "`, молча не увидел бы причин в записях бота. Проверено на
    живой базе: в ней уже есть строки обоих видов.

    `no_end_time` здесь намеренно **нет**: приём всегда пишет `error_end_time`
    (он равен времени отчёта, `solving_time = 0`), поэтому эта причина у новых
    записей невозможна. Ставить её «на всякий случай» значило бы помечать
    полные записи неполными.
    """
    reasons = []

    if not card_id:
        # Ошибку не с кем связать: в метрики по работнику она не попадёт.
        reasons.append("no_employee")

    if not str(result.get("photo_url") or "").strip():
        # Фото к ошибке не привязано (загрузка не удалась).
        reasons.append("no_photo")

    if _review_marked(result) and not _review_columns_available():
        # Колонок пометки в таблице ещё нет — но факт терять нельзя: он
        # уезжает в `unresolved_reason`, который читается и сегодня.
        reasons.append("number_not_in_registry")

    return ", ".join(reasons) or None


def load_templates() -> list:
    """
    Справочник `issue_templates` (кэш на 10 минут).

    Пустой список — не ошибка: он означает «шаблонов нет». `None` от `rest_get`
    тоже приводится к пустому списку, потому что сбой базы **не должен** ломать
    приём: отчёт запишется с прежними значениями, как до этой правки. Разница
    между «база недоступна» и «шаблонов нет» для подбора не важна — в обоих
    случаях шаблон не найден, и подставлять наугад нельзя.
    """
    global _templates_cache, _templates_checked_at

    now = time.time()

    if _templates_cache is not None and now - _templates_checked_at < _TEMPLATES_TTL_SECONDS:
        return _templates_cache

    from sendToDataBase import rest_get

    # `optional=True`: сбой справочника — ожидаемая ситуация, не ERROR в логе.
    rows = rest_get(
        TEMPLATES_TABLE,
        {"select": ",".join(TEMPLATE_COLUMNS), "limit": "500"},
        optional=True,
    )

    _templates_cache = list(rows or [])
    _templates_checked_at = now

    return _templates_cache


def reset_templates_cache() -> None:
    """Сбросить кэш справочника (тесты и ручная проверка)."""
    global _templates_cache, _templates_checked_at

    _templates_cache = None
    _templates_checked_at = 0.0


def _template_values(template: Optional[Dict[str, Any]], category: str, module: str, device_type: str) -> Dict[str, Any]:
    """
    Значения колонок журнала из подобранного шаблона.

    Маппинг восстановлен по живой базе, а не придуман: на 12 474 классических
    строках, где человек проставил шаблон точно, `second_column` =
    `employee_title` совпадает в 79 % случаев, `first_column` =
    `issue_sub_type` — в 53 %.

    При `template = None` возвращаются прежние значения приёма: категория
    оборудования и модуль. Это честный откат, а не догадка — пустая колонка
    лучше ошибки, отнесённой не к тому типу.
    """
    # **Имена колонок единой таблицы `exceptions`** (до 12.10.2026 —
    # `exceptions_glpc`). Текст пишется снимком в `raw_*`, а справочник
    # подключается ссылкой `template_id`: читатель предпочитает снимок, а
    # шаблон берёт запасным путём. Так правка шаблона не переписывает
    # исторические отчёты задним числом.
    if not template:
        return {
            "raw_fault": category,
            "equipment_category": category,
            "raw_template_title": module or device_type,
            "raw_recovery": None,
            # 🔴 **`None`, а не `0`.** Ноль — это «решили мгновенно», то есть
            # утверждение о времени, которого никто не делал. Читатель
            # (`journal.py`, `exception_rows.py`) предпочитает снимок `raw_*`
            # справочнику, поэтому `0` **перебивал бы** заполненный
            # `solving_time` шаблона: даже после заполнения заготовки строка
            # навсегда показывала бы `0:00`. Проверено на живой базе: все 192
            # строки бота имеют `raw_solving_time = 0`, и метрика «сколько
            # решается ошибка» считала эти нули измерениями.
            "raw_solving_time": None,
            "template_id": None,
        }

    return {
        # `issue_sub_type` — «подтип» ошибки, именно он лежит в `first_column` у
        # классических строк. Если у шаблона он пуст (такие есть: id 122),
        # берём название, чтобы колонка не осталась пустой.
        "raw_fault": template.get("issue_sub_type") or template.get("employee_title"),
        "equipment_category": template.get("issue_type"),
        "raw_template_title": template.get("employee_title"),
        "raw_recovery": template.get("recovery_title") or None,
        # Пустое значение справочника остаётся пустым снимком — см. объяснение
        # выше: незаполненная заготовка не должна выглядеть решённой за 0 минут.
        "raw_solving_time": _solving_time(template.get("solving_time")),
        # Ссылка на справочник. `id` есть у каждого шаблона; при его отсутствии
        # (усечённый SELECT) остаётся `None` — ссылку не выдумываем.
        "template_id": template.get("id"),
    }


def _solving_time(value: Any) -> int | None:
    """
    Время решения в минутах — целое, либо `None`, если в справочнике пусто.

    **Почему `None`, а не `0`.** Ноль означает «решение заняло 0 минут» — это
    утверждение о факте, которого в справочнике нет. Читатели отдают снимок
    `raw_solving_time` в приоритете над справочником, поэтому `0` **скрывал бы**
    заполненный шаблон навсегда. `None` же передаёт ход справочнику, и после
    заполнения заготовки значение появится и в старых строках.

    В базе колонка целочисленная, а `error_end_time` считается как
    `error_start_time + solving_time`; `None` значит «время решения неизвестно»,
    и конец ошибки тогда равен началу — как и раньше при нуле.
    Нечисловое значение справочника тоже даёт `None`: испорченный справочник не
    должен ронять запись отчёта и не должен подставлять выдуманное число.
    """
    if value is None or value == "":
        return None

    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def build_row(
    result: Dict[str, Any],
    warehouse: str,
    include_optional: bool = True,
    include_split: bool = True,
    include_review: bool = True,
    include_robot: bool = True,
) -> Dict[str, Any]:
    """Первая (и обычно единственная) строка журнала для отчёта.

    Оставлена ради существующих вызовов: у отчёта об одном устройстве строка
    ровно одна. Для нескольких устройств (см. `device_numbers`) нужен
    `build_rows`, потому что вернуть одну строку из нескольких нельзя, не
    потеряв остальные.
    """
    rows = build_rows(
        result, warehouse,
        include_optional=include_optional,
        include_split=include_split,
        include_review=include_review,
        include_robot=include_robot,
    )

    return rows[0] if rows else {}


def build_rows(
    result: Dict[str, Any],
    warehouse: str,
    include_optional: bool = True,
    include_split: bool = True,
    include_review: bool = True,
    include_robot: bool = True,
) -> list[Dict[str, Any]]:
    """
    Map an intake result onto the columns of the unified journal (`exceptions`).

    **Возвращается список: по строке на каждое устройство в номере.**
    Требование владельца 08.10.2026, дословно: «номера девайсов делятся через
    `/`, если `4305/435` то это два отдельных девайса, и одна ошибка должна быть
    продублирована на сервере два раза или больше, в зависимости от того сколько
    разделений с разными номерами девайсов».

    До этой правки `3561/3528` уходило в `device_number` целиком, а
    `error_robot` оставался пустым — на живой базе таких строк **56**, и у всех
    `error_robot is null`. Ошибка не принадлежала ни одному роботу, поэтому в
    отчётах «ошибки по роботам» не учитывалась вообще.

    **Каждая строка самостоятельна:** свой `error_robot`, своя зона
    (`issue_warehouse`), свой `device_number` и **свой `uniq_key`**. Последнее
    обязательно: `uniq_key` уникален, и без суффикса вторая строка была бы
    отброшена уникальным индексом как дубликат — молча и без ошибки.

    **Два независимых набора колонок.** `include_optional` отвечает за старые
    аналитические колонки (миграции 0067–0075), `include_split` — за
    разложенные пункты (миграция `0080`). Выключение одного **не** выключает
    другой: раньше это был один флаг, и одна неприехавшая миграция уносила всю
    аналитику разом (на живой базе — **1663** строки без `cause` из 2574).
    Выключенный набор просто не пишется; всё остальное в строке совпадает,
    поэтому расхождение схемы и кода не теряет отчёт.

    `add_by`, `photo_url` and the analytics fields are read from `result`,
    which `flow._confirm` fills while it already has the employee row at hand —
    so the writer never issues a second lookup for data the caller knows.
    """
    # Импорт внутри функции — как у остальных обращений к `sendToDataBase`:
    # на уровне модуля это создало бы цикл (`sendToDataBase` импортирует части
    # приёма).
    from sendToDataBase import warehouse_id

    answers = result.get("answers") or {}
    category = intake_category(answers.get("object"))
    code = identifier(answers)
    label = str(answers.get("device_type") or "").strip()
    module = str(answers.get("module") or "").strip()
    resolved = canonical_type(warehouse, category, code)

    # The operator's label is never lost: it goes to the description whenever
    # the column carries the canonical type instead.
    device_type = resolved or label

    received = _received_at(result)

    # Shift boundaries are defined in Europe/Warsaw, so the timestamp is
    # converted to that zone explicitly. `astimezone()` without an argument
    # would use the host's local time (UTC on Railway) and push night reports
    # into the wrong shift.
    shift_date, shift_name = get_current_shift(received.astimezone(WARSAW_TZ))
    report_id = _report_id(result, answers)
    user_id = result.get("user_id")

    # **Каждый пункт — в свою колонку, а не в склейку.**
    #
    # Требование владельца 11.10.2026: «чтобы ошибки которые отправляются через
    # бота каждый пункт записи был в отдельном столбе на базе данных».
    #
    # Раньше здесь собирался многострочный текст:
    #
    #     Reported error: Wrong pick
    #     Identifier: 3502
    #     Reported type: K50H
    #     Module: Lifting
    #
    # Проверено на живой базе: так выглядели **все 1819** записей бота. Три
    # строки из четырёх **дублировали** колонки, которые уже есть
    # (`device_number`, `device_type`, `module`), а четвёртая — единственный
    # текст оператора — тонула в них. В выгрузке Excel это давало четыре строки
    # в ячейке «问题描述» там, где нужна одна.
    #
    # `reported_type` — **как назвал оператор** (`K50H`), отдельно от
    # `device_type` с кодом справочника (`RT_KUBOT_MINI_HAIFLEX`): это разные
    # значения, и смешивать их нельзя.
    # `module_text` — модуль из ответа, отдельно от `module`: тот заполняется
    # подбором шаблона и может быть пуст.
    reported_error = str(answers.get("description") or "").strip() or None
    reported_type = label if (label and label != device_type) else None
    module_text = module or None

    # `issue_description` остаётся **сырым текстом оператора** — для истории и
    # разбора. Склейку в него больше не пишем: значения уже в своих колонках, а
    # повторять их здесь значило бы вернуть ту самую проблему.
    #
    # **Исключение — точная связь с заготовкой.** Тогда описание ошибки берётся
    # из шаблона (см. `raw_description` ниже): владелец просил, чтобы поля
    # «подставлялись автоматически», и описание ошибки — первое из них. Текст
    # оператора при этом не теряется: он лежит в `reported_error`.
    details = [reported_error] if reported_error else []
    linked_description = None

    # `add_by` is the employee's `card_id`, which the caller already resolved
    # while looking the name up. It stays None when the account is not linked or
    # the database is unavailable — the report must still be filed, exactly like
    # `employee` does. A missing card is acceptable; an invented one is not.
    card_id = _card_id(result.get("employee_card_id"))

    # **Готовое описание ошибки из справочника.** Раньше сюда шёл сырой текст
    # оператора: `first_column` и `issue_type` дублировали категорию оборудования
    # («robot» = «robot»), `recovery_title` был пуст, `solving_time` равен нулю.
    # Из-за этого строки приёма нельзя было ни группировать по типу ошибки, ни
    # считать время решения. Проверено на живой базе 05.10.2026: так выглядели
    # все 246 строк бота.
    #
    # 🔴 **Порядок изменился 12.10.2026 — точная связь важнее подбора.**
    #
    # Требование владельца, дословно: «нужно сделать связь как-то и при добавление
    # новой ошибки делать заготовку тоже чтобы потом были поля которые будут
    # подставлены автоматически». Поэтому сначала берётся **связь** варианта
    # дерева с шаблоном (`option_id` → `telegram_intake_options.template_id`, её
    # задаёт редактор `/tree`, плюс карта `error_templates.py` для контракта
    # узлов `robot_errors_*`), и только если связи нет — прежний подбор по тексту.
    #
    # **Почему связь вообще понадобилась.** Подбор даёт «лучшее приближение», и у
    # шаблонов-дублей он ошибается: у id 5 «Driver component exception» и id 10
    # «Speed error» совпадает `issue_sub_type`, и оператор писал «driver component
    # exception», а в журнал попадало `Speed error`. Связь по id такой
    # неоднозначности не оставляет (разбор дублей — в `error_templates.py`).
    #
    # Подбор **не выброшен**: у старых и нетиповых путей `option_id` нет, и
    # «оставить как было» безопаснее, чем потерять шаблон вовсе.
    steps = result.get("path") or []

    # Связь читается **лениво**: `storage` тянет за собой дерево и его чтение, а
    # писателю журнала они нужны только ради карты связей. Ошибка чтения связи —
    # не повод не записать отчёт: `option_template_links` возвращает пустую карту,
    # и подбор по тексту остаётся запасным путём.
    try:
        from equipment_intake.storage import option_template_links

        option_links = option_template_links()
    except Exception:  # noqa: BLE001
        logger.exception("intake journal: не удалось прочитать связи вариантов")
        option_links = {}

    template, template_source = resolve_with_source(
        answers.get("description"),
        load_templates(),
        steps=steps,
        option_links=option_links,
        # Шов намеренно проходит через **модуль**: тесты подменяют
        # `rw.match_template`, и без явной передачи они бы перестали отключать
        # подбор (см. докстроку `resolve_with_source`).
        text_matcher=match_template,
    )
    template_columns = _template_values(template, category, module, device_type)

    # Описание ошибки для колонки журнала — **только при точной связи**. У
    # заготовок оно равно названию ошибки словами владельца; у шаблонов с
    # заполненным `issue_description` берётся оно. Пустое значение означает
    # «шаблон не описал ошибку», и тогда остаётся текст оператора: подставлять
    # пустоту вместо реального текста было бы потерей данных.
    if template_source == "link" and template is not None:
        linked_description = str(template.get("issue_description") or "").strip() or None

    # Время решения берём из шаблона; конец ошибки считается по конвенции
    # журнала (`error_end_time = error_start_time + solving_time`), проверенной
    # на 23 384 из 23 400 строк. **Если справочник время не задал, конец равен
    # началу** — это не «решили мгновенно», а «время решения неизвестно»:
    # снимок остаётся `None` (см. `_solving_time`), и после того как человек
    # заполнит заготовку, значение появится и в отчёте.
    solving_time = template_columns["raw_solving_time"]
    error_end = received + timedelta(minutes=solving_time or 0)

    # **Разделение номеров: по строке на каждое устройство.**
    #
    # `device_numbers` разбирает `3561/3528` на два номера. Для каждого строится
    # своя строка журнала: со своим `error_robot`, своей зоной и своим
    # `uniq_key`. Без суффикса в ключе вторая строка была бы отброшена
    # уникальным индексом как дубликат — молча и без ошибки.
    numbers = device_numbers(code, category) or [""]

    # **Зарядка: в записи ДВА устройства — станция и робот.**
    #
    # Решение владельца 12.10.2026: «если зарядка то сначала спрашиваем зарядку
    # потом номер робота и храним данные на сервере корректно». Станция — это
    # `device_number` (что именно сломалось), робот — `robot_number`
    # (историческое `error_robot`, кто стоял на станции). Без второго поля
    # ошибка зарядки не связана ни с одним роботом и не попадает в метрики по
    # устройству.
    charging_robot = (
        str(answers.get("error_robot") or "").strip()
        if category == "charging"
        else ""
    )

    rows: list[Dict[str, Any]] = []

    for number in numbers:
        # **Номер устройства остаётся в `device_number`, а не в тексте.**
        #
        # Раньше при нескольких устройствах в описание вставлялась строка
        # `Device: 3561` — это был единственный способ понять, к какому роботу
        # относится строка. С 11.10.2026 номер лежит в **своей колонке**
        # (`device_number` заполняется ниже, для каждой строки свой), и вторая
        # копия в тексте — ровно тот дубль, который владелец просил убрать.
        #
        # Оставляем строку **только** в запасном пути без колонок
        # (`include_optional=False`): там `device_number` не пишется, и без
        # пометки две строки одного отчёта стали бы неразличимы.
        row_details = list(details)

        if len(numbers) > 1 and not include_optional:
            row_details.insert(1, f"Device: {number}")

        # ─── Имена колонок единой таблицы `exceptions` ───────────────────────
        #
        # До 12.10.2026 строка собиралась под `exceptions_glpc`
        # (`error_robot`, `add_by`, `error_start_time`, `issue_data`,
        # `issue_warehouse`, `warehouse` названием). После перехода таблица
        # одна, и имена у неё другие — писать старые значило бы получить
        # `column ... does not exist` и потерять отчёт (именно так возникали
        # те 8 мусорных строк: старая схема уходила в новую таблицу).
        #
        # «Склад» здесь — **числовой id** (`warehouse_id(title)`), а не
        # название: в единой таблице это внешний ключ на `warehouses`.
        row = {
            # **Робот зарядки важнее станции** в этом поле: `robot_number` —
            # числовой номер робота. Если робота не спросили (старый путь или
            # API), остаётся прежнее поведение — разбор номера станции.
            "robot_number": _robot_number(charging_robot or number),
            "handle_by": card_id,
            "device_type": device_type,
            "employee": result.get("employee") or result.get("username") or f"Telegram {user_id}",
            "end_time": error_end.isoformat(),
            "start_time": received.isoformat(),
            "raw_fault": template_columns["raw_fault"],
            # **Описание ошибки из шаблона — но только при ТОЧНОЙ связи.**
            #
            # Требование владельца 12.10.2026: «на сервере в столбцы с описанием
            # ошибки что было сделано и тп». `raw_description` — та самая колонка
            # «问题描述 Issue Description» в выгрузке смены (`tk-new`,
            # `export-columns.ts`), и читатели предпочитают её справочнику
            # (`exception_rows.py`): снимок важнее ссылки.
            #
            # Поэтому при связи по `option_id` сюда встаёт **описание из
            # шаблона**, а сырой текст оператора остаётся в `reported_error` — он
            # никуда не девается и виден в разборе.
            #
            # **Почему не всегда.** При подборе по тексту уверенности нет, и
            # подставить описание «похожего» шаблона значило бы выдать догадку за
            # факт. Там поведение прежнее — сырой текст оператора.
            #
            # С 11.10.2026 склейки здесь **нет** ни в одном из случаев:
            # `reported_error` содержит текст оператора, а три остальных значения
            # уехали в свои колонки (`reported_type`, `device_number`,
            # `module_text`).
            "raw_description": linked_description or "\n".join(row_details) or None,
            "equipment_category": template_columns["equipment_category"],
            "template_id": template_columns["template_id"],
            "photo_url": result.get("photo_url") or None,
            "raw_recovery": template_columns["raw_recovery"],
            "raw_template_title": template_columns["raw_template_title"],
            "raw_solving_time": solving_time,
            # Суффикс только при нескольких устройствах: у обычного отчёта ключ
            # остаётся прежним, и уже записанные строки не теряют идентичность.
            "uniq_key": _uniq_key(result, report_id, user_id)
            + (f":{number}" if len(numbers) > 1 else ""),
            "shift_type": shift_name,
            "warehouse": warehouse_id(warehouse, fallback=None),
            "operating_day": shift_date,
            # Зона склада. У SMALL-P3 она выводится из устройства (`D`/`E`), у
            # GLP-C — `C2`. Пара (склад, зона) обязана быть в `warehouse_zones`:
            # на неё ссылается составной внешний ключ, и зона чужого склада
            # вставку уронит. Неизвестная пара даёт `None` — «зона неизвестна»,
            # а не догадку.
            #
            # Считается **по своему** номеру: у `3561/3528` зоны могут
            # различаться, и одна общая зона была бы неверна для второго робота.
            "zone": _allowed_zone(
                warehouse,
                device_zone(warehouse, charging_robot or number)
                or DEFAULT_ISSUE_WAREHOUSE,
            ),
            # Источник записи: приём оборудования через Telegram. Позволяет
            # отличить этот путь от API (`api`) и классического журнала
            # (`legacy`), когда путей станет больше двух.
            "source": "intake_bot",
        }

        if include_optional:
            row.update(
                _optional_fields(
                    answers, category, module, report_id, _unresolved_reason(card_id, result)
                )
            )
            # `device_number` — своё у каждой строки, а не общая строка с косой
            # чертой: иначе поиск по номеру робота не нашёл бы ни одну из них.
            row["device_number"] = number or None

        # **Каждый пункт — в своей колонке** (миграция
        # `0080_error_columns_not_text.sql`).
        #
        # Отдельный набор (`include_split`), а не общий с `include_optional`:
        # миграции разные, и раньше отсутствие `reported_*` снимало заодно
        # `cause` и прочую аналитику — дыра на **1663** строки живой базы.
        # Проверка здесь, а не в основном словаре: на базе без миграции вставка
        # с этими колонками упала бы, и отчёт потерялся бы целиком.
        if include_split:
            row["reported_error"] = reported_error
            row["reported_type"] = reported_type
            row["module_text"] = module_text

        # **Пометка «на разбор»** (`needs_review` + `review_reason`).
        #
        # Только когда оператор согласился сохранить номер, которого нет в
        # справочнике (решение владельца 12.10.2026). Отдельный набор колонок:
        # в единой таблице их пока нет, и писать их «на всякий случай» значило
        # бы уронить вставку целиком.
        if include_review:
            marked = _review_marked(result)

            row["needs_review"] = marked
            row["review_reason"] = (
                str(result.get("review_reason") or "").strip() or None
            )

        # **Ссылка на карточку робота** (`robot_id`).
        #
        # 🔴 Требование владельца 13.10.2026: «на сервере не храниться robot_id на
        # записях». Проверено на живой базе: из 275 записей бота он был заполнен у
        # **нуля** — писался только `robot_number`, а он повторяется между
        # складами, поэтому карточку робота по нему надёжно не открыть.
        #
        # Отдельный набор колонок: PostgREST отвергает запрос **целиком** из-за
        # одной неизвестной колонки, и общий набор уронил бы вместе с ней всю
        # аналитику (дефект 11.10.2026 — 1663 строки без `cause`).
        #
        # **Своё значение у каждой строки.** У отчёта про `3544/1234` это две
        # строки, и у каждой — id **своего** р��бота: общий id привязал бы вторую
        # ошибку к первому роботу.
        if include_robot:
            row["robot_id"] = robot_id_for(number, warehouse)

        rows.append(row)

    return rows


def _wire_payload(rows: list[Dict[str, Any]]) -> Any:
    """Тело запроса к PostgREST: объект для одного устройства, массив — для нескольких.

    **Почему не всегда массив.** У отчёта об одном устройстве (обычный случай)
    тело обязано остаться **тем же объектом**, что и раньше: `rest_post`
    отвечает на массив списком, а вся обработка ответа и логи рассчитаны на
    объект. Менять формат там, где форма не нужна, — риск без выгоды.

    **Почему массив, а не N запросов.** Разделённый отчёт (`3561/3528`) — это
    две строки, и они должны записаться **вместе**: PostgREST выполняет массив
    одной транзакцией, поэтому «половина отчёта» (ошибка учтена у одного робота
    и потеряна у другого) невозможна.
    """
    if len(rows) == 1:
        return rows[0]

    return rows


def _columns_available(
    table: str, columns: tuple, cache: str, checked_at_key: str,
    ttl: float = _OPTIONAL_TTL_SECONDS,
) -> bool:
    """
    Присутствует ли **конкретный** набор колонок в таблице?

    **Почему один набор, а не таблица целиком.** Наборы приезжают разными
    миграциями (`0075` — `cause`, `0080` — `reported_*`). Пока проверка была
    одна на все колонки, отсутствие **любой** из них отключало аналитику
    целиком: PostgREST отвечает `400`, если в `select` есть хотя бы одна
    неизвестная колонка.

    Проверка идёт дешёвым чтением одной строки, как это делает редактор дерева
    для своей v2-схемы: бот выкатывается **раньше**, чем применяется миграция,
    и пропавшая колонка — ожидаемое состояние, а не сбой.

    `None` неоднозначен (колонки нет **или** база недоступна). Второе
    выясняется обычным чтением и намеренно **не** кэшируется: сетевой сбой не
    должен снимать колонки со следующих 120 секунд отчётов.
    """
    from sendToDataBase import rest_get

    cached = globals().get(cache)

    if cached is not None and time.time() - globals()[checked_at_key] < ttl:
        return cached

    # `optional=True`: отсутствие колонок — ожидаемое состояние до миграции.
    rows = rest_get(
        table,
        {"select": ",".join(columns), "limit": "1"},
        optional=True,
    )

    if rows is None:
        reachable = rest_get(table, {"select": "id", "limit": "1"}) is not None

        if not reachable:
            # База недоступна: это не «колонок нет», и запоминать это нельзя.
            logger.warning(
                "intake journal: could not check %s in %s; filing with the full row",
                ", ".join(columns),
                table,
            )
            return True

        logger.warning(
            "intake journal: %s has no columns (%s) — filing reports without them",
            table,
            ", ".join(columns),
        )
        _remember_columns(cache, checked_at_key, False)

        return False

    _remember_columns(cache, checked_at_key, True)

    return True


def _remember_columns(cache: str, checked_at_key: str, value: bool) -> None:
    """Запомнить ответ проверки на короткий TTL (см. `_OPTIONAL_TTL_SECONDS`)."""
    globals()[cache] = value
    globals()[checked_at_key] = time.time()


def _analytics_columns_available() -> bool:
    """Есть ли **старый** набор аналитических колонок (миграции 0067–0075)."""
    return _columns_available(
        GLPC_TABLE,
        OPTIONAL_COLUMNS,
        "_optional_columns_state",
        "_optional_columns_checked_at",
    )


def _split_columns_available() -> bool:
    """Есть ли **новый** набор разложенных пунктов (миграция `0080`).

    Отдельная проверка — и есть исправление дыры: пока это читалось вместе со
    старыми колонками, одна неприменённая миграция снимала `cause` со всех
    отчётов, и причина снова оставалась только в JSON `answers`.
    """
    return _columns_available(
        GLPC_TABLE,
        SPLIT_COLUMNS,
        "_split_columns_state",
        "_split_columns_checked_at",
    )


def _review_columns_available() -> bool:
    """Есть ли набор пометки «на разбор» (`needs_review`, `review_reason`).

    Отдельная проверка по той же причине, что и у остальных наборов: в единой
    таблице этих колонок пока нет, и запрос с ними уронил бы **всю** вставку
    (PostgREST отвергает запрос целиком из-за одной неизвестной колонки). Пока
    колонок нет, пометка живёт в `unresolved_reason`.
    """
    return _columns_available(
        GLPC_TABLE,
        REVIEW_COLUMNS,
        "_review_columns_state",
        "_review_columns_checked_at",
    )


def _robot_columns_available() -> bool:
    """Есть ли ссылка на карточку робота (`robot_id`).

    Отдельная проверка по той же причине, что и у остальных наборов: запрос с
    неизвестной колонкой PostgREST отвергает **целиком**, и общий набор уронил бы
    вместе с ней всю аналитику (этот дефект уже был 11.10.2026).
    """
    return _columns_available(
        GLPC_TABLE,
        ROBOT_COLUMNS,
        "_robot_columns_state",
        "_robot_columns_checked_at",
    )


def reset_schema_cache() -> None:
    """Сброс памяти о колонках — только для тестов."""
    global _optional_columns_state, _optional_columns_checked_at
    global _split_columns_state, _split_columns_checked_at
    global _review_columns_state, _review_columns_checked_at
    global _robot_columns_state, _robot_columns_checked_at

    _optional_columns_state = None
    _optional_columns_checked_at = 0.0
    _split_columns_state = None
    _split_columns_checked_at = 0.0
    _review_columns_state = None
    _review_columns_checked_at = 0.0
    _robot_columns_state = None
    _robot_columns_checked_at = 0.0


def write_exception(result: Dict[str, Any], warehouse: Optional[str] = None) -> bool:
    """
    File one completed report in `exceptions`.

    Returns True when the row exists after the call (a repeated delivery counts
    as success — `uniq_key` makes the insert idempotent).

    Raises `WarehouseError` when the warehouse is unknown, so a mis-routed topic
    can never be filed under the default warehouse.

    Rollout safety: the analytics columns (`module`, `device_number`,
    `object_type`, `report_id`) arrive with a migration that may lag the deploy,
    and PostgREST rejects a whole insert when one of them is unknown. Each set is
    checked up front (see `_analytics_columns_available` / `_split_columns_available`),
    so the report is written once, with whatever columns actually exist, and the
    expected pre-migration state never reaches the logs as an error.

    **Запись идемпотентна и не теряет строки разделения.** Отчёт `3545/314/141`
    — это три строки с тремя своими `uniq_key`. Вставка массивом **без**
    разрешения конфликта роняла **всю** пачку с `409`, если хотя бы один ключ
    уже был в таблице, а `ignore_conflict` возвращал пустой список — то есть
    писатель докладывал «сохранено», хотя две из трёх строк не попали. Теперь
    пишем через upsert по `uniq_key`: каждая строка гарантированно
    записывается или обновляется, и «половина отчёта» невозможна.
    """
    title = validate_warehouse(warehouse if warehouse is not None else result.get("warehouse"))
    include_optional = _analytics_columns_available()
    include_split = _split_columns_available()
    include_review = _review_columns_available()
    include_robot = _robot_columns_available()

    from sendToDataBase import rest_upsert

    # **Все строки уходят одним запросом.**
    #
    # `build_rows` возвращает по строке на каждое устройство из номера (см.
    # `device_numbers`): `3561/3528` — две строки, у каждой свой `robot_number`
    # и свой `uniq_key`. PostgREST принимает массив и вставляет его одной
    # транзакцией: либо все строки, либо ни одной — «половина отчёта» записана
    # быть не может.
    #
    # Отправка по одной в цикле дала бы ту же атомарность только ценой N
    # запросов; здесь он всегда один.
    # **У одного устройства формат прежний — объект, а не массив из одного.**
    # Это не мелочь: `rest_post` с массивом отвечает списком, и вся
    # существующая обработка ответа рассчитана на объект. Массив там, где
    # всегда был объект, — изменение ради изменения.
    rows = build_rows(
        result, title,
        include_optional=include_optional,
        include_split=include_split,
        include_review=include_review,
        include_robot=include_robot,
    )
    # Номера устройств нужны для логов и переживают пересборку ниже.
    row_numbers = [str(row.get("device_number") or "?") for row in rows]
    payload = _wire_payload(rows)
    saved = rest_upsert(GLPC_TABLE, payload, on_conflict="uniq_key")

    # **Откат по набору колонок, а не «по всем сразу».**
    #
    # Проверка схемы только что ответила «колонки есть» и база доступна —
    # значит, отказ почти наверняка из-за колонки. Повтор без них дешёв и
    # спасает отчёт; негативный ответ запоминается, чтобы следующие отчёты не
    # платили за проверку. Наборы снимаются **по одному**: неприехавшая
    # миграция `0080` не должна заодно стирать `cause` (именно это и дало
    # 1663 пустые причины на живой базе).
    if saved is None and (include_optional or include_split or include_review or include_robot):
        logger.warning(
            "Write to %s failed with the analytics columns (%s); retrying without them",
            GLPC_TABLE,
            "+".join(
                name
                for name, present in (
                    ("optional", include_optional),
                    ("split", include_split),
                    ("review", include_review),
                    ("robot", include_robot),
                )
                if present
            ),
        )

        if include_optional:
            _remember_columns("_optional_columns_state", "_optional_columns_checked_at", False)

        if include_split:
            _remember_columns("_split_columns_state", "_split_columns_checked_at", False)

        if include_review:
            _remember_columns("_review_columns_state", "_review_columns_checked_at", False)

        if include_robot:
            _remember_columns("_robot_columns_state", "_robot_columns_checked_at", False)

        rows = build_rows(
            result, title,
            include_optional=False,
            include_split=False,
            include_review=False,
            include_robot=False,
        )
        row_numbers = [str(row.get("device_number") or "?") for row in rows]
        payload = _wire_payload(rows)
        saved = rest_upsert(GLPC_TABLE, payload, on_conflict="uniq_key")

    # Список устройств для логов. **Не** итерирую `payload`: у одного устройства
    # это объект, и перебор дал бы его ключи, а не строки.
    devices = ", ".join(str(number or "?") for number in row_numbers)

    if saved is None:
        logger.error(
            "Could not write report to %s: warehouse=%s, devices=%s",
            GLPC_TABLE,
            title,
            devices,
        )
        return False

    if len(row_numbers) > 1:
        # Запись о разделении: по одному номеру в логе нельзя понять, что отчёт
        # размножился, а это меняет число строк в отчётности.
        logger.info(
            "Report split across %d devices: %s",
            len(row_numbers),
            devices,
        )

    return True
