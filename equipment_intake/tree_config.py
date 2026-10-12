"""
Дерево решений производственного приёма ошибок.

Путь сотрудника — выбор категории, затем конкретики, затем текстовые шаги:

    Object → Device number → Type → Module → Error → Cause → Summary
                                                          └ Other → Description

    Robot → номер робота → K50H | A42T | A42T C2 → модуль модели →
    **ошибка модуля** → причина
    Workstation      — временно закрыто (`UNAVAILABLE`-узлы)
    Charging station — временно закрыто
    QR Code → Shelf | Floor → номер → причина

**Почему номер спрашивается сразу после категории (схема владельца 12.10.2026).**
Владелец, дословно: «после фото **не спрашивает сразу номер робота** и потом
**не проверяет его наличие на сервере**». На его схеме «Ask device number» стоит
**сразу после** выбора категории, а «Check device number on server» — **между**
номером и моделью. Отсюда порядок выше: сначала номер, затем модель и модуль.

**Описание ошибки спрашивается только для `Other`.** Владелец, дословно:
«убери вопросы об описание ошибки оно нужно **только если other выберет**».
Свободный текст остаётся в дереве (`ask_description`), но обычный путь на него
не ведёт: он открывается **только** после причины `Other`.

Почему так (по требованию заказчика):
  * сотруднику нужно выбрать всего две вещи — что это и какая разновидность;
  * **у каждой модели робота свой набор модулей, а у каждого модуля — свой
    набор ошибок** (схема владельца 12.10.2026);
  * уникальный номер оборудования вводится руками: список техники меняется,
    закрыть его кнопками нельзя;
  * описание — свободным текстом. Именно эти описания потом собираются и
    превращаются в готовые варианты ошибок (см. `data/ANALYSIS.md` — разбор
    реальной выгрузки и `tools/build_tree_from_errors.py`).

**Слой ошибок — новое здесь.** До 12.10.2026 модуль вёл сразу на номер
устройства: в дереве было 13 узлов и **ни одного узла ошибок**, а что именно
сломалось, оператор описывал только свободным текстом. Теперь ошибка — это
выбор из кнопок, а свободный текст остаётся дополнительным уточнением.

Дерево задано кодом осознанно: это зафиксированная бизнес-логика, а не
догадка. Расширять просто — добавить вариант в нужный узел.

`load_tree()` дополнительно принимает дерево из файла, строки JSON или URL —
на случай, когда структура поедет из backend или админки.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .types import (
    DecisionTree,
    Node,
    NodeType,
    choice_node,
    opt,
)
from .validation import (
    KIND_QR_FLOOR,
    KIND_QR_SHELF,
    KIND_ROBOT,
    KIND_STATION,
    KIND_WORKSTATION,
)



# ============================================================
# ПОСТРОЕНИЕ УЗЛОВ
# ============================================================

# Следующий шаг после выбора разновидности — всегда один и тот же:
# спросить номер оборудования, затем описание.
ASK_NUMBER = "ask_number"
ASK_DESCRIPTION = "ask_description"
#: **Второй номер зарядной ветки — робот, который стоял на станции.**
#:
#: Решение владельца 12.10.2026, дословно: «если зарядка то сначала спрашиваем
#: зарядку потом номер робота и храним данные на сервере корректно». В запись
#: идут **оба**: станция в `device_number`, робот в `robot_number` (историческое
#: `error_robot`). До этой правки ветка спрашивала **один** номер, и на вопрос
#: «Which charging station?» оператор отвечал номером робота — ровно тот класс
#: дефекта, из-за которого в `device_number` QR-строк оказались `3492` и `2668`.
#:
#: **Сейчас узел в дереве не стоит.** Решение владельца 12.10.2026: «Workstation
#: и Charging Station ПОКА НЕ РАБОТАЮТ». Ветка зарядки убрана из дерева целиком,
#: иначе её узлы стали бы недостижимыми и валидатор справедливо упал бы. Сама
#: машинерия сохранена — `validation` по-прежнему знает вид `station`, а
#: `report_writer` пишет станцию и робота в две колонки: включить ветку обратно
#: значит вернуть её варианты и этот узел, а не писать логику заново.
ASK_CHARGING_ROBOT = "ask_charging_robot"
#: Вопрос о причине ошибки — **владелец: «добавить причину ошибки как ещё один
#: вопрос»**. Отвечает на «почему это случилось», а не «что сломалось»:
#: описание проблемы и её причина — разные вещи, и раньше причину не спрашивали
#: вовсе, поэтому в отчётах её не было.
ASK_CAUSE = "ask_cause"
SUMMARY = "summary"

#: **Тексты «ветка пока не работает».**
#:
#: Решение владельца 12.10.2026: Workstation и Charging Station «ПОКА НЕ
#: РАБОТАЮТ» — выбор такой категории обязан **сразу** сказать об этом и **не
#: начинать диалог**. Текст по-английски (язык бота), вежливый и с указанием,
#: что делать дальше: молчаливая кнопка или отказ без объяснения заставили бы
#: оператора гадать, сломался бот или нет.
#:
#: Узел имеет тип `UNAVAILABLE`: вариантов у него нет, текст не принимается,
#: кнопка «Confirm» не рисуется, а `flow._confirm` отказывает — то есть запись
#: в журнал из этой ветки невозможна.
WORKSTATION_UNAVAILABLE = (
    "⚠️ Workstations are not available yet.\n\n"
    "Please tap Cancel, send the photo again and choose Robot for now."
)
CHARGING_UNAVAILABLE = (
    "⚠️ Charging stations are not available yet.\n\n"
    "Please tap Cancel, send the photo again and choose Robot for now."
)

#: id узлов «ветка закрыта» — на них ведут варианты корня.
WORKSTATION_UNAVAILABLE_NODE = "workstation_unavailable"
CHARGING_UNAVAILABLE_NODE = "charging_unavailable"


# ============================================================
# МОДУЛИ И ОШИБКИ — СХЕМА ВЛАДЕЛЬЦА 12.10.2026
# ============================================================
#
# **Владелец дал схему:** под каждой моделью робота — **свой** список модулей,
# а под каждым модулем — **свой** список ошибок. Здесь это и зафиксировано.
#
# **Что было и почему это главная правка.** До 12.10.2026 модуль вёл **сразу на
# номер устройства**: в дереве было 13 узлов и **ни одного узла ошибок**. То есть
# «что именно сломалось» оператор описывал только свободным текстом, и по этому
# тексту потом подбирался шаблон. Теперь ошибка — выбор из кнопок (слой
# `robot_errors_*`), а свободное описание осталось **дополнением** к ней.
#
# **id узлов ошибок — контракт:** `robot_errors_<module>`, где `<module>` —
# нижним регистром латиницей (`robot_errors_qr_code`). **id вариантов** —
# `<module>_<slug>` (`chassis_went_off_road`, `safety_front_button`). По ним
# третий слой (заготовки `issue_templates`) связывает ошибку дерева с шаблоном
# автоматически, поэтому они **не переименовываются** произвольно.
#
# **Почему дубли со схемы убраны.** В списках A42T и A42T C2 `ROTATE` и
# `QR CODE` написаны дважды — это ошибка рисования, а не два разных модуля:
# физически поворот и код один. Оставлено по одному, а порядок приведён к
# «сначала основные модули, потом QR CODE и SAFETY»: так оба списка читаются
# одинаково и совпадают с колонкой A42T C2 на схеме.

#: Ошибки каждого модуля: `module → ((option_id, label), …)`.
#:
#: Тексты — **дословно со схемы владельца** (английский, включая его написание
#: `trigger` вместо `triggered` и регистр `HORN`/`Hook`). Переписывать их
#: «правильнее» нельзя: по этим строкам строится связь с `issue_templates`, и
#: расхождение в букве разорвало бы её.
MODULE_ERRORS = {
    "chassis": (
        ("chassis_went_off_road", "Went off the road"),
    ),
    "lift": (
        ("lift_cant_lift", "Can't lift up/down the fork"),
    ),
    "rotate": (
        ("rotate_cant_rotate", "Can't rotate fork"),
    ),
    "tray": (
        ("tray_cant_extend", "Can't extend/expand the tray"),
    ),
    "horn": (
        ("horn_cant_extend", "Can't extend/expand the HORN"),
    ),
    "hook": (
        ("hook_cant_extend", "Can't extend/expand the Hook"),
    ),
    "qr_code": (
        ("qr_code_dirty", "QR Code dirty"),
    ),
    "safety": (
        ("safety_front_button", "Front emergency button is trigger"),
        ("safety_rear_button", "Rear emergency button is trigger"),
        ("safety_bumper", "Safety bumper trigger"),
        ("safety_system_disconnect", "Safety system disconnect"),
    ),
}

#: Как модуль называется в заголовке своего узла ошибок. Ключ — хвост
#: `robot_errors_<module>`; это же имя читает редактор `/tree`.
ERROR_MODULE_TITLES = {
    "chassis": "Chassis",
    "lift": "Lift",
    "rotate": "Rotate",
    "tray": "Tray",
    "horn": "Horn",
    "hook": "Hook",
    "qr_code": "QR Code",
    "safety": "Safety",
}

#: Модули каждой модели: `node_id → ((option_id, label, error_module), …)`.
#:
#: **Порядок вариантов — это порядок из схемы владельца**, он же порядок кнопок.
#: `error_module` — хвост узла `robot_errors_<module>`, куда ведёт модуль.
#:
#: **Почему id вариантов, а не подписи.** id попадает в оверлей редактора
#: (`telegram_intake_options`), и его смена осиротила бы уже сохранённые правки.
#: Где id существовал, он сохранён: `chassis`, `safety`, `lifting` — и добавлен
#: `rotation` (прежний id поворота у A42T).
#:
#: **K50H — другой набор модулей**, и это не мелочь: у него есть `Lifting`, но
#: нет `Lift`/`Rotate`/`Tray`/`Horn`/`Hook`. Ошибки общих модулей (Chassis,
#: QR Code, Safety) — те же, что у A42T; `Lifting` несёт ошибку `Lift`
#: («Can't lift up/down the fork»).
ROBOT_MODULES = {
    "robot_module_k50h": (
        ("chassis", "Chassis", "chassis"),
        ("qr_code", "QR Code", "qr_code"),
        ("safety", "Safety", "safety"),
        ("lifting", "Lifting", "lift"),
    ),
    "robot_module_a42t": (
        ("chassis", "Chassis", "chassis"),
        ("lifting", "Lifting", "lift"),
        ("rotation", "Rotation", "rotate"),
        ("tray", "Tray", "tray"),
        ("horn", "Horn", "horn"),
        ("hook", "Hook", "hook"),
        ("qr_code", "QR Code", "qr_code"),
        ("safety", "Safety", "safety"),
    ),
    #: **A42T C2 — отдельная ветка**, а не слияние с A42T (решение владельца
    #: 12.10.2026). Список модулей совпадает с A42T, но узел свой: иначе в
    #: отчёте нельзя было бы отличить C2 от A42T и правки редактора для одной
    #: модели меняли бы другую.
    "robot_module_a42t_c2": (
        ("chassis", "Chassis", "chassis"),
        ("lifting", "Lifting", "lift"),
        ("rotation", "Rotation", "rotate"),
        ("tray", "Tray", "tray"),
        ("horn", "Horn", "horn"),
        ("hook", "Hook", "hook"),
        ("qr_code", "QR Code", "qr_code"),
        ("safety", "Safety", "safety"),
    ),
}

#: Прежние модули, **удалённые совсем** (не скрытые) по решению владельца
#: 12.10.2026. Список оставлен как справка и для сторожа тестов: в дереве их
#: быть не должно. История журнала при этом не трогается — `Not a module` (42
#: записи), `Personally` (12), `Bummer` (11) остаются в базе как есть.
REMOVED_MODULES = (
    "not_a_module",
    "personally",
    "bummer",
    "ground_camera",
    "lidar",
    "fingers",
)


def error_node_id(module: str) -> str:
    """
    id узла ошибок модуля: `chassis` → `robot_errors_chassis`.

    **Контракт имён.** Так узел называется и в дереве, и в редакторе `/tree`, и
    в связке с заготовками `issue_templates` (`error_templates.is_error_node`
    проверяет тот же префикс). Одна функция на все места — чтобы префикс нельзя
    было случайно написать по-разному в дереве и в проверке.
    """
    return f"robot_errors_{module}"


def _model_title(module_node_id: str) -> str:
    """
    Название модели для заголовка узла модулей.

    `robot_module_a42t_c2` → `A42T C2`: хвост id переводится в верхний регистр,
    а разделитель `_` возвращается в пробел. Простое `upper()` дало бы
    `A42T_C2` — в заголовке редактора это читается как опечатка, а заголовок
    нужен именно для того, чтобы модели различались глазами.
    """
    return module_node_id.replace("robot_module_", "").replace("_", " ").upper()


def _build_nodes() -> dict:
    nodes = {}

    def add(node: Node) -> None:
        nodes[node.id] = node

    # ------------------------------------------------------------
    # Шаг 1: что за оборудование
    # ------------------------------------------------------------
    add(choice_node(
        "root",
        "What is shown in the photo?",
        key="object",
        summary_label="Object",
        description="Choose what is in the photo.",
        options=(
            # **Номер спрашивается СРАЗУ после категории** — схема владельца
            # 12.10.2026 и его замечание: «после фото не спрашивает сразу номер
            # робота». До этой правки вариант вёл на `robot_type`, и номер
            # оказывался в самом конце пути.
            opt("robot", "Robot", ASK_NUMBER),
            # **Workstation и Charging station — «пока недоступно».** Вариант
            # ведёт прямо на узел-заглушку: диалог по шагам не начинается, и
            # сразу видно, что делать вместо этого. Раньше здесь были
            # `workstation_type` и `charger_type` — они убраны вместе с ветками.
            opt("workstation", "Workstation", WORKSTATION_UNAVAILABLE_NODE),
            opt("charger", "Charging station", CHARGING_UNAVAILABLE_NODE),
            opt("qr", "QR Code", "qr_type"),
        ),
    ))

    # ------------------------------------------------------------
    # Шаг 2: разновидность внутри выбранного оборудования
    # ------------------------------------------------------------
    # Порядок моделей и их списки модулей — **точно по схеме владельца**.
    #
    # **A42T C2 ведёт в свой узел** (решение владельца 12.10.2026): до этого он
    # шёл в общий с A42T узел, и в отчёте нельзя было понять, какая это модель.
    add(choice_node(
        "robot_type",
        "Which robot?",
        key="device_type",
        summary_label="Type",
        description="Choose the robot model.",
        options=(
            opt("k50h", "K50H", "robot_module_k50h"),
            opt("a42t", "A42T", "robot_module_a42t"),
            opt("a42t_c2", "A42T C2", "robot_module_a42t_c2"),
        ),
    ))

    # ------------------------------------------------------------
    # Модули робота — **отдельный узел на каждую модель**
    # ------------------------------------------------------------
    #
    # **Владелец 07.10.2026:** «нужно как-то сделать модуля отдельно на роботов,
    # потому что например на K50H мы имеем только 3 модуля это lift, chassis and
    # safety, больше нету — и нету смысла показывать все в таком случае».
    #
    # **Почему отдельные узлы, а не один с `only_for`.** Механизм `only_for` в
    # дереве есть и работает (`visible_options` фильтрует варианты по пути), но
    # валидатор запрещает **одинаковые id вариантов внутри узла** — а `lifting`
    # нужен и K50H, и A42T. Отдельные узлы снимают это ограничение честно:
    # у каждой модели свой список модулей, и его правит редактор независимо.
    # Побочно это точнее отвечает на вопрос «какие модули у этой модели».
    #
    # Идентификатор варианта остаётся **человеческим** (`lifting`, `chassis`):
    # он попадает в отчёт и в статистику, поэтому префикс модели к нему не
    # добавляю — иначе в отчётах появились бы `k50h_lifting` и `a42t_lifting`
    # как разные модули, хотя это один и тот же узел железа.
    # **Заголовок называет модель — иначе узлы не различить.**
    #
    # Дефект, найденный владельцем 07.10.2026: «не понятно как настроить модули
    # на роботах, текста одинаковые». Разделив модули по моделям, я дал обоим
    # узлам **один и тот же** заголовок «Which robot module?», и в редакторе
    # (`/tree`) они выглядели как две одинаковые строки, различимые только по
    # техническому id в скобках.
    #
    # Теперь заголовок отвечает на вопрос «какие модули у **этой** модели» и
    # работает как подпись в списке редактора.
    # **Старые модули удалены совсем**, а не скрыты: `Not a module`,
    # `Personally`, `Bummer`, `Ground camera`, `Lidar`, `Fingers` (решение
    # владельца 12.10.2026). Записи в журнале при этом не трогаются — 65 штук
    # (`Not a module` 42, `Personally` 12, `Bummer` 11) остаются историей;
    # убраны только кнопки дерева. Список для сторожа — `REMOVED_MODULES`.
    for module_node_id, module_options in ROBOT_MODULES.items():
        model = _model_title(module_node_id)

        add(choice_node(
            module_node_id,
            f"{model}: which module?",
            key="module",
            summary_label="Module",
            description=(
                "Choose the faulty module of this robot. "
                "The error is chosen on the next step."
            ),
            options=tuple(
                opt(option_id, label, error_node_id(error_module))
                for option_id, label, error_module in module_options
            ),
        ))

    # ------------------------------------------------------------
    # Шаг 2в: **ошибки модуля** — новый слой дерева
    # ------------------------------------------------------------
    #
    # **Это и есть главная правка 12.10.2026.** До неё модуль вёл прямо на
    # `ask_number`, и в дереве не было **ни одного** узла ошибок: что именно
    # сломалось, оператор описывал свободным текстом, а система пыталась угадать
    # шаблон по этому тексту. Теперь ошибка — обычный выбор из кнопок.
    #
    # **Узел ошибок один на модуль, а не на пару «модель + модуль».** Одинаковые
    # модули несут одинаковые ошибки: у K50H `Chassis`, `QR Code` и `Safety` —
    # те же, что у A42T, а `Lifting` — ошибка `Lift` («Can't lift up/down the
    # fork»). Железо и неисправности те же, и второй узел с тем же содержимым
    # разошёлся бы при первой же правке. Различие моделей задают **узлы
    # модулей** выше, а не узлы ошибок.
    for error_module, error_options in MODULE_ERRORS.items():
        add(choice_node(
            error_node_id(error_module),
            f"{ERROR_MODULE_TITLES[error_module]}: what is wrong?",
            key="error",
            summary_label="Error",
            description="Choose the error you see.",
            options=tuple(
                # **Ошибка ведёт сразу к причине.** Номер уже введён в начале
                # пути (схема владельца 12.10.2026), а описание убрано с
                # обычного пути: его спрашивают только при причине `Other`.
                opt(option_id, label, ASK_CAUSE)
                for option_id, label in error_options
            ),
        ))

    # ------------------------------------------------------------
    # Ветки, закрытые владельцем: Workstation и Charging station
    # ------------------------------------------------------------
    # **Владелец 12.10.2026: «Workstation и Charging Station ПОКА НЕ РАБОТАЮТ».**
    #
    # Выбор такой категории **сразу** показывает сообщение и **не начинает
    # диалог**: у узла нет вариантов, текст не принимается, кнопка «Confirm» не
    # рисуется, а `flow._confirm` отказывает (узел не `FINAL` — сессия не
    # завершена). Запись в журнал из этой ветки поэтому невозможна.
    #
    # Текст вежливый и **говорит, что делать**: молчаливый отказ заставил бы
    # оператора гадать, сломался бот или нет.
    add(Node(
        id=WORKSTATION_UNAVAILABLE_NODE,
        title="Workstations are not available yet",
        type=NodeType.UNAVAILABLE,
        description=WORKSTATION_UNAVAILABLE,
    ))

    add(Node(
        id=CHARGING_UNAVAILABLE_NODE,
        title="Charging stations are not available yet",
        type=NodeType.UNAVAILABLE,
        description=CHARGING_UNAVAILABLE,
    ))

    add(choice_node(
        "qr_type",
        "Which QR code?",
        key="device_type",
        summary_label="Type",
        description="Choose where the QR code is located.",
        options=(
            opt("qr_shelf", "Shelf", ASK_NUMBER),
            opt("qr_floor", "Floor", ASK_NUMBER),
        ),
    ))

    # ------------------------------------------------------------
    # Шаг 3: уникальный номер оборудования
    # ------------------------------------------------------------
    # Вводится руками: у техники бывают составные номера вида «H108/1834».
    #
    # **Подсказка и проверка зависят от ветки, а узел — один.**
    # `placeholder` у узла один, поэтому на ветке QR оператор видел пример
    # **номера робота** и вводил робота (дефект владельца 12.10.2026: 16
    # QR-строк с номерами `3492`, `2668`). Вид номера выводится из ПУТИ
    # (`number_kind_for` ниже): карта живёт в коде и наложением правок
    # редактора не теряется. Отдельные узлы на каждую ветку разошлись бы —
    # редактор правит по одному, и в дереве появились бы мёртвые двойники.
    #
    # `answer_kind` на узле — запасной ответ для веток, которых нет в карте.
    add(Node(
        id=ASK_NUMBER,
        title="Device number",
        type=NodeType.INPUT,
        key="device_number",
        summary_label="Device",
        description="Unique equipment number.",
        placeholder="For example: 3780 or H108/1834",
        answer_kind="robot",
        # **Следующий шаг зависит от ветки.**
        #
        # Схема владельца 12.10.2026: «Ask device number» стоит сразу после
        # категории, «Check device number on server» — между номером и моделью.
        # Значит у робота после проверки справочника спрашивается **модель**.
        #
        # У QR модели, модуля и ошибки нет: код места/полки — уже всё, что
        # известно о железе, поэтому дальше сразу **причина**. Без этой развилки
        # QR пошёл бы выбирать модель робота.
        #
        # **Ветки зарядки здесь нет.** Раньше стояло
        # `next_node_by_kind=((KIND_STATION, ASK_CHARGING_ROBOT),)`: после номера
        # станции спрашивался робот. Сама ветка убрана из дерева владельцем
        # 12.10.2026, поэтому развилка для неё не нужна — механизм оставлен в
        # `types.Node.next_node_by_kind` и включается возвратом ветки.
        next_node_by_kind=(
            (KIND_QR_FLOOR, ASK_CAUSE),
            (KIND_QR_SHELF, ASK_CAUSE),
        ),
        next_node="robot_type",
    ))

    # ------------------------------------------------------------
    # Шаг 3б: робот, стоявший на зарядной станции — **узел убран из дерева**
    # ------------------------------------------------------------
    # **Владелец 12.10.2026: «Charging Station ПОКА НЕ РАБОТАЕТ».** Ветка
    # зарядки убрана целиком, поэтому и второй её узел (`ask_charging_robot`)
    # стал недостижим — а недостижимый узел валидатор справедливо запрещает.
    #
    # Логика не потеряна: писатель (`report_writer`) по-прежнему умеет писать
    # станцию в `device_number`, а робота — в `robot_number`, и `validation`
    # знает вид `station`. Вернуть ветку значит вернуть варианты корня, узел
    # типа станции и этот узел — а не писать код заново. Константа
    # `ASK_CHARGING_ROBOT` и карта `_NUMBER_KIND_BY_NODE` оставлены для этого.

    # ------------------------------------------------------------
    # Шаг 4: описание проблемы — **только для причины `Other`**
    # ------------------------------------------------------------
    # **Владелец 12.10.2026, дословно:** «убери вопросы об описание ошибки оно
    # нужно **только если other выберет**».
    #
    # Узел **не удалён** из дерева: он нужен ветке `Other` (см. `ASK_CAUSE`), и
    # тексты бота остаются прежними. Убран только **переход** на него с обычного
    # пути: после причины, отличной от `Other`, бот идёт сразу к `summary`.
    #
    # Почему описание вообще убрали. Ошибка теперь выбирается кнопками
    # (`robot_errors_*`), и свободный текст дублировал бы её. Смысл вопроса
    # остаётся ровно там, где кнопок не хватает: если причина не подошла ни под
    # один вариант, оператор объясняет её словами.
    add(Node(
        id=ASK_DESCRIPTION,
        title="Describe the problem",
        type=NodeType.INPUT,
        key="description",
        summary_label="Problem",
        description="Describe the problem in your own words.",
        placeholder="For example: does not lift the forks, red light is blinking",
        next_node=SUMMARY,
    ))

    # ------------------------------------------------------------
    # Шаг 5: причина ошибки
    # ------------------------------------------------------------
    # **Владелец: «добавить причину ошибки как ещё один вопрос».** Это не
    # повтор описания: описание отвечает «что не так» («не поднимает вилы»),
    # причина — «почему» («наехал на посторонний предмет»). Раньше причина не
    # спрашивалась, поэтому в отчётах её не было, и разбор повторяющихся ошибок
    # упирался в догадки.
    #
    # Список — стартовый набор из практики склада; правится через редактор
    # (`/tree`), как и остальные узлы. «Other» оставлен намеренно: закрытый
    # список без него заставляет выбирать неточное и портит статистику.
    #
    # **Ведёт по-разному в зависимости от варианта** (решение владельца
    # 12.10.2026): `Other` — на свободное описание, все остальные причины —
    # сразу к итогу. Это и есть «описание нужно только если other выберет».
    add(choice_node(
        ASK_CAUSE,
        "What caused it?",
        key="cause",
        summary_label="Cause",
        description="Why it happened — helps to prevent a repeat.",
        options=(
            opt("human_error", "Human error", SUMMARY),
            opt("obstacle", "Obstacle on the path", SUMMARY),
            opt("dirty_code", "Dirty or damaged floor code", SUMMARY),
            opt("mechanical_wear", "Mechanical wear", SUMMARY),
            opt("software_fault", "Software fault", SUMMARY),
            opt("battery_charge", "Battery or charging", SUMMARY),
            # Единственная причина, ведущая на описание: своего варианта у неё
            # нет, и слова оператора — единственный источник подробностей.
            opt("other", "Other", ASK_DESCRIPTION),
        ),
    ))

    # ------------------------------------------------------------
    # Финал
    # ------------------------------------------------------------
    add(Node(
        id=SUMMARY,
        title="Inspection result",
        type=NodeType.FINAL,
        summary_title="Inspection result",
        final_action="save_inspection",
    ))

    return nodes


# ============================================================
# ВИД НОМЕРА — ЗАВИСИТ ОТ ВЕТКИ, А НЕ ОТ УЗЛА
# ============================================================

#: Какой номер вводится на этом шаге — по варианту, который оператор уже
#: выбрал. Ключ — `option.id` из дерева (или из правок редактора: id тот же).
#:
#: **Зачем карта, если у узла есть `answer_kind`.** `ask_number` один на всё
#: дерево, а `ask_charging_robot` — соседний узел. Вид номера задаёт **ветка**,
#: и её видно в пути. Карта живёт в коде, поэтому наложение правок редактора её
#: не теряет: правки меняют подписи и переходы, но не id вариантов.
_NUMBER_KIND_BY_OPTION = {
    # Шаг 1 — что за оборудование.
    "robot": KIND_ROBOT,
    # `workstation` и `charger` сейчас ведут на узлы «недоступно», и номер по ним
    # не спрашивается вовсе. Записи оставлены намеренно: это вид номера **той**
    # ветки, и при её возврате карта уже верна.
    "workstation": KIND_WORKSTATION,
    "charger": KIND_STATION,
    "qr": KIND_QR_FLOOR,
    # Шаг 2 — разновидность.
    "a42t_c2": KIND_ROBOT,
    "a42t": KIND_ROBOT,
    "k50h": KIND_ROBOT,
    "ws_pick": KIND_WORKSTATION,
    "ws_conveyor": KIND_WORKSTATION,
    "ws_tally": KIND_WORKSTATION,
    "qr_shelf": KIND_QR_SHELF,
    "qr_floor": KIND_QR_FLOOR,
    # Шаг 2в — **ошибки модулей**. Вид номера ищется от последнего шага пути к
    # первому, поэтому ошибка (самый глубокий шаг) проверяется первой. Все
    # ошибки робота дают номер робота; карта перечисляет их явно, чтобы новая
    # ветка «ошибка → номер» не зависела от того, стоит ли выше модель в карте.
    **{
        option_id: KIND_ROBOT
        for options in MODULE_ERRORS.values()
        for option_id, _label in options
    },
}

#: Вид номера по конкретному узлу (там, где ветку определяет сам узел).
#:
#: Сейчас пусто: единственным таким узлом был `ask_charging_robot`, и он убран
#: вместе с закрытой веткой зарядки. Механизм оставлен — он нужен, как только
#: узел вернётся.
_NUMBER_KIND_BY_NODE = {}


def number_kind_for(path=None, node=None) -> str:
    """
    Вид номера на текущем шаге: `robot`, `station`, `workstation`,
    `qr_floor`, `qr_shelf`.

    **Почему по пути, а не по узлу.** Узел `ask_number` один, и подсказка у
    него одна — поэтому оператор на ветке QR видел пример **номера робота** и
    вводил робота (16 таких QR-строк на 12.10.2026). Вид вводимого значения
    задаёт **ветка**, а ветка — это выбранные варианты в пути.

    Порядок разбора: сначала конкретный узел (робот на зарядке), затем
    варианты пути от последнего к первому — так более близкий выбор уточняет
    более ранний. Пустой путь или неизвестная ветка дают `robot`: это обычный
    узел номера, каким он и был до правки.
    """
    if node is not None and getattr(node, "id", None) in _NUMBER_KIND_BY_NODE:
        return _NUMBER_KIND_BY_NODE[node.id]

    for step in reversed(list(path or ())):
        kind = _NUMBER_KIND_BY_OPTION.get(str(getattr(step, "option_id", "") or ""))

        if kind:
            return kind

    return KIND_ROBOT


#: Пример ввода для каждой ветки — то, что оператор видит подсказкой.
#: **Пример обязан совпадать с веткой:** именно несовпадение и рождало
#: «номер робота в поле кода полки».
NUMBER_EXAMPLES = {
    KIND_ROBOT: "For example: 3780 or H108/1834",
    KIND_STATION: "For example: 137",
    KIND_WORKSTATION: "For example: C2-H143",
    KIND_QR_FLOOR: "For example: SS512-020118-0101",
    KIND_QR_SHELF: "For example: Shelf-138-039-1",
}


def number_example(kind: str) -> str:
    """Подсказка-пример для ветки (по умолчанию — как у робота)."""
    return NUMBER_EXAMPLES.get(kind, NUMBER_EXAMPLES[KIND_ROBOT])


# ============================================================
# КОРНЕВОЕ ДЕРЕВО
# ============================================================

# Подписи кнопок навигации.
#
# Намеренно БЕЗ эмодзи и коротко: на телефоне и в перчатках кнопка должна
# читаться мгновенно, а не пестрить. Списки вариантов тоже без иконок.
BACK_LABEL = "Back"
RESTART_LABEL = "Restart"
CANCEL_LABEL = "Cancel"
DONE_LABEL = "Done"
SKIP_LABEL = "Skip"


# Файл со сгенерированным из данных деревом (результат
# tools/build_tree_from_errors.py). По умолчанию НЕ используется: логика
# выше — авторитетная. Данные остаются материалом для будущего расширения,
# когда описаний накопится достаточно.
GENERATED_TREE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tree.generated.json"
)


def _authoritative_tree() -> DecisionTree:
    """Дерево по бизнес-логике заказчика."""
    tree = DecisionTree(root_id="root", nodes=_build_nodes())
    tree.normalize()
    tree.validate()

    return tree


def _load_generated_tree() -> Optional[DecisionTree]:
    """
    Дерево из `tree.generated.json`, если файл есть и читается.

    Нужно только для явного запроса (см. load_tree("generated")). Ошибку
    глотаем намеренно — отсутствие файла не должно ломать импорт пакета.
    """
    try:
        with open(GENERATED_TREE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None

    return DecisionTree.from_dict(data)


# Дерево по умолчанию — зафиксированная логика заказчика.
DEFAULT_TREE = _authoritative_tree()


# ============================================================
# ЗАГРУЗКА ДЕРЕВА (статический конфиг ИЛИ backend)
# ============================================================

def load_tree(source: Optional[str] = None) -> DecisionTree:
    """
    Возвращает дерево решений.

    Без аргумента — встроенная логика заказчика.

    source допускает:
      * None/"default" — встроенное дерево;
      * "generated" — дерево, собранное из выгрузки ошибок;
      * путь к JSON-файлу (локальная проверка);
      * строку с JSON (например, ответ backend);
      * URL http(s):// — дерево грузится с backend и кэшируется.

    Это и есть точка расширения: чтобы дерево приходило из БД, достаточно
    отдать JSON того же формата (DecisionTree.to_dict()).
    """
    if not source or source == "default":
        return DEFAULT_TREE

    if source == "generated":
        return _load_generated_tree() or DEFAULT_TREE

    if source.startswith("http://") or source.startswith("https://"):
        return _load_tree_from_url(source)

    if os.path.exists(source):
        with open(source, "r", encoding="utf-8") as handle:
            return DecisionTree.from_dict(json.load(handle))

    return DecisionTree.from_dict(json.loads(source))


# Кэш загруженного дерева: backend не дёргаем на каждый шаг.
_CACHE: dict = {}


def _load_tree_from_url(url: str) -> DecisionTree:
    if url in _CACHE:
        return _CACHE[url]

    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    tree = DecisionTree.from_dict(payload)
    _CACHE[url] = tree

    return tree


def clear_cache() -> None:
    """Сброс кэша backend-дерева (например, после правок в админке)."""
    _CACHE.clear()
