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

#: 🔴 **Источник для ОТЧЁТОВ — представление «только в работе».**
#:
#: Требование владельца 12.10.2026: «Вся экипировка что не в работе не должна
#: попадать в рапорты за смены». Правило живёт в представлении
#: `exceptions_in_work` (миграция `0107` в `tk-assist-api`) — **одно место для
#: всех отчётов**: веб-страница смены, PDF, Excel и этот бот.
#:
#: **Почему отдельная константа, а не замена `EXCEPTIONS_TABLE`.** Исходная
#: таблица нужна для **записи** (фото инцидента, новые с��роки) и для путей, где
#: важна **полная** история. Заменив её всюду, бот перестал бы видеть записи
#: выведенного из работы оборудования там, где они нужны. Поэтому отчётные
#: читатели берут эту константу, а писатели — прежнюю.
EXCEPTIONS_REPORT_TABLE = "exceptions_in_work"

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


#: Значения, которые НЕ являются типом ошибки, хотя лежат в полях ошибок.
#:
#: **Категории оборудования** (`equipment.category`: robot, charging,
#: workstation, conveyor). Проверено на живой базе 14.10.2026: у 272 строк
#: недели по GLP-C в `raw_fault` и `equipment_category` стоит `robot`, а
#: настоящая неисправность — в `reported_error` («Unable to rotate», «No
#: object»). Показать в топе «robot» значит ответить «что случилось?» словом
#: «робот».
#:
#: **Локализованная категория** `设备Equipment` — 14 277 строк из 24 674 (58 %
#: по GLP-C), плюс `环境Environment` и склейки вида `行走异常Unable to drive`.
#:
#: **Имена модулей** (`robot_module_*` в дереве приёма): у строк с
#: `raw_template_title` = «Chassis» ошибка лежит в `reported_error`
#: («Collision»). Модуль отвечает на «где», ошибка — на «что».
NOT_AN_ISSUE = (
    "设备", "环境", "Equipment", "Environment",
    "robot", "charging", "workstation", "conveyor", "qr_code",
    "Chassis", "Lifting", "Rotation", "Tray", "Horn", "Hook",
    "QR Code", "Safety", "Network", "Fingers",
)

#: Китайские префиксы в склейке вида `行走异常Unable to drive`.
ISSUE_PREFIXES = ("行走异常", "取放货异常", "驱动组件异常", "取放箱位置错误")


def meaningful_issue(row: Dict[str, Any]) -> str:
    """
    Тип ошибки из строки журнала — или `unknown`.

    🔴 **Одно правило на все команды и отчёты.** Дефект 14.10.2026 возник
    именно из-за копий: `/top` брал `issue_type`, рапорт смены — то же, и оба
    показывали **категорию оборудования** вместо неисправности. Когда правило
    живёт в одном месте, команды не могут разойтись между собой.

    **Порядок полей** — как у остальных читателей журнала: снимок (`raw_*`)
    важнее справочника, потому что правка заготовки не должна переписывать
    историю. `issue_type` (legacy-имя `equipment_category`) оставлен последним:
    у строк без снимка он хоть что-то значит.

    `unknown` — честный ответ: он виден в отчёте и показывает, что заготовка не
    заполнена. Подставить вместо него категорию значило бы выдать её за ошибку.
    """
    # 🔴 **Порядок: `raw_fault` ПЕРВЫМ, а `raw_template_title` — после.**
    #
    # Проверено на живых данных 14.10.2026, и это неочевидно: `raw_template_title`
    # хранит **название заготовки**, а не ошибку. У 262 строк GLP-C там стоит
    # `Unknown reason` — при том что настоящая ошибка лежит рядом в `raw_fault`
    # (`Problem Cannot located`). Показать «Unknown reason» вместо неё значит
    # скрыть то, что известно.
    #
    # Это же подтверждает **старый код**: он читал `issue_type`, а запасным путём
    # держал `first_column` — то есть `raw_fault` (см. `legacy_row`). Значит
    # `raw_fault` и был основным полем, а порядок я сначала перепутал.
    for field in ("raw_fault", "raw_template_title", "reported_error", "issue_type"):
        value = str(row.get(field) or "").strip()

        if not value:
            continue

        # Склейка «китайское слово + английский перевод»: берём английскую часть.
        for prefix in ISSUE_PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix):].strip() or value

        if value in NOT_AN_ISSUE or (
            any(mark in value for mark in NOT_AN_ISSUE) and len(value) <= 24
        ):
            continue

        return value

    return "unknown"


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


