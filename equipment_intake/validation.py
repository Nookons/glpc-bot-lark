"""
Проверка номера устройства по типу — ДО записи отчёта.

**Зачем модуль.** Владелец 12.10.2026: «сейчас очень много некорректных данных
идёт через бота… главное правило что данные не должны быть битые». Проверено на
его выгрузке за октябрь: **103 невалидных номера из 2340**, среди них `'—'`,
`'Unable to drive'`, `'Driver component exception. 3528'`. То есть в поле номера
попадало **описание ошибки**, и строка журнала переставала быть связанной с
устройством.

**Два уровня строгости — так решил владелец.**

1. **Формат** (`validate_format`). Не подошёл по форме (текст вместо числа для
   робота) — это мусор: бот показывает понятную ошибку и **переспрашивает**. В
   поле номера не должно попадать описание ошибки.
2. **Справочник** (`lookup`). Формат верный, но устройства нет в справочнике —
   это **не мусор**: показать предупреждение («Robot 9999 is not in the
   registry. Save anyway?») и **дать сохранить**, помечая запись на разбор.
   Данные не теряются, но видно, что их надо проверить.

**Мягкая деградация.** Формат проверяется всегда — он не зависит от базы.
Проверка «есть в справочнике» работает **только когда таблица доступна**: нет
таблицы `map_shelves` / `map_locations` (их заливает отдельный агент) — `lookup`
возвращает `None`, и запись идёт без пометки, а не падает.

Термины совпадают с `report_writer` (`robot`, `charging`, `workstation`,
`qr_code`) — это категории справочника `equipment`.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Виды номера. `answer_kind` узла дерева ссылается на них.
KIND_ROBOT = "robot"
KIND_STATION = "station"
KIND_WORKSTATION = "workstation"
KIND_QR_FLOOR = "qr_floor"
KIND_QR_SHELF = "qr_shelf"

#: Категория справочника `equipment` для каждого вида номера. У QR своей
#: категории в `equipment` нет — места и полки лежат в `map_locations` /
#: `map_shelves`, поэтому там стоит `None`.
EQUIPMENT_CATEGORY = {
    KIND_ROBOT: "robot",
    KIND_STATION: "charging",
    KIND_WORKSTATION: "workstation",
    KIND_QR_FLOOR: None,
    KIND_QR_SHELF: None,
}

#: **Только ASCII-цифры.** `re.ASCII` здесь не украшение: без него `\d`
#: принимает и арабо-индийские (`٣٧٨٠`), и полноширинные (`１２３`) цифры, и
#: номер «٣٧٨٠» сошёл бы за верный. Тот же класс дефекта уже ловили на
#: `str.isdecimal()` (`report_writer._robot_number`) — здесь он у порога ввода.
_ASCII = re.ASCII

#: Формат номера робота. `H108` — документированный буквенно-цифровой вид
#: (`tree_config.py`: «For example: 3780 or H108/1834»).
ROBOT_PART = re.compile(r"^(?:\d+|H\d+)$", _ASCII)

#: Формат номера зарядной станции — только цифры.
STATION = re.compile(r"^\d+$", _ASCII)

#: Рабочая станция: буквенно-цифровой код склада (`C2-H143`, `209`,
#: `LT_MAP:POINT:...`). Разрешены буквы, цифры, `_`, `-`, `:`, `.`.
WORKSTATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$", _ASCII)

#: Код места на полу. Пример владельца: `SS512-020118-0101`,
#: допустим суффикс `_<слово>` (например `_A1`).
QR_FLOOR = re.compile(r"^SS\d{3}-\d{6}-\d{4}(_\w+)?$", _ASCII)

#: Код полки. Пример владельца: `Shelf-138-039-1`.
QR_SHELF = re.compile(r"^Shelf-\d{3}-\d{3}-\d$", _ASCII)

#: Таблицы справочника мест и полок: `(таблица, колонка для поиска)`.
#:
#: **Проверено на живой базе 12.10.2026** (справочник залил отдельный агент):
#: `map_shelves` — 15 562 полки, `map_shelf_places` — 1 369 475 мест.
#: Первоначально здесь стоял `map_locations` — это имя оказалось неверным, и
#: проверка молча деградировала бы: таблицы нет, значит «неизвестно», значит
#: номер никогда не помечался бы на разбор. Такое несоответствие ловится только
#: сверкой с живой базой, а не чтением кода.
#:
#: **Код места может нести суффикс** (`SS512-069045-0102_coop_haiflex`), а
#: `base_code` хранит чистый код места. Поэтому ищем по `base_code` — иначе
#: номер из наклейки не нашёлся бы при наличии суффикса.
QR_TABLES = {
    KIND_QR_FLOOR: ("map_shelf_places", "base_code"),
    KIND_QR_SHELF: ("map_shelves", "code"),
}


def _text(value) -> str:
    return str(value or "").strip()


def _parts(kind: str, value: str) -> list[str]:
    """
    Части номера, которые надо проверить.

    Для робота и станции `/` — **разделитель нескольких устройств**
    (требование владельца 08.10.2026: «4305/435 — это два отдельных девайса»).
    Для QR кода слэш запрещён: код места и полки содержит **дефисы**, а слэш
    означал бы ошибку оператора, а не второе устройство.
    """
    if kind in (KIND_QR_FLOOR, KIND_QR_SHELF):
        return [value]

    return [chunk.strip() for chunk in value.split("/") if chunk.strip()]


def validate_format(kind: str, value) -> Optional[str]:
    """
    Понятная человеку ошибка формата или `None`, если форма верна.

    Текст ошибки **по-английски** — это ответ оператору в боте.
    """
    text = _text(value)

    if not text:
        return "Enter the equipment number"

    value = text

    # Слэш в QR-коде — ошибка оператора, а не второе устройство: код места и
    # полки содержит дефисы (`SS512-020118-0101`, `Shelf-138-039-1`).
    if kind in (KIND_QR_FLOOR, KIND_QR_SHELF) and "/" in value:
        return (
            "A QR code never contains '/'. Use the code printed on the label, "
            "e.g. SS512-020118-0101 or Shelf-138-039-1"
        )

    parts = _parts(kind, value)

    if not parts:
        return "Enter the equipment number"

    for part in parts:
        if kind == KIND_ROBOT and not ROBOT_PART.match(part):
            return (
                "A robot number is digits only (or H followed by digits), "
                "e.g. 3780 or H108. Do not paste the error description here"
            )

        if kind == KIND_STATION and not STATION.match(part):
            return "A charging station number is digits only, e.g. 137"

        if kind == KIND_WORKSTATION and not WORKSTATION.match(part):
            return "Enter the workstation code, e.g. C2-H143"

        if kind == KIND_QR_FLOOR and not QR_FLOOR.match(part):
            return "Floor code looks like SS512-020118-0101"

        if kind == KIND_QR_SHELF and not QR_SHELF.match(part):
            return "Shelf code looks like Shelf-138-039-1"

    return None


def lookup(kind: str, warehouse: str, value) -> Optional[bool]:
    """
    Есть ли устройство в справочнике: `True` / `False` / `None` (неизвестно).

    `None` — **не ошибка и не «нет в справочнике»**: таблица недоступна или
    запрос не удался. Писать пометку «на разбор» в этом случае нельзя — она
    обвинила бы верный номер. Справочники полок и мест — отдельные таблицы
    (`map_shelves`, `map_shelf_places`), и если их ещё нет, проверка
    выключается, а не падает.
    """
    text = _text(value)

    if not text:
        return None

    parts = _parts(kind, text)

    if not parts:
        return None

    results = []

    for part in parts:
        found = _lookup_one(kind, warehouse, part)

        if found is None:
            # Одна неизвестная часть делает неизвестным весь ответ: частично
            # проверенный номер нельзя объявлять отсутствующим.
            return None

        results.append(found)

    return all(results)


def parts_with_lookup(kind: str, warehouse: str, value) -> List[Tuple[str, Optional[bool]]]:
    """
    Каждое устройство из ввода и результат его проверки по справочнику.

    🔴 **Зачем по отдельности.** В одной строке оператор называет несколько
    устройств: `3544/1234` — два робота, `3545/314/141` — три. Требование
    владельца 13.10.2026: «должно делить на двух роботов и проверять двух сразу
    и более».

    Раньше проверка отвечала одним `True`/`False` на всю строку, и в
    предупреждении оказывалась **вся** строка ввода: при `3544/1234` (робот
    `3544` есть, `1234` — нет) оператор видел `Robot 3544/1234` и не мог понять,
    который из двух не найден. Исправлять же надо ровно один.

    `None` в результате — «неизвестно» (справочник недоступен): это **не**
    «нет в справочнике», и обвинять верный номер из-за упавшего чтения нельзя.

    Для QR-кода список всегда из одного элемента: слэш в коде места или полки —
    ошибка оператора, а не второе устройство (`_parts`).
    """
    parts = _parts(kind, value)

    return [(part, _lookup_one(kind, warehouse, part)) for part in parts]


def _lookup_one(kind: str, warehouse: str, code: str) -> Optional[bool]:
    """
    Есть ли устройство в своём справочнике: `True` / `False` / `None`.

    **Отдельной проверки «существует ли таблица» здесь нет намеренно.**
    Отсутствующая таблица отвечает `404`, и `rest_get(optional=True)` возвращает
    `None` — ровно так же, как сетевой сбой. Оба случая означают одно:
    **«неизвестно»**, и пометку «на разбор» ставить нельзя. Так мягкая
    деградация получается сама, без второго обращения к базе и без зависимости
    от помощника `table_exists` (он появился позже, и его отсутствие в подмене
    модуля роняло проверку целиком).

    Различить «таблицы нет» и «строки нет» важно, и это различие сохранено:
    успешный ответ с пустым списком (`[]`) — это «строки нет», то есть номер
    действительно не в справочнике.
    """
    from sendToDataBase import rest_get

    if kind in QR_TABLES:
        table, column = QR_TABLES[kind]
        params = {"select": "id", "limit": "1", **{column: f"eq.{code}"}}
    else:
        table = "equipment"
        params = {
            "select": "id",
            "category": f"eq.{EQUIPMENT_CATEGORY[kind]}",
            "equipment_code": f"eq.{code}",
            "limit": "1",
        }

        if warehouse:
            params["warehouse"] = f"eq.{warehouse}"

    rows = rest_get(table, params, optional=True)

    if rows is None:
        # Таблицы нет или чтение не удалось — «неизвестно», а не «нет в справочнике».
        logger.info(
            "intake validation: справочник %s недоступен для %r — проверка пропущена",
            table,
            code,
        )
        return None

    return bool(rows)
