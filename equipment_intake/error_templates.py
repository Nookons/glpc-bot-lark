# -*- coding: utf-8 -*-
"""
Заготовки `issue_templates` для ошибок модулей робота и точная связь с деревом.

**Зачем отдельный модуль.** Требование владельца 12.10.2026, дословно: «нужно
сделать **связь** как-то и при добавление новой ошибки делать **заготовку**
тоже чтобы потом были поля которые будут **подставлены автоматически при
копирование** и **на сервере в столбцы с описанием ошибки что было сделано и
тп»».

Проверено на живой базе 12.10.2026: **ни одной** из одиннадцати ошибок со схемы
владельца в справочнике `issue_templates` нет. Совпадало только слово `dirty`
(`QR Code dirty`), и по safety есть запись с **другим** смыслом
(`issue_type='Robot safety device triggered'`, `issue_sub_type='Security module
failure'`). То есть связь «ошибка дерева → шаблон» надо **создавать**, а не
находить; механизм заготовки — основной путь, а не вспомогательный.

**Какие ошибки модулей сюда входят** (схема владельца, 11 штук):

    CHASSIS   Went off the road
    LIFT      Can't lift up/down the fork
    ROTATE    Can't rotate fork
    TRAY      Can't extend/expand the tray
    HORN      Can't extend/expand the HORN
    HOOK      Can't extend/expand the Hook
    QR CODE   QR Code dirty
    SAFETY    Front emergency button is trigger
              Rear emergency button is trigger
              Safety bumper trigger
              Safety system disconnect

**Почему связь по `option_id`, а не по паре `equipment_type` + `issue_sub_type`.**
Уникальность на данных проверена, и естественный ключ справочника **не
уникален**:

| Кандидат в ключ справочника                 | Дублей |
|---|---|
| `equipment_type` + `issue_sub_type`         | **2** |
| `equipment_type` + `issue_type` + `issue_sub_type` | **1** |
| `employee_title`                            | 0 (29 записей) |

Две пары совпадают буквально: `Equipment` + `Network disconnection` и
`Equipment` + `Driver component exception`. Связь по ним дала бы ту же
неоднозначность, что и подбор по тексту, — ровно тот дефект, из-за которого
оператор писал «driver component exception», а в журнал попадало `Speed error`
(см. докстроку `template_match.py`). Поэтому связь держится **на id**:
`telegram_intake_options.template_id` → `issue_templates.id`, а этот модуль
задаёт, какому `option_id`/подписи какая заготовка соответствует.

**Почему карта в коде, а не только в базе.** Миграция `0109` заводит 11 заготовок
и строки-связи, но приём ошибок — боевой путь, и он не должен зависеть от того,
применена ли миграция и совпал ли `node_id` с тем, что выбрал слой дерева.
Карта ключей работает и **до** миграции: `option_id` берётся прямо из выбранного
пути (`Step.option_id`). База при этом остаётся авторитетной: если админ
связал вариант с **другим** шаблоном, побеждает связь из базы.

**Значения полей заготовок — не выдуманы.**

* `employee_title` и `issue_sub_type` — дословные тексты схемы владельца;
* `issue_description` — тот же текст (описание ошибки для журнала), потому что
  другого владелец не давал, а придумывать формулировку нельзя;
* `issue_type` — **модуль** (`Chassis`, `Lift`, `Rotate`, `Tray`, `Horn`,
  `Hook`, `QR Code`, `Safety`): так ошибки группируются по модулям, а не по
  категории оборудования. У ошибки модуля категория всегда одна;
* `equipment_type` — `Equipment`. В справочнике три значения: `Equipment` (15
  записей: робот и его механизмы), `Environment` (8: пол, полки, DM-коды) и
  `Operation` (6: действия оператора). Ошибки модулей робота — это поломка
  **самого робота**, поэтому `Equipment`. Значение проверено по `tk-new`
  (`dashboard/templates/page.tsx` группирует по `equipment_type`, «Other» для
  пустого) и по `daily_report_service.py`;
* `recovery_title` = `NULL` («что было сделано») и `solving_time` = `NULL` —
  **намеренно не заполнены**. Время решения в справочнике 2–8 минут, и
  выдуманное число испортило бы метрику «сколько решается ошибка»; заполняет
  человек через `/tree` или страницу шаблонов. Пустое значение честнее догадки
  (то же правило, что у подбора по тексту: лучше пустая колонка, чем неверная).

🔴 **`node_id` может отличаться от ожидаемого.** Контракт слоя дерева —
  узлы `robot_errors_<module>` с опциями вида `chassis_went_off_road`. Совпадение
  подписи проверяется **точно** (без нечёткого сравнения) и только на узлах
  `robot_errors_*`: `ask_cause` тоже содержит вариант «Went off the road», и
  привязывать причину к шаблону ошибки модуля по одной лишь подписи нельзя.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Префикс узлов слоя ошибок (контракт владельца: `robot_errors_<module>`).
ERROR_NODE_PREFIX = "robot_errors"

#: Как назвать модуль в колонке `issue_type`. Ключ — хвост `node_id` или
#: канонический модуль из карты ниже.
MODULE_TITLES = {
    "chassis": "Chassis",
    "lift": "Lift",
    "rotate": "Rotate",
    "tray": "Tray",
    "horn": "Horn",
    "hook": "Hook",
    "qr_code": "QR Code",
    "safety": "Safety",
}

#: Справочник заготовок. `labels` — дословные тексты владельца (по ним ищем
#: вариант в дереве, если `option_id` другой); `option_ids` — канонические id
#: плюс разумные синонимы, которые может выбрать слой дерева.
#:
#: `option_ids` перечисляет и короткие формы (`went_off_the_road`) — ровно такой
#: вариант **уже есть** в живом дереве на узле `ask_cause`, и слой ошибок может
#: переиспользовать то же имя.
_TEMPLATE_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "module": "chassis",
        "option_ids": ("chassis_went_off_road", "went_off_the_road"),
        "label": "Went off the road",
    },
    {
        "module": "lift",
        "option_ids": ("lift_cant_lift", "cant_lift_up_down_the_fork", "lifting"),
        "label": "Can't lift up/down the fork",
    },
    {
        "module": "rotate",
        "option_ids": ("rotate_cant_rotate", "cant_rotate_fork", "rotation"),
        "label": "Can't rotate fork",
    },
    {
        "module": "tray",
        "option_ids": ("tray_cant_extend", "cant_extend_expand_the_tray"),
        "label": "Can't extend/expand the tray",
    },
    {
        "module": "horn",
        "option_ids": ("horn_cant_extend", "cant_extend_expand_the_horn"),
        "label": "Can't extend/expand the HORN",
    },
    {
        "module": "hook",
        "option_ids": ("hook_cant_extend", "cant_extend_expand_the_hook"),
        "label": "Can't extend/expand the Hook",
    },
    {
        "module": "qr_code",
        "option_ids": ("qr_code_dirty", "qr_code_dirty_option"),
        "label": "QR Code dirty",
    },
    {
        "module": "safety",
        "option_ids": ("safety_front_button", "front_emergency_button_is_trigger"),
        "label": "Front emergency button is trigger",
    },
    {
        "module": "safety",
        "option_ids": ("safety_rear_button", "rear_emergency_button_is_trigger"),
        "label": "Rear emergency button is trigger",
    },
    {
        "module": "safety",
        "option_ids": ("safety_bumper", "safety_bumper_trigger"),
        "label": "Safety bumper trigger",
    },
    {
        "module": "safety",
        "option_ids": ("safety_system_disconnect", "safety_system_disconnect_option"),
        "label": "Safety system disconnect",
    },
)


def _normalize(value: Any) -> str:
    """Подпись для точного сравнения: регистр и лишние пробелы не важны."""
    return " ".join(str(value or "").split()).casefold()


def template_specs() -> Tuple[Dict[str, Any], ...]:
    """Одиннадцать заготовок (для миграции, тестов и редактора)."""
    return _TEMPLATE_SPECS


def seed_for(module: str) -> Optional[Dict[str, Any]]:
    """Заготовка по имени модуля (первая, если на модуль их несколько)."""
    wanted = _normalize(module)

    for spec in _TEMPLATE_SPECS:
        if spec["module"] == wanted or _normalize(MODULE_TITLES.get(spec["module"], "")) == wanted:
            return spec

    return None


def seed_for_option(option_id: Any, label: Any = None, node_id: Any = None) -> Optional[Dict[str, Any]]:
    """
    Заготовка, которой соответствует вариант дерева, или `None`.

    Порядок: сначала **точный** `option_id`, затем — **точная** подпись. Нечёткого
    сравнения здесь нет намеренно: это связь, а не подбор, и «почти совпало»
    вернуло бы ту самую неоднозначность.

    🔴 **`option_id` проверяется только на узле слоя ошибок.** Это не
    перестраховка: в живом дереве на `ask_cause` уже есть вариант
    `went_off_the_road` — но это **причина** («почему это случилось»), а не
    ошибка модуля. Без ограничения он связался бы с заготовкой «Went off the
    road», и в журнал уезжал бы `issue_type = Chassis` у причины, к шасси
    отношения не имеющей. Проверено запуском: без ограничения
    `seed_for_option('went_off_the_road')` возвращал заготовку шасси.
    """
    # Подпись — второй ключ: слой дерева мог назвать id иначе, но текст владельца
    # фиксирован.
    if not is_error_node(node_id):
        return None

    wanted_id = str(option_id or "").strip()

    if wanted_id:
        for spec in _TEMPLATE_SPECS:
            if wanted_id in spec["option_ids"]:
                return spec

    wanted_label = _normalize(label)

    if not wanted_label:
        return None

    for spec in _TEMPLATE_SPECS:
        if _normalize(spec["label"]) == wanted_label:
            return spec

    return None


def is_error_node(node_id: Any) -> bool:
    """Узел слоя ошибок? (`robot_errors_chassis`, `robot_errors_lift`, …)."""
    return str(node_id or "").startswith(ERROR_NODE_PREFIX)


def module_from_node(node_id: Any) -> str:
    """
    Имя модуля из id узла: `robot_errors_qr_code` → `QR Code`.

    Пусто — узел не похож на узел слоя ошибок. Тогда `issue_type` заготовки
    остаётся пустым: приписать ошибку чужому модулю хуже, чем не приписать.
    """
    if not is_error_node(node_id):
        return ""

    tail = str(node_id)[len(ERROR_NODE_PREFIX):].strip("_").casefold()

    return MODULE_TITLES.get(tail, "")


def stub_row(label: str, module: str = "", created_by: Any = None) -> Dict[str, Any]:
    """
    Строка заготовки `issue_templates` для новой ошибки.

    Заполняются только те поля, что **известны** на момент добавления: название
    (его ввёл человек) и модуль (он следует из узла, на котором добавлен
    вариант). Остальное — `NULL`: «что было сделано» и время решения знает
    только человек, а выдуманное время испортило бы метрику.

    `issue_description` = название: это и есть описание ошибки словами владельца,
    и именно оно подставляется в журнал при выборе ошибки.
    """
    name = str(label or "").strip()

    return {
        "employee_title": name or None,
        "issue_type": str(module or "").strip() or None,
        "issue_sub_type": name or None,
        "issue_description": name or None,
        "recovery_title": None,
        "solving_time": None,
        "equipment_type": "Equipment",
        "created_by": created_by,
    }


def resolve(templates: Any, option_id: Any = None, label: Any = None,
            node_id: Any = None, template_id: Any = None) -> Optional[Dict[str, Any]]:
    """
    Шаблон справочника по точной связи, или `None`.

    Приоритет — у ссылки из базы (`template_id` строки варианта): админ мог
    связать вариант с **другим** шаблоном, и его выбор сильнее карты в коде.
    Затем — карта `option_id`/подписи, и уже по ней находится строка
    справочника (по `employee_title`, единственному уникальному полю: 0 дублей
    на 29 записях).

    Ни одно из сравнений не является нечётким: неоднозначная связь не лучше
    отсутствующей.
    """
    rows = [row for row in (templates or []) if isinstance(row, dict)]

    if template_id not in (None, ""):
        for row in rows:
            if str(row.get("id")) == str(template_id):
                return row

    spec = seed_for_option(option_id, label, node_id)

    if spec is None:
        return None

    wanted = _normalize(spec["label"])

    for row in rows:
        if _normalize(row.get("employee_title")) == wanted:
            return row

    return None


def linked_template(steps: Any, templates: Any,
                    option_links: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Шаблон, **точно** связанный с выбранной в дереве ошибкой.

    Путь просматривается с конца: последняя выбранная ошибка — та, что описывает
    отчёт. У зарядки в пути есть и станция, и робот, поэтому «последний
    подходящий», а не «первый».

    `option_links` — карта `option_id → template_id` из
    `telegram_intake_options.template_id` (ссылка, выставленная в редакторе).
    """
    links = option_links or {}

    for step in reversed(list(steps or [])):
        if not isinstance(step, dict):
            continue

        option_id = step.get("option_id")

        if not option_id:
            continue

        found = resolve(
            templates,
            option_id=option_id,
            label=step.get("label"),
            node_id=step.get("node_id"),
            template_id=links.get(str(option_id)),
        )

        if found is not None:
            return found

    return None


def option_seeds() -> List[Tuple[str, str, str]]:
    """
    Пары для миграции: `(option_id, label, employee_title)`.

    `employee_title` — ключ поиска заготовки в справочнике; он же текст владельца.
    """
    return [
        (spec["option_ids"][0], spec["label"], spec["label"])
        for spec in _TEMPLATE_SPECS
    ]