def last_issue_for_robot(
    robot_id: Any,
    warehouse: Any = None,
    *,
    limit: int = 1,
) -> Optional[str]:
    """
    Последняя **осмысленная** ошибка робота — или `None`.

    🔴 **Зачем (владелец 17.10.2026):** «когда робот офлайн находим его и берём
    его последнюю ошибку с сервера что была и предлагаем как причину для офлайна
    и если окей то пишем её как причину офлайна если нет то пользователь нажимает
    other и пишет сам».

    **Тип ошибки берётся тем же правилом, что и везде** — `meaningful_issue`.
    Своя копия логики здесь была бы третьей в проекте, а расхождение копий уже
    приводило к тому, что `/top` и рапорт смены показывали **категорию**
    оборудования вместо неисправности. Поэтому правило одно.

    **Склад обязателен.** Номера роботов повторяются между складами (`121`,
    `122`, `123` есть и в GLP-C, и в SMALL-P3), поэтому поиск только по номеру
    вернул бы ошибку **чужой машины** — и она попала бы в причину офлайна.
    Но здесь фильтруем по `robot_id`: это внешний ключ на
    `robots_maintenance_list`, он однозначен и склада не требует. Параметр
    `warehouse` оставлен для случая, когда id нет (старые записи).

    **`None` — честный ответ.** У четырёх роботов из одиннадцати офлайновых
    ошибок в журнале нет вовсе: предлагать им «последнюю ошибку» нечего, и
    выдумывать причину нельзя. Диалог в этом случае работает как раньше.

    Сбой чтения — тоже `None`: причина офлайна не то место, где стоит падать.
    """
    if robot_id in (None, "", 0):
        if not warehouse:
            return None
    else:
        try:
            robot_id = int(robot_id)
        except (TypeError, ValueError):
            return None

    try:
        from sendToDataBase import rest_get
    except ImportError:  # pragma: no cover — журнал без базы (тесты)
        return None

    # 🔴 **`issue_type` в списке колонок быть не должно.**
    #
    # Я добавил его по аналогии с `meaningful_issue` — и запрос **падал целиком**
    # (`PostgREST` отвечает `400` на неизвестную колонку, а `rest_get(...,
    # optional=True)` возвращает `None`). Снаружи это выглядело как «у робота нет
    # ошибок»: функция честно возвращала `None`, и **причина офлайна не
    # предлагалась ни разу**. Найдено проверкой на живых данных, а не тестом.
    #
    # В `exceptions` таких колонок нет: `issue_type` переименован в
    # `equipment_category` (см. `legacy_row`), а `raw_*` — снимок. Правило
    # `meaningful_issue` читает словарь **уже полученной строки**, и отсутствие
    # ключа там безопасно; в `select` его просить нельзя.
    params = {
        # 🔴 **`module` и `cause` — для описания офлайна.**
        #
        # Владелец 18.10.2026: «при выборе ошибка полностью сама переводила
        # робота в офлайн по данным с последней ошибки сама брала оттуда
        # описание». Описание собирается из трёх полей: тип ошибки, модуль
        # («где») и причина («почему»). Без них в заметку офлайна попадал бы
        # только тип, и читатель не понимал бы, где искать.
        #
        # Проверено на живых данных: `module` заполнен у 3603 записей из 4129,
        # `cause` — у 2622. Отсутствие любого из них не ошибка: описание
        # собирается из того, что есть, и недостающее не оставляет пустых
        # разделителей.
        "select": "raw_fault,raw_template_title,reported_error,start_time,module,cause",
        "order": "start_time.desc",
        "limit": str(max(1, int(limit))),
    }

    if robot_id not in (None, "", 0):
        params["robot_id"] = f"eq.{robot_id}"
    else:
        params["warehouse"] = f"eq.{warehouse}"

    rows = rest_get("exceptions", params, optional=True)

    if not rows:
        return None

    for row in rows:
        issue = meaningful_issue(row)

        # `unknown` — это «в журнале нечего взять», а не название ошибки.
        # Предложить его причиной значило бы показать оператору бессмыслицу.
        if issue and issue != "unknown":
            return issue

    return None


def last_issue_details_for_robot(robot_id: Any, warehouse: Any = None) -> Optional[Dict[str, Any]]:
    """
    Последняя ошибка робота **целиком**: тип, модуль, причина.

    🔴 **Зачем (владелец 18.10.2026):** «при выборе ошибка полностью сама
    переводила робота в офлайн по данным с последней ошибки сама брала оттуда
    описание и всё только фото или видео просила с возможностью пропустить».

    То есть при выборе последней ошибки бот больше **не спрашивает текст
    причины** — он берёт его из записи об ошибке. Здесь возвращается всё, что
    для этого нужно.

    Возвращает словарь:

    * `issue` — тип ошибки (правилом `meaningful_issue`, как везде);
    * `module` — модуль оборудования («где сломалось»), может быть `None`;
    * `cause` — причина («почему»), может быть `None`;
    * `note` — **готовое описание** для заметки офлайна: «Went off the road ·
      Chassis · Mechanical». Именно оно уходит в `problem_note`, поэтому
      собирается здесь, а не в трёх местах вызывающего кода.

    `None` — ошибок нет или все неразборчивы (`unknown`). Пустой `note`
    невозможен: если есть `issue`, он и станет текстом.

    **Модуль и причина добавляются только когда заполнены.** В данных `cause`
    есть у 2622 записей из 4129, `module` — у 3603. Пустые разделители («Went
    off the road · ·») читались бы как сбой вывода, а не как отсутствие данных.
    """
    if robot_id in (None, "", 0) and not warehouse:
        return None

    try:
        from sendToDataBase import rest_get
    except ImportError:  # pragma: no cover — журнал без базы (тесты)
        return None

    params = {
        "select": "raw_fault,raw_template_title,reported_error,start_time,module,cause",
        "order": "start_time.desc",
        "limit": "5",
    }

    if robot_id not in (None, "", 0):
        try:
            params["robot_id"] = f"eq.{int(robot_id)}"
        except (TypeError, ValueError):
            return None
    else:
        params["warehouse"] = f"eq.{warehouse}"

    rows = rest_get("exceptions", params, optional=True)

    if not rows:
        return None

    for row in rows:
        issue = meaningful_issue(row)

        if not issue or issue == "unknown":
            continue

        module = str(row.get("module") or "").strip()
        cause = str(row.get("cause") or "").strip()

        parts = [issue]
        if module:
            parts.append(module)
        if cause:
            parts.append(cause)

        return {
            "issue": issue,
            "module": module or None,
            "cause": cause or None,
            "note": " · ".join(parts),
        }

    return None
