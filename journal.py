"""Единый журнал инцидентов для бота: таблица, имена колонок и адаптер формы.

**Зачем отдельный модуль.** До 12.10.2026 бот работал с таблицей
`exceptions_glpc` в четырёх местах (`sendToDataBase`, `analytics`,
`robot_card`, приём оборудования). Переход завершён: история перенесена
миграцией `0069` в `exceptions`, писатели API переведены туда же. Если бы
каждый читатель сам помнил новые имена колонок, они разошлись бы — ровно тот
класс дефектов, из-за которого уже дважды терялись данные (номер устройства,
`report_id`).

Здесь собрано в одном месте:

* **имя таблицы** — `EXCEPTIONS_TABLE`;
* **карта переименований** `column → legacy` — переход назвал колонки иначе
  (`robot_number` против `error_robot`, `warehouse` — числовым id вместо
  названия);
* **`legacy_row()`** — приводит строку новой таблицы к исторической форме,
  которую читает остальной код бота. Это позволяет перевести источник данных,
  **не переписывая** расчёты метрик: они по-прежнему видят `error_robot`,
  `issue_type` и `solving_time`.

**Почему форма, а не переименование кода.** Метрики бота (`shift_stats`,
`shift_report_data`, топы) читают исторические имена в десятке мест. Замена
имени в источнике и в потребителе — две правки вместо одной, и любая забытая
даёт пустую колонку молча. Адаптер оставляет потребителей как есть.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: Таблица инцидентов. Единственный источник: `exceptions_glpc` — архив
#: только для чтения (сохранён намеренно, см. план перехода в `tk-assist-api`).
EXCEPTIONS_TABLE = "exceptions"

#: Колонки, которые нужны читателям бота (смена, топы, карточка робота).
#: `raw_*` — снимок текста, который пишут перенос `0069` и ручка API;
#: `equipment_category` — категория (в legacy это было `issue_type`).
JOURNAL_COLUMNS = (
    "id, created_at, robot_number, handle_by, employee, device_type, "
    "start_time, end_time, equipment_category, raw_fault, raw_description, "
    "raw_recovery, raw_template_title, raw_solving_time, template_id, "
    "shift_type, warehouse, operating_day, zone, device_number, object_type, "
    "uniq_key, photo_url, module, module_text, reported_error, reported_type, cause"
)

#: Склад → числовой id. Проверено на живой базе 12.10.2026.
#:
#: **Зачем карта, а не запрос.** `exceptions.warehouse` — ссылка на
#: `warehouses`, а команды и топики приходят названием. Справочник складов не
#: меняется, поэтому обращение к базе на каждый отчёт было бы лишней задержкой
#: приёма. Неизвестного склада здесь нет намеренно: вызывающий код обязан
#: отказать, а не писать наугад (см. `report_writer.validate_warehouse`).
WAREHOUSE_IDS = {
    "GLP-C": 1,
    "SMALL-P3": 3,
}

#: Зоны, которые знает справочник `warehouse_zones`, по складам.
#: У GLP-C — `C2`; у SMALL-P3 зона выводится из устройства (`D`/`E`). Пара
#: `(склад, зона)` обязана быть здесь: на неё ссылается составной внешний ключ
#: `exceptions_zone_fkey`, и лишняя зона уронит вставку.
KNOWN_ZONES = {
    "GLP-C": ("C2",),
    "SMALL-P3": ("D", "E"),
}


def warehouse_id(title: Optional[str]) -> Optional[int]:
    """Название склада → id для колонки `exceptions.warehouse`."""
    return WAREHOUSE_IDS.get(str(title or "").strip())


def allowed_zone(warehouse: Optional[str], zone: Optional[str]) -> Optional[str]:
    """Зона, если пара (склад, зона) есть в справочнике; иначе `None`.

    Догадка запрещена: приписать ошибку зоне, где её не было, хуже, чем не
    знать зону. `None` — «зона неизвестна», и база такую строку принимает
    (`NULL` в составном ключе `MATCH SIMPLE` не проверяется).
    """
    known = KNOWN_ZONES.get(str(warehouse or "").strip())

    if not known or not zone:
        return None

    return zone if zone in known else None


def legacy_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Строка `exceptions` → историческая форма, которую читает код бота.

    Дополняет строку, **не удаляя** новые имена: они нужны тем местам, что уже
    перешли на них. Соответствие:

    | Новая колонка        | Историческое имя    |
    |----------------------|---------------------|
    | `robot_number`       | `error_robot`       |
    | `handle_by`          | `add_by`            |
    | `start_time`         | `error_start_time`  |
    | `end_time`           | `error_end_time`    |
    | `operating_day`      | `issue_data`        |
    | `zone`               | `issue_warehouse`   |
    | `equipment_category` | `issue_type`        |
    | `raw_fault`          | `first_column`      |
    | `raw_template_title` | `second_column`     |
    | `raw_description`    | `issue_description` |
    | `raw_recovery`       | `recovery_title`    |
    | `raw_solving_time`   | `solving_time`      |

    **`warehouse` здесь не меняется** на название: бот и так сравнивает склад
    с тем, что передал сам (из топика), а не читает его из строки. Обратный
    перевод id → название потребовал бы справочника там, где он не нужен.
    """
    result = dict(row)

    result["error_robot"] = row.get("robot_number")
    result["add_by"] = row.get("handle_by")
    result["error_start_time"] = row.get("start_time")
    result["error_end_time"] = row.get("end_time")
    result["issue_data"] = row.get("operating_day")
    result["issue_warehouse"] = row.get("zone")
    result["issue_type"] = row.get("equipment_category")

    # Текст: снимок `raw_*` приоритетен, справочник — запасной путь для строк,
    # записанных после перехода (`template_id` есть, снимка нет).
    template = row.get("template") or {}
    result["first_column"] = _first(row.get("raw_fault"), template.get("issue_sub_type"))
    result["second_column"] = _first(
        row.get("raw_template_title"), template.get("issue_sub_type")
    )
    result["issue_description"] = _first(
        row.get("raw_description"), template.get("issue_description")
    )
    result["recovery_title"] = _first(
        row.get("raw_recovery"), template.get("recovery_title")
    )
    solving = row.get("raw_solving_time")
    result["solving_time"] = (
        solving if solving is not None else template.get("solving_time")
    )

    return result


def legacy_rows(rows: Optional[Iterable[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """То же, что `legacy_row`, для списка. `None` остаётся `None`.

    Различие важно: `None` означает «чтение не удалось», а пустой список —
    «данных нет». Смешать их значило бы показать сбой как «смена без ошибок».
    """
    if rows is None:
        return None

    return [legacy_row(row) for row in rows]


def _first(*values: Any) -> Any:
    """Первое непустое значение: пустая строка — тоже «нет значения»."""
    for value in values:
        if value is not None and value != "":
            return value

    return None
