"""
Пошаговый осмотр оборудования из Telegram: команда `/inspection`.

Требование владельца (16.10.2026), дословно: «сделать команду /inspection и
чтобы предлагало выбор инспекции и после выбора то как на сайте вопросы или по
очереди фото примера, что проверяем, зачем проверяем и просьба проверить и
отправить фото после чего сохраняем фото инспекции и идём дальше и так до
конца но сначала после команды спроси номер девайса … если станция то только
ищем по станциям … привязывай инспекцию к устройству через ключ … после
инспекции выдавать результат и общую ссылку для всех на веб версию инспекции
так же в инспекции нужно помнить кто и когда её делал».

Уточнения того же дня: **фото на КАЖДЫЙ пункт**; **фото = «норма»**, а
отдельная кнопка **«Problem»** — если что-то не так; присланное фото и
фото-пример **удаляются** из чата, после чего диалог идёт дальше.

─── Схема шагов ─────────────────────────────────────────────────────────────

    /inspection
      │
      ├─ 0. Склад — **только если он не определён топиком** (см. ниже):
      │        · топик ошибок конкретного склада → склад известен, шага нет;
      │        · служебный топик Inspection (25189) → склад спрашиваем;
      │        · предлагаются только те склады, куда есть доступ по API.
      │
      ├─ 1. Тип оборудования: [Robot] [Charging station] [Workstation] [✖ Cancel]
      │        └─ тип обязателен: он задаёт категорию поиска (см. ниже)
      │
      ├─ 2. Номер устройства (текстом) → поиск строго в этой категории
      │     И на выбранном складе
      │        └─ не найден / найден в другой категории — осмотр НЕ начинается
      │
      ├─ 3. Шаблон осмотра: кнопки, отфильтрованные по категории устройства
      │        └─ «Robot daily inspection», «K50H daily inspection», …
      │
      ├─ 4. Пункты по очереди (пункт N из M):
      │        · фото-пример (если есть) — «так должно быть»
      │        · что проверяем (title) + зачем (purpose) + подсказка (hint)
      │        · [✖ Cancel]
      │        └─ оператор присылает фото
      │              → фото = «норма» (ok)
      │              → кнопка [⚠️ Problem] остаётся, если что-то не так
      │              → фото-пример и фото оператора удаляются
      │              → следующий пункт
      │        · [⚠️ Problem] → комментарий текстом → warning (для критичного
      │          пункта failed) → следующий пункт
      │
      ├─ 5. Результат: сколько проверено, сколько проблем, вердикт осмотра
      │
      └─ 6. Публичная ссылка «for everyone on the web»

─── Почему состояние в памяти ───────────────────────────────────────────────

Как и остальные диалоги бота (`flow`, `editor`, `robot_status`): словарь с TTL
и блокировкой. Плюс `/cancel` и понятный отказ при истечении. Состояние осмотра
при этом живёт **в базе** (`equipment_inspections`) с первой секунды: в памяти
только «на каком пункте мы стоим», и перезапуск бота не теряет сам осмотр —
оператор увидит его на веб-версии незакрытым.

─── Где живёт команда и почему спрашивается склад (17.10.2026) ─────────────

Владелец перенёс `/inspection` в служебный топик `Inspection` (thread 25189,
`TELEGRAM_TOPIC_SERVICE=25189`). Из этого следует две вещи:

1. **Топик должен быть разрешён на отправку.** `is_shared_topic` знал `service`
   и раньше, поэтому команда принималась, но барьер `_output_topic_allowed`
   отклонял **ответ** — человек видел молчание. Список на отправку
   (`allowed_output_topics`) дополнен видом `service` в `telegram_bot.py`.
2. **Склад из топика больше не выводится.** В топике ошибок
   (`Ex GLPC` / `Ex SP3`) склад известен из самого топика, и там вопрос не
   задаётся — он только замедлял бы работу. В служебном топике
   `error_warehouse_for_thread()` возвращает `None`, и раньше осмотр молча
   привязывался к `DEFAULT_WAREHOUSE` (GLP-C) — то есть станция SMALL-P3
   искалась бы в GLP-C. Теперь склад спрашивают (шаг 0), а поиск номера
   (`validation.find_equipment`) всегда идёт **в выбранном** складе: номера
   повторяются между складами (`121`, `122`, `123` есть в обоих), и склад в
   поиске обязателен.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import telegram_api as tg

from equipment_intake import validation
from inspection_api import (
    InspectionApiError,
    InspectionApiUnavailable,
    api_configured,
    complete_inspection,
    create_inspection,
    create_share,
    list_templates,
    public_url,
    set_item_verdict,
    upload_item_photo,
)

#: Лог диалога осмотра. Нужен там, где молчаливое решение скрыло бы проблему
#: (например, склад карточки, которого нет в `TELEGRAM_WAREHOUSES`).
logger = logging.getLogger(__name__)


# ============================================================
# СОСТОЯНИЕ: ПАМЯТЬ + TTL
# ============================================================

#: Сколько живёт диалог без действий.
#:
#: Полчаса — как у дерева приёма: осмотр из девяти пунктов с фото занимает
#: минуты, но оператор может отвлечься на робота. Час был бы «диалог, который
#: уже никто не ждёт», а пять минут — «не успел дойти до станции».
SESSION_TTL_SECONDS = int(os.environ.get("TELEGRAM_INSPECTION_TTL", "1800") or "1800")

#: Шаги диалога.
STEP_WAREHOUSE = "warehouse"  # выбор склада (лишь если топик его не задал)
STEP_TYPE = "type"          # выбор типа оборудования
STEP_NUMBER = "number"      # номер устройства
STEP_TEMPLATE = "template"  # выбор шаблона осмотра
STEP_ITEM = "item"          # показ пункта и ожидание фото
STEP_PROBLEM = "problem"    # ожидание комментария к проблемному пункту
STEP_DONE = "done"          # осмотр закрыт, показан итог

#: Категории оборудования, доступные к осмотру: (код справочника, подпись
#: кнопки, вид номера для `validation`).
#:
#: `conveyor` в списке нет: у конвейера отдельный шаблон (id=4), но в
#: справочнике он не адресуется номером так, как остальные, — а выдумывать
#: способ ввода значило бы делать непроверяемую ветку. Появится требование —
#: добавится строка.
CATEGORIES = (
    ("robot", "🤖 Robot", validation.KIND_ROBOT),
    ("charging", "🔌 Charging station", validation.KIND_STATION),
    ("workstation", "🏭 Workstation", validation.KIND_WORKSTATION),
)

#: Подписи статуса устройства для человека. `not_in_work` пишется заглавными
#: не для красоты: это единственный статус, при котором осмотр **отказан**, и
#: он должен бросаться в глаза.
STATUS_LABELS = {
    "online": "online",
    "offline": "offline",
    "maintenance": "maintenance",
    "not_in_work": "NOT IN WORK",
    "unknown": "unknown",
}

#: Префикс callback-данных диалога. `ins` не занят никем: занятые префиксы —
#: `dt` (дерево), `ed` (редактор), `rf` (номер робота), `em` (меню ошибки),
#: `w` (склад), `st` (статус).
#:
#: 🔴 **`CB_WAREHOUSE = "w"` — это НЕ конфликт с префиксом `w`.** Имена разведены
#: по позиции в `callback_data`, а не по букве: выбор склада отправляет
#: `ins:w:<title>`, и `handle_callback` сначала проверяет **первое** поле
#: (`ins`). До чужого обработчика `w` (`_handle_warehouse_choice`, он ловит
#: данные, начинающиеся с `w:`) такое нажатие не доходит. Совпадение буквы
#: безопасно именно потому, что Telegram кодирует **всю** строку целиком, а
#: диспетчер смотрит её первый сегмент.
CB_PREFIX = "ins"

CB_WAREHOUSE = "w"   # ins:w:<warehouse_title>
CB_TYPE = "t"        # ins:t:<category>
CB_TEMPLATE = "k"    # ins:k:<template_id>
CB_PROBLEM = "p"     # ins:p:<item_index>
CB_CANCEL = "x"      # ins:x

#: Склады, куда у API есть доступ у любого (не админа): **только свой склад**
#: карточки, как в `can_access_warehouse`.
#:


@dataclass
class Session:
    """Состояние одного диалога осмотра."""

    chat_id: int
    user_id: int
    step: str = STEP_TYPE
    #: `auth_id` работника: им подписывается токен для API. Осмотр «от лица
    #: бота» записал бы всех на одного служебного сотрудника, и требование
    #: «помнить кто делал» перестало бы выполняться.
    auth_id: Optional[str] = None
    employee_name: str = ""
    warehouse: str = ""
    #: Склады, доступные этому человеку. Пусто — «не вычисляли»: в этом случае
    #: работает прежнее правило «склад из топика».
    warehouses: List[str] = field(default_factory=list)
    thread_id: Optional[int] = None
    #: Тип оборудования и его вид для проверки номера.
    category: Optional[str] = None
    kind: Optional[str] = None
    #: Найденное устройство. Осмотр привязывается к `equipment["id"]`.
    equipment: Dict[str, Any] = field(default_factory=dict)
    templates: List[Dict[str, Any]] = field(default_factory=list)
    #: Созданный осмотр целиком (с пунктами).
    inspection: Dict[str, Any] = field(default_factory=dict)
    item_index: int = 0
    #: Комментарий проблемного пункта, собранный по шагам.
    problem_comment: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def items(self) -> List[Dict[str, Any]]:
        return self.inspection.get("items") or []

    def current_item(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.item_index < len(self.items):
            return self.items[self.item_index]
        return None


_SESSIONS: Dict[tuple, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _key(chat_id, user_id) -> tuple:
    return (int(chat_id), int(user_id))


def put_session(session: Session) -> None:
    with _LOCK:
        _SESSIONS[_key(session.chat_id, session.user_id)] = {
            "session": session,
            "expires": time.time() + SESSION_TTL_SECONDS,
        }


def get_session(chat_id, user_id) -> Optional[Session]:
    """Живая сессия или None. Просроченная удаляется по дороге."""
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = _SESSIONS.get(key)

        if entry and entry["expires"] < time.time():
            _SESSIONS.pop(key, None)
            entry = None

    if not entry:
        return None

    return entry["session"]


def peek_session(chat_id, user_id) -> Optional[Session]:
    """Живая сессия без изъятия — нужна на развилках диспетчера.

    Диспетчер спрашивает «идёт ли у этого человека осмотр» **до** того, как
    отдать сообщение другим флоу (приёму ошибок, меню фото). Изъять сессию там
    нельзя: сообщение может оказаться не про осмотр, и диалог потерялся бы.

    Защита от «двойного тапа» живёт не здесь, а на входе: `_update_key`
    дедуплицирует нажатие по `cb:<callback_id>`. Поэтому изымать сессию при
    чтении не нужно.
    """
    return get_session(chat_id, user_id)


def drop_session(chat_id, user_id) -> Optional[Session]:
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = _SESSIONS.pop(key, None)

    return entry["session"] if entry else None


def sweep_sessions() -> int:
    """Убирает просроченные сессии, чтобы словарь не рос."""
    now = time.time()

    with _LOCK:
        stale = [key for key, entry in _SESSIONS.items() if entry["expires"] < now]

        for key in stale:
            _SESSIONS.pop(key, None)

    return len(stale)


def reset_state() -> None:
    """Сброс хранилища — только для тестов."""
    with _LOCK:
        _SESSIONS.clear()


# ============================================================
# ЛЕНИВЫЙ ДОСТУП К БОТУ
# ============================================================
#
# Тот же приём и по той же причине, что в `equipment_intake.flow`: бот
# запускается как `python3 telegram_bot.py`, то есть живёт в `sys.modules` под
# именем `__main__`. Простой `import telegram_bot` загрузил бы ВТОРУЮ копию
# модуля с пустыми картами топиков, и ответы ушли бы не в тот топик.

def _bot():
    import sys

    main = sys.modules.get("__main__")

    if main is not None and getattr(main, "handle_update", None) is not None:
        return main

    import telegram_bot

    return telegram_bot


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
#
# 🔴 **Не больше двух рядов вариантов** (требование владельца 13.10.2026) —
# раскладку считает общий `keyboard_layout`, а не этот модуль.


def cancel_button() -> Dict[str, str]:
    return {"text": "✖️ Cancel", "callback_data": f"{CB_PREFIX}:{CB_CANCEL}"}


def warehouse_keyboard(session: Session) -> Dict[str, list]:
    """
    Кнопки выбора склада — **всегда все склады бота** (обычно две).

    🔴 **Роль больше не участвует (решение владельца 17.10.2026).** Была попытка
    вычислять доступные склады по роли работника, сверяясь с правилом API
    (`can_access_warehouse`: работник и лидер — только свой склад, любой —
    только `admin`/`manager`). Владелец это отменил: «пускай просто будет две
    кнопки и всё, не с сервера».

    **Почему так можно.** Отказ API при чужом складе приходит **сразу после
    выбора шаблона** — осмотр создаётся именно там (`_choose_template` →
    `create_inspection`), а не в конце. То есть человек теряет один тап, а не
    пройденные пункты с фото. Зато список складов перестал зависеть от чтения
    карточки: он одинаков для всех и не может разойтись с правами API.

    Подписи **не хардкодятся**: их источник — `warehouses.WAREHOUSES`, словарь
    из `TELEGRAM_WAREHOUSES` (тот же список, что и у остальных команд).
    """
    import keyboard_layout

    labels = [str(title) for title in session.warehouses]
    columns = keyboard_layout.columns_for_labels(labels)

    buttons = [
        {
            "text": str(title),
            "callback_data": f"{CB_PREFIX}:{CB_WAREHOUSE}:{title}",
        }
        for title in session.warehouses
    ]

    rows = keyboard_layout.grid_rows(
        buttons, columns=columns, max_rows=len(buttons) or 1
    )
    rows.append([cancel_button()])

    return {"inline_keyboard": rows}


def type_keyboard() -> Dict[str, list]:
    import keyboard_layout

    labels = [label for _cat, label, _kind in CATEGORIES]
    columns = keyboard_layout.columns_for_labels(labels)

    buttons = [
        {"text": label, "callback_data": f"{CB_PREFIX}:{CB_TYPE}:{category}"}
        for category, label, _kind in CATEGORIES
    ]

    rows = keyboard_layout.grid_rows(
        buttons, columns=columns, max_rows=len(buttons) or 1
    )
    rows.append([cancel_button()])

    return {"inline_keyboard": rows}


def template_keyboard(session: Session) -> Dict[str, list]:
    import keyboard_layout

    labels = [str(t.get("name") or "") for t in session.templates]
    columns = keyboard_layout.columns_for_labels(labels)

    buttons = [
        {
            "text": str(template.get("name") or f"Template {template.get('id')}"),
            "callback_data": f"{CB_PREFIX}:{CB_TEMPLATE}:{template.get('id')}",
        }
        for template in session.templates
    ]

    rows = keyboard_layout.grid_rows(
        buttons, columns=columns, max_rows=len(buttons) or 1
    )
    rows.append([cancel_button()])

    return {"inline_keyboard": rows}


def item_keyboard(session: Session) -> Dict[str, list]:
    """
    Кнопки пункта.

    Фото = «норма», поэтому основной путь — просто прислать снимок. Кнопка
    `Problem` нужна для случая «что-то не так»: она открывает комментарий, и
    вердикт ставится `warning` (или `failed`, если пункт критичный).
    """
    item = session.current_item()
    rows: List[List[Dict[str, str]]] = []

    if item is not None:
        rows.append([{
            "text": "⚠️ Problem",
            # Индекс пункта, а не id: у пункта id есть, но он приходит из API,
            # и повторная доставка апдейта не должна подменить пункт.
            "callback_data": f"{CB_PREFIX}:{CB_PROBLEM}:{session.item_index}",
        }])

    rows.append([cancel_button()])

    return {"inline_keyboard": rows}


def problem_keyboard() -> Dict[str, list]:
    return {"inline_keyboard": [[cancel_button()]]}


# ============================================================
# ТЕКСТЫ
# ============================================================

def warehouse_prompt(session: Session) -> str:
    """
    Первый шаг в служебном топике: **какой склад осматриваем**.

    Зачем вопрос вообще нужен: `/inspection` теперь живёт в служебном топике
    `Inspection`, а не в топике ошибок, и склад из топика там не выводится.
    Раньше в этой роли работал `bot.current_warehouse()`, который для всего
    неизвестного отдаёт `DEFAULT_WAREHOUSE` (GLP-C) — то есть осмотр станции
    SMALL-P3 молча привязался бы к GLP-C и номер там не нашёлся бы (номера
    повторяются между складами: `121`, `122`, `123` есть и в GLP-C, и в
    SMALL-P3).
    """
    return (
        "🔍 Equipment inspection\n"
        "\n"
        "Which warehouse do you inspect?\n"
        "The device number is then checked on that warehouse only."
    )


def type_prompt() -> str:
    return (
        "🔍 Equipment inspection\n"
        "\n"
        "What do you want to inspect?\n"
        "Pick the type first — the number is then checked "
        "in that category only."
    )


def number_prompt(session: Session) -> str:
    """Подсказка по формату — **своя для каждого типа**."""
    hints = {
        "robot": "e.g. 3780 or H108",
        "charging": "e.g. 137 (charging station number)",
        "workstation": "e.g. C2-H143",
    }
    label = dict((c, l) for c, l, _k in CATEGORIES).get(session.category, "")

    return (
        f"🔢 {label}\n"
        "\n"
        "Send the equipment number.\n"
        f"{hints.get(session.category, '')}\n"
        "\n"
        "It must exist in the system — I'll check it before starting."
    )


def template_prompt(session: Session) -> str:
    equipment = session.equipment
    status = str(equipment.get("status") or "unknown")
    warehouse = equipment.get("warehouse") or session.warehouse
    zone = equipment.get("sub_warehouse")
    zone_line = f" · zone {zone}" if zone else ""

    return (
        f"✅ Found: {equipment.get('equipment_code')} "
        f"({session.category})\n"
        f"{warehouse}{zone_line} · status: {STATUS_LABELS.get(status, status)}\n"
        "\n"
        "Choose the inspection checklist:"
    )


def item_caption(session: Session, item: Dict[str, Any]) -> str:
    """
    Подпись пункта: что проверяем, зачем, подсказка и «пришлите фото».

    Владелец: «что проверяем, зачем проверяем и просьба проверить и отправить
    фото». Поэтому порядок именно такой, а не «title + hint».
    """
    total = len(session.items)
    position = session.item_index + 1
    lines = [f"📋 Step {position} of {total} — {item.get('title')}"]

    purpose = str(item.get("purpose") or "").strip()

    if purpose:
        lines.append(f"❓ Why: {purpose}")

    if item.get("is_critical"):
        lines.append("🔴 Critical point — a problem here fails the whole inspection")

    if item.get("requires_photo"):
        lines.append("📷 A photo is required for this point")

    example = str(item.get("example_photo_url") or "").strip()

    if example:
        lines.append("🖼 The photo above is the example — it should look like this")

    lines.append("")
    # 🔴 **Только ФОТО, без обещания видео** (исправлено 23.10.2026).
    #
    # Раньше здесь стояло «Send a photo or a video if it's OK». Обещание было
    # **ложным сразу в двух местах**, и это выяснилось на живой ошибке:
    #
    # 1. **API не принимает видео.** `validate_image` определяет тип по
    #    сигнатуре файла и знает только JPEG/PNG/WebP — MP4 и MOV отклоняются с
    #    `400 File content does not match a supported image type`. Проверено.
    # 2. **Веб не умеет показывать видео.** Страница осмотра рисует
    #    `<img src={item.photo_url}>`, то есть видео в этой колонке не
    #    отобразилось бы даже при удачной загрузке.
    #
    # Хуже всего было следствие: оператор снимал видео, бот его забирал и
    # пытался приложить, получал отказ и **пункт не закрывался**. Со стороны это
    # выглядело как «бот завис» — при том, что инструкция сама предлагала видео.
    #
    # Поэтому обещание убрано, а не «поддержано втихую»: обещать то, чего нет,
    # хуже, чем не обещать. Если видео понадобится, сначала API и веб, потом
    # текст.
    lines.append("📷 Send a photo if it's OK, or tap ⚠️ Problem.")

    return "\n".join(lines)


def result_text(session: Session) -> str:
    """
    Итог осмотра: сколько проверено, сколько проблем, вердикт.

    Считаем **по пунктам из осмотра**, а не по нашей памяти: вердикт осмотра
    считает сервер, и два источника правды разошлись бы.
    """
    inspection = session.inspection
    items = inspection.get("items") or []

    checked = [item for item in items if item.get("verdict")]
    problems = [
        item for item in items
        if item.get("verdict") in ("warning", "failed")
    ]
    failed = [item for item in items if item.get("verdict") == "failed"]

    verdict = str(inspection.get("verdict") or "ok")
    headline = {
        "ok": "✅ Inspection passed",
        "warning": "⚠️ Inspection passed with warnings",
        "failed": "⛔ Inspection failed",
    }.get(verdict, "Inspection finished")

    equipment = session.equipment
    lines = [
        headline,
        "",
        f"🔧 {equipment.get('equipment_code')} ({session.category}) "
        f"· {equipment.get('warehouse') or session.warehouse}",
        f"📋 Checklist: {inspection.get('template_id') and _template_name(session) or '—'}",
        f"👤 Done by: {session.employee_name or 'unknown'}",
        f"🕒 {_warsaw_now()}",
        "",
        f"✔️ Checked: {len(checked)} of {len(items)}",
        f"⚠️ Problems: {len(problems)}",
    ]

    if failed:
        lines.append(f"⛔ Failed points: {len(failed)}")

    if problems:
        lines.append("")
        lines.append("Problem points:")

        for item in problems:
            mark = "⛔" if item.get("verdict") == "failed" else "⚠️"
            comment = str(item.get("comment") or "").strip()
            suffix = f" — {comment}" if comment else ""
            lines.append(f"{mark} {item.get('title')}{suffix}")

    return "\n".join(lines)


def _template_name(session: Session) -> str:
    template_id = session.inspection.get("template_id")

    for template in session.templates:
        if str(template.get("id")) == str(template_id):
            return str(template.get("name") or template_id)

    return str(template_id or "—")


def _warsaw_now() -> str:
    from datetime import datetime

    from shift import WARSAW_TZ

    return datetime.now(WARSAW_TZ).strftime("%d.%m.%Y %H:%M")


# ============================================================
# ОТПРАВКА
# ============================================================

def _delete_quiet(chat_id, message_id) -> bool:
    if message_id is None:
        return False

    return tg.delete_message(chat_id, message_id)


def _delete_user_message(chat_id, message_id) -> bool:
    return _bot()._delete_user_message(chat_id, message_id)


# ============================================================
# СТАРТ ДИАЛОГА
# ============================================================

def start(chat_id, sender, reply_to=None) -> bool:
    """
    Вход в диалог по `/inspection`. True — команда обработана.

    Право проверяем здесь, а не на каждом шаге: если осмотр в этом окружении
    невозможен (нет адреса API или секрета), честнее сказать сразу.
    """
    bot = _bot()

    if not api_configured():
        bot._send(
            chat_id,
            "⚠️ Inspections are not available: the service is not configured "
            "on the bot (TK_ASSIST_API_URL / SUPABASE_JWT_SECRET).",
            reply_to_message_id=reply_to,
        )
        return True

    employee_name = ""
    employee = None

    try:
        from telegram_store import StoreUnavailable, get_employee_context

        # **Через `bot.` тоже работает, но только если имя импортировано в
        # модуль бота.** В `telegram_bot` импортированы не все помощники
        # `telegram_store`, поэтому берём функцию из самого хранилища: иначе
        # `AttributeError` зависел бы от того, какие имена кто-то решил
        # реэкспортировать, и это ломалось бы при безобидной правке импортов.
        employee_name, employee = get_employee_context(sender.get("id"), strict=True)
    except StoreUnavailable:
        bot._send(
            chat_id,
            "⚠️ Can't read the database right now. Please try again in a minute.",
            reply_to_message_id=reply_to,
        )
        return True
    except ImportError:
        employee = None

    if not employee_name or not employee:
        bot._send(
            chat_id,
            "⚠️ You are not registered yet.\n"
            "Send /reg <Your Name> (the name as it is written in Lark), e.g.\n"
            "/reg Ivan Petrenko",
            reply_to_message_id=reply_to,
        )
        return True

    auth_id = employee.get("auth_id")

    if not auth_id:
        # `auth_id` есть у всех сотрудников (проверено: 37 из 37), но если
        # появится карточка без него — осмотр записался бы без автора, а
        # владелец требует помнить, кто делал.
        bot._send(
            chat_id,
            "⚠️ Your account is not linked to a system user (no auth_id), "
            "so inspections can't be attributed to you. Ask a leader to fix "
            "your employee card.",
            reply_to_message_id=reply_to,
        )
        return True

    session = Session(
        chat_id=chat_id,
        user_id=sender.get("id"),
        auth_id=auth_id,
        employee_name=employee_name or "",
        warehouse=bot.current_warehouse(),
        thread_id=bot._reply_thread(chat_id),
    )

    # 🔴 **Команда работает только в служебном топике `Inspection`.**
    #
    # Требование владельца 17.10.2026: «нужно только в топике что я тебе дал».
    # Топик задаётся переменной `TELEGRAM_TOPIC_SERVICE` (в проде 25189).
    #
    # **Почему не «молча».** Сюда команда попадает только из топика, который бот
    # **обслуживает** (в чужом его отсекает `_handle_wrong_topic` раньше), поэтому
    # ответить здесь — не нарушение правила «бот молчит там, где его нет». А
    # молчание было бы худшим исходом: ровно так `/inspection` уже «не работала»,
    # когда топик не был разрешён на отправку, и это выглядело как поломка.
    #
    # **Если служебный топик не настроен — не ограничиваем.** Иначе на свежей
    # установке без `TELEGRAM_TOPIC_SERVICE` команда стала бы мёртвой везде, и
    # поломка выглядела бы как «бот не отвечает». Тот же принцип, что у
    # `_output_topic_allowed`: пустая настройка не должна превращаться в немоту.
    service_thread = bot.topic_thread("service")

    if service_thread is not None and int(session.thread_id or 0) != int(service_thread):
        logger.info(
            "Осмотр: команда пришла из топика %s, а работает только в %s",
            session.thread_id,
            service_thread,
        )
        bot._send(
            chat_id,
            "⚠️ /inspection works only in the Inspection topic.",
            reply_to_message_id=reply_to,
        )
        return True

    # 🔴 **Склад: спрашиваем, если не задан топиком ошибок.**
    #
    # В топике ошибок склада (`Ex GLPC` / `Ex SP3`) склад уже известен — вопрос
    # был бы лишним шагом: человек стоит в топике этого склада. В служебном
    # топике (`Inspection`) топик склад не задаёт, поэтому спрашиваем: без этого
    # работа привязалась бы к `DEFAULT_WAREHOUSE` (GLP-C), и станция SMALL-P3
    # искалась бы не на том складе — номера повторяются между складами.
    #
    # Тот же признак, по которому определяется склад для приёма ошибок:
    # `error_warehouse_for_thread` отдаёт `None` для всего, что не топик ошибок.
    from_topic = bot.error_warehouse_for_thread(chat_id, session.thread_id)
    choices = _warehouse_choices()

    if not choices:
        # Склады не настроены вовсе (`TELEGRAM_WAREHOUSES` пуст). Предлагать
        # нечего, а поиск без склада запрещён — честно объясняем.
        bot._send(
            chat_id,
            "⚠️ No warehouses are configured for this bot "
            "(TELEGRAM_WAREHOUSES), so inspections can't run.",
            reply_to_message_id=reply_to,
        )
        return True

    if from_topic:
        # Склад задан топиком ошибок — вопроса нет.
        session.warehouse = from_topic
        session.step = STEP_TYPE
        put_session(session)
        _ask_type(session, reply_to)

        return True

    session.warehouses = choices
    session.step = STEP_WAREHOUSE
    put_session(session)

    sent = bot._send(
        chat_id,
        warehouse_prompt(session),
        reply_to_message_id=reply_to,
        reply_markup=warehouse_keyboard(session),
    )

    if sent:
        session.data["prompt_message_id"] = sent.get("message_id")
        put_session(session)

    return True


def _ask_type(session: Session, reply_to=None) -> None:
    """Показывает шаг выбора типа — общий путь для всех «склад уже известен»."""
    sent = _bot()._send(
        session.chat_id,
        type_prompt(),
        reply_to_message_id=reply_to,
        reply_markup=type_keyboard(),
    )

    if sent:
        session.data["prompt_message_id"] = sent.get("message_id")
        put_session(session)


def _warehouse_choices() -> List[str]:
    """
    Склады для кнопок — **все склады бота**, без проверки роли.

    🔴 **Решение владельца 17.10.2026:** «пускай просто будет две кнопки и всё,
    не с сервера». До этого список вычислялся по роли из карточки работника и
    повторял правило API `can_access_warehouse` (работник и лидер — только свой
    склад). Владелец попросил убрать эту зависимость: кнопки должны быть
    простыми и одинаковыми, а не результатом чтения карточки.

    **Что это меняет по существу.** Раньше работник GLP-C вообще не видел
    SMALL-P3, и «осмотреть чужой склад» было невозможно. Теперь кнопка есть у
    всех, а выбор чужого склада упрётся в `403` от API. Это **осознанный
    размен**: отказ приходит сразу после выбора шаблона — осмотр создаётся
    именно там (`_choose_template` → `create_inspection`), а не в конце, поэтому
    теряется один тап, а не пройденные пункты с фото.

    **У функции нет аргументов — и это намеренно.** Раньше она принимала
    карточку работника и склад топика, чтобы посчитать доступ. Теперь списку
    неоткуда взяться, кроме `TELEGRAM_WAREHOUSES`: если снова понадобится
    фильтр по роли, его придётся добавить явно, а не «случайно», потому что
    аргумент уже под рукой.

    Список — из `warehouses.WAREHOUSES` (переменная `TELEGRAM_WAREHOUSES`), а не
    из строк в коде: второй источник правды разошёлся бы с остальными командами
    бота.
    """
    from warehouses import WAREHOUSES

    known = [str(title) for title in WAREHOUSES.values()]

    if not known:
        # Складов не настроено вовсе (`TELEGRAM_WAREHOUSES` пуст). Предлагать
        # нечего: без склада поиск номера запрещён — номера повторяются между
        # складами, и вернулось бы оборудование чужой площадки.
        logger.warning("Осмотр: TELEGRAM_WAREHOUSES пуст — склады не настроены")
        return []

    return known


# ============================================================
# CALLBACK-КНОПКИ
# ============================================================

def handle_callback(chat_id, sender, parts, message_id, callback_id) -> bool:
    """Кнопки осмотра. False — это не наша кнопка."""
    if not parts or parts[0] != CB_PREFIX:
        return False

    bot = _bot()
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    def answer(text: str = None):
        if callback_id:
            tg.answer_callback_query(callback_id, text)

    if action == CB_CANCEL:
        session = drop_session(chat_id, sender.get("id"))
        answer("Cancelled")

        if session:
            # 🔴 **Убираем ВСЁ, что бот отправил по этому осмотру.**
            #
            # Владелец 23.10.2026: «cancel работает криво очень». Дефект был в
            # том, что список удаляемого **отставал** от списка отправляемого:
            # `Cancel` знал про карточку пункта и образец, но не про сообщение
            # «Describe what is wrong» — оно оставалось в чате с рабочими на
            # вид кнопками.
            #
            # Хуже: после отмены человек видел **старые** кнопки `Problem` /
            # `Cancel` и нажимал их снова. Сессии уже нет, и ответ был
            # «This inspection expired» — то есть отмена выглядела как поломка.
            _delete_quiet(chat_id, session.data.pop("prompt_message_id", None))
            _delete_quiet(chat_id, session.data.pop("example_message_id", None))
            _delete_quiet(chat_id, session.data.pop("photo_message_id", None))
            _delete_quiet(chat_id, session.data.pop("problem_message_id", None))

            # Само нажатие тоже убираем: иначе в чате остаётся сообщение с
            # кнопками, которые уже ничего не делают.
            _delete_quiet(chat_id, message_id)

        return True

    session = get_session(chat_id, sender.get("id"))

    if session is None:
        # Сессия истекла или бот перезапустился. Молчание здесь выглядело бы
        # как сломанная кнопка, поэтому говорим, что делать.
        answer("This inspection expired — send /inspection to start again")
        _delete_quiet(chat_id, message_id)
        return True

    try:
        if action == CB_WAREHOUSE:
            _choose_warehouse(session, arg, chat_id, message_id, answer)
        elif action == CB_TYPE:
            _choose_type(session, arg, chat_id, message_id, answer)
        elif action == CB_TEMPLATE:
            _choose_template(session, arg, chat_id, message_id, answer)
        elif action == CB_PROBLEM:
            _start_problem(session, arg, chat_id, message_id, answer)
        else:
            answer("Unknown action")
            return True
    except InspectionApiUnavailable as error:
        answer("Service unavailable")
        bot._send(chat_id, f"⚠️ {error.message}")
        drop_session(chat_id, sender.get("id"))
        return True
    except InspectionApiError as error:
        answer("Rejected")
        bot._send(chat_id, f"⚠️ {error.message}")
        return True

    return True


def _choose_warehouse(session: Session, title: str, chat_id, message_id, answer) -> None:
    """
    Первый шаг: склад выбран → показываем выбор типа.

    Проверяем выбор по списку из сессии, а не «на слово»: сессия живёт в памяти
    и обновляется только этим кодом, но кнопка устаревшего сообщения могла
    прийти с чужим складом (например, после правки переменных окружения). Тогда
    лучше отказать, чем искать устройство на складе, которого нет.
    """
    if title not in session.warehouses:
        answer("Unknown warehouse")
        return

    session.warehouse = title
    session.step = STEP_TYPE
    put_session(session)

    answer(title)

    bot = _bot()
    bot._send(
        chat_id,
        type_prompt(),
        reply_markup=type_keyboard(),
    )
    _delete_quiet(chat_id, message_id)


def _choose_type(session: Session, category: str, chat_id, message_id, answer) -> None:
    labels = {code: label for code, label, _kind in CATEGORIES}

    if category not in labels:
        answer("Unknown type")
        return

    session.category = category
    session.kind = dict((c, k) for c, _l, k in CATEGORIES)[category]
    session.step = STEP_NUMBER
    put_session(session)

    answer(labels[category])

    bot = _bot()
    bot._send(
        chat_id,
        number_prompt(session),
        reply_markup={"inline_keyboard": [[cancel_button()]]},
    )
    _delete_quiet(chat_id, message_id)


def _choose_template(session: Session, template_id: str, chat_id, message_id, answer) -> None:
    template = next(
        (t for t in session.templates if str(t.get("id")) == str(template_id)),
        None,
    )

    if not template:
        answer("Unknown checklist")
        return

    if not template.get("items"):
        answer("This checklist has no points")
        return

    answer("Starting…")

    inspection = create_inspection(
        session.auth_id,
        equipment_id=int(session.equipment["id"]),
        template_id=int(template["id"]),
    )

    session.inspection = inspection
    session.item_index = 0
    session.step = STEP_ITEM
    put_session(session)

    _delete_quiet(chat_id, message_id)
    _show_item(session)


def _start_problem(session: Session, arg: str, chat_id, message_id, answer) -> None:
    """Кнопка «Problem»: просим комментарий, вердикт поставим после него."""
    try:
        index = int(arg)
    except (TypeError, ValueError):
        answer("Unknown point")
        return

    if index != session.item_index or session.current_item() is None:
        # Нажатие на кнопку устаревшего сообщения: пункт уже пройден.
        answer("This point is already done")
        return

    session.step = STEP_PROBLEM
    session.problem_comment = ""
    put_session(session)

    answer("Describe the problem")

    bot = _bot()
    sent = bot._send(
        chat_id,
        f"⚠️ Problem at: {session.current_item().get('title')}\n"
        "\n"
        "Describe what is wrong (one message).\n"
        "A critical point marked as a problem fails the whole inspection.",
        reply_markup=problem_keyboard(),
    )

    # 🔴 **Сообщение проблемы запоминаем, иначе Cancel его не уберёт.**
    #
    # Владелец 23.10.2026: «cancel работает криво очень». Причина: `Cancel`
    # удалял `prompt_message_id` (карточку пункта) и `example_message_id`
    # (образец), а это сообщение **нигде не сохранялось** — и оставалось в
    # чате после отмены. За осмотр из четырнадцати пунктов, где оператор
    # несколько раз нажимал `Problem`, топик забивался брошенными
    # «Describe what is wrong» с живыми на вид кнопками.
    #
    # Ключ отдельный, а не `prompt_message_id`: карточка пункта и этот вопрос —
    # **разные** сообщения, и затирание одного id другим оставило бы мусор.
    if sent:
        session.data["problem_message_id"] = sent.get("message_id")
        put_session(session)

    _delete_quiet(chat_id, message_id)


# ============================================================
# ТЕКСТ: НОМЕР И КОММЕНТАРИЙ ПРОБЛЕМЫ
# ============================================================

def handle_text(chat_id, sender, text: str, message_id) -> bool:
    """
    Текст в диалоге осмотра: номер устройства или комментарий проблемы.

    False — сейчас ждём не ответ осмотра.
    """
    session = get_session(chat_id, sender.get("id"))

    if session is None:
        return False

    if session.step == STEP_NUMBER:
        _handle_number(session, text, chat_id, message_id)
        return True

    if session.step == STEP_PROBLEM:
        _handle_problem_comment(session, text, chat_id, message_id)
        return True

    # Прочие шаги текст не принимают: номер и пункты — единственные текстовые
    # шаги, а на шаге выбора кнопка надёжнее свободного текста. Шаг склада тоже
    # кнопочный: названия складов приходят из `TELEGRAM_WAREHOUSES`, и сверять
    # свободный текст с ними значило бы завести второе правило разбора.
    return False


def _handle_number(session: Session, text: str, chat_id, message_id) -> None:
    bot = _bot()
    value = str(text or "").strip()

    # **Формат — жёстко, справочник — мягко** (решение владельца 12.10.2026).
    # Не подошло по форме — это мусор, переспрашиваем.
    format_error = validation.validate_format(session.kind, value)

    if format_error:
        bot._send(chat_id, f"⚠️ {format_error}")
        return

    found = validation.find_equipment(session.kind, session.warehouse, value)

    if not found:
        # Номер не найден **в выбранной категории**. Владелец требует не
        # подменять категорию молча — показываем, где он всё-таки есть.
        elsewhere = validation.find_elsewhere(session.kind, session.warehouse, value)
        bot._send(chat_id, _not_found_text(session, value, elsewhere))
        return

    session.equipment = found

    # **Единица не в работе — осматривать нечего.** В проекте
    # `not_in_work` исключается из рапортов (владелец: «вся экипировка что не
    # в работе не должна попадать в рапорты»), история перехода не ведётся.
    # Разрешить молча — значит отправить человека осматривать то, что стоит в
    # ремонте или списано. Поэтому отказываем, но **говорим, что делать**:
    # вернуть в работу через /online (это единственный штатный путь).
    status = str(found.get("status") or "")

    if status == "not_in_work":
        bot._send(
            chat_id,
            f"⛔ {found.get('equipment_code')} is NOT IN WORK "
            "(under repair or written off).\n"
            "Inspections are not done on equipment that is out of service.\n"
            "Return it to work first with /online.",
        )
        return

    _delete_user_message(chat_id, message_id)

    try:
        session.templates = [
            t for t in list_templates(session.auth_id, session.category)
            if t.get("items")
        ]
    except InspectionApiUnavailable as error:
        bot._send(chat_id, f"⚠️ {error.message}")
        return
    except InspectionApiError as error:
        bot._send(chat_id, f"⚠️ {error.message}")
        return

    if not session.templates:
        bot._send(
            chat_id,
            f"⚠️ No inspection checklist is configured for "
            f"{session.category}. Ask a leader to add one.",
        )
        return

    session.step = STEP_TEMPLATE
    put_session(session)

    # Мягкое предупреждение о состоянии, но не отказ: `offline` и
    # `maintenance` — это «сейчас не работает», а не «списано». Отказ здесь
    # мешал бы осматривать станцию, которую как раз чинят.
    warning = ""

    if status in ("offline", "maintenance"):
        warning = (
            f"ℹ️ {found.get('equipment_code')} is currently "
            f"{STATUS_LABELS.get(status, status)}.\n\n"
        )

    bot._send(
        chat_id,
        warning + template_prompt(session),
        reply_markup=template_keyboard(session),
    )


def _not_found_text(session: Session, value: str, elsewhere: list) -> str:
    """Понятный отказ: не найден и, если есть, где он есть на самом деле."""
    label = dict((c, l) for c, l, _k in CATEGORIES).get(session.category, session.category)

    lines = [
        f"⛔ {label}: {value} is not in the system "
        f"({session.category}, {session.warehouse}).",
    ]

    if elsewhere:
        # Самая частая ошибка — номер есть, но это ДРУГОЕ устройство (в GLP-C
        # номер 13 — и робот, и зарядная станция). Молча подставить категорию
        # значило бы осмотреть не то, что человек назвал.
        lines.append("")
        lines.append("The same number exists as:")

        for warehouse, category in elsewhere[:5]:
            lines.append(f"• {category} · {warehouse}")

        lines.append("")
        lines.append("Send /inspection again and pick the right type.")
    else:
        lines.append("")
        lines.append("Check the number and send it again, or /cancel.")

    return "\n".join(lines)


def _handle_problem_comment(session: Session, text: str, chat_id, message_id) -> None:
    bot = _bot()
    item = session.current_item()

    if item is None:
        drop_session(chat_id, session.user_id)
        return

    comment = str(text or "").strip()

    if not comment:
        bot._send(chat_id, "⚠️ Please describe the problem in one message.")
        return

    # 🔴 **«Problem» ВСЕГДА требует снимок — требование владельца (23.10.2026).**
    #
    # Дословно: «Если Problem то тоже нужно фото просто пометить что проблемное».
    # То есть снимок нужен **любому** проблемному пункту, а не только тому, у
    # которого шаблон требует фото: именно снимок и есть доказательство поломки,
    # и без него тому, кто будет чинить, нечего смотреть.
    #
    # **Что было до этого.** Снимок просился только при `requires_photo`, а в
    # остальных случаях `_finish_item` вызывался **без `photo_path`**: пункт
    # закрывался вердиктом `warning`/`failed` и **без фотографии**. Для пунктов
    # шаблона это ещё и вело в тупик — сервер отказывался закрывать осмотр,
    # если снимок обязателен (`A photo is required for: K-12`), а пункт уже
    # считался пройденным.
    #
    # **Комментарий сохраняем, а не теряем.** Человек уже написал «Horn is
    # cracked» — текст кладём в `session.problem_comment`, и снимок закрывает
    # пункт **с этим объяснением** и вердиктом `failed`/`warning`
    # (`handle_photo`). Иначе после фото пункт стал бы `ok`, то есть «проблемы
    # нет», и слова оператора пропали бы.
    if not item.get("photo_url"):
        session.problem_comment = comment
        session.step = STEP_PROBLEM
        put_session(session)
        bot._send(
            chat_id,
            f"📷 Send a photo of the problem: {item.get('title')}.\n"
            "\n"
            f"Your description is saved: “{comment}”\n"
            "The photo will be attached together with it, and the point will be "
            "marked as a problem.\n"
            "Without a photo the point can't be closed.",
            reply_markup=problem_keyboard(),
        )
        return

    # **Критичный пункт → `failed`, остальное → `warning`.** Требование
    # владельца: кнопка «Problem» — для «что-то не так». Провал (failed) требует
    # комментария и делает осмотр провальным; для некритичного пункта достаточно
    # предупреждения, иначе осмотр «падал» бы из-за мелочи вроде пыли.
    verdict = "failed" if item.get("is_critical") else "warning"

    _finish_item(
        session,
        verdict=verdict,
        comment=comment,
        chat_id=chat_id,
        delete_message_id=message_id,
    )


# ============================================================
# ФОТО
# ============================================================

def handle_photo(chat_id, sender, message, message_id, photo_path: str) -> bool:
    """
    Фото пункта осмотра. `photo_path` — уже скачанный ботом файл.

    **Фото = «норма» (`ok`)** — требование владельца. Отдельного «подтвердите,
    что всё хорошо» не нужно: снимок и есть подтверждение.

    🔴 **Видео получает честный отказ (23.10.2026).** Раньше видео уходило в
    загрузку как фото и падало на `validate_image` с
    `400 File content does not match a supported image type`. Оператор видел
    «Couldn't save the photo. Please send the photo again» и **не понимал, что
    не так**: он сделал ровно то, что предлагала подсказка.

    Теперь видео распознаётся **до** попытки загрузки, и ответ называет
    причину. Пункт при этом **не закрывается** — снимка всё ещё нет, и
    выдавать видео за подтверждённую проверку нельзя.
    """
    # Видео и «кружок» — не фото: API принимает только изображения, а веб
    # рисует `<img>`. Говорим об этом прямо, а не «фото не сохранилось».
    if message.get("video") or message.get("video_note") or message.get("animation"):
        _bot()._send(
            chat_id,
            "⚠️ Video isn't supported for the inspection — send a photo instead.\n"
            "A photo is what gets attached to the point and shown on the web.",
        )
        return True

    session = get_session(chat_id, sender.get("id"))

    if session is None or session.step not in (STEP_ITEM, STEP_PROBLEM):
        return False

    if session.step == STEP_PROBLEM:
        # 🔴 **Фото на шаге проблемы = снимок доказательства (23.10.2026).**
        #
        # Требование владельца: «если Problem то тоже нужно фото просто пометить
        # что проблемное». Поэтому здесь два случая, и они **разные**:
        #
        # 1. **Объяснение уже есть** (`problem_comment`) — снимок дополняет его,
        #    и пункт закрывается **сразу** с вердиктом `failed`/`warning`.
        #    Раньше бот и в этом случае просил текст заново, хотя человек его
        #    уже написал.
        # 2. **Объяснения ещё нет** — снимок прикрепили, но описание нужно:
        #    «что-то не так» без слов не объясняет, что чинить.
        if not _attach_photo(session, photo_path, chat_id, message_id):
            return True

        problem_comment = str(session.problem_comment or "").strip()

        if problem_comment:
            _finish_item(
                session,
                verdict=(
                    "failed"
                    if (session.current_item() or {}).get("is_critical")
                    else "warning"
                ),
                comment=problem_comment,
                chat_id=chat_id,
                delete_message_id=message_id,
                # Фото уже прикреплено внутри `_attach_photo` — передавать
                # `photo_path` нельзя: снимок загрузился бы второй раз.
                photo_path=None,
            )
            return True

        bot = _bot()
        bot._send(
            chat_id,
            "📷 Photo saved. Now describe the problem in one message.",
            reply_markup=problem_keyboard(),
        )
        return True

    # 🔴 **Фото на пункте, который заявлен как проблемный (23.10.2026).**
    #
    # `Problem` просит снимок **первым** (`STEP_PROBLEM`), но снимок может
    # прийти и раньше — пока пункт ещё открыт. В этом случае нельзя ставить
    # `ok`: человек уже нажал `Problem`, и «норма» перечеркнула бы его слова.
    #
    # Проверяем это **до** `_finish_item`, иначе снимок закрывал пункт как
    # исправный — то есть ровно противоположно тому, что оператор сообщил.
    problem_comment = str(session.problem_comment or "").strip()

    if problem_comment:
        _finish_item(
            session,
            verdict="failed" if session.current_item().get("is_critical") else "warning",
            comment=problem_comment,
            chat_id=chat_id,
            delete_message_id=message_id,
            photo_path=photo_path,
        )
        return True

    _finish_item(
        session,
        verdict="ok",
        comment=None,
        chat_id=chat_id,
        delete_message_id=message_id,
        photo_path=photo_path,
    )

    return True


def _attach_photo(session: Session, photo_path: str, chat_id, message_id) -> bool:
    """Загружает фото пункта в API. True — прикрепилось."""
    bot = _bot()
    item = session.current_item()

    if item is None:
        return False

    try:
        with open(photo_path, "rb") as handle:
            content = handle.read()
    except OSError:
        bot._send(
            chat_id,
            "⚠️ Can't read the photo file on the server. Please send it again.",
        )
        return False

    try:
        uploaded = upload_item_photo(
            session.auth_id,
            inspection_id=int(session.inspection["id"]),
            item_id=int(item["id"]),
            content=content,
        )
    except InspectionApiUnavailable as error:
        bot._send(chat_id, f"⚠️ {error.message}")
        return False
    except InspectionApiError as error:
        bot._send(
            chat_id,
            f"⚠️ Couldn't save the photo: {error.message}\n"
            "Please send the photo again.",
        )
        return False

    # 🔴 **Снимок запоминаем в сессии (23.10.2026).**
    #
    # Раньше сюда приходило `upload_item_photo(...)` без сохранения результата:
    # фото уходило на сервер, но в `session.items[i]["photo_url"]` оставалось
    # пусто. Из-за этого **следующая** проверка «есть ли у пункта снимок»
    # считала, что его нет, — и предлагала снять ещё раз, хотя снимок уже был.
    #
    # Проверка нужна в двух местах: при `Problem` на пункте с обязательным фото
    # и при повторной отправке. Оба смотрят в сессию, а не в API: лишний запрос
    # на каждый шаг незачем, а после успешной загрузки ответ API всё равно
    # содержит `photo_url`.
    if isinstance(uploaded, dict):
        url = uploaded.get("photo_url") or (uploaded.get("data") or {}).get("photo_url")

        if url:
            item["photo_url"] = url
            put_session(session)

    return True


def _finish_item(
    session: Session,
    verdict: str,
    comment: Optional[str],
    chat_id,
    delete_message_id=None,
    photo_path: Optional[str] = None,
) -> None:
    """
    Ставит вердикт пункта, убирает фото из чата и переходит к следующему.

    Порядок важен: фото загружаем **до** вердикта. Если наоборот, при сбое
    загрузки пункт остался бы с вердиктом «норма» и без снимка — то есть
    непроверенное выдано за проверенное.
    """
    bot = _bot()
    item = session.current_item()

    if item is None:
        drop_session(chat_id, session.user_id)
        return

    if photo_path and not _attach_photo(session, photo_path, chat_id, delete_message_id):
        # Фото не сохранилось — пункт не закрываем: оператор повторит снимок.
        return

    try:
        set_item_verdict(
            session.auth_id,
            inspection_id=int(session.inspection["id"]),
            item_id=int(item["id"]),
            verdict=verdict,
            comment=comment,
        )
    except InspectionApiUnavailable as error:
        bot._send(chat_id, f"⚠️ {error.message}")
        return
    except InspectionApiError as error:
        bot._send(
            chat_id,
            f"⚠️ Couldn't save the point: {error.message}",
        )
        return

    # **Фото оператора, фото-пример И сообщение пункта убираем из чата.**
    #
    # 🔴 **Сообщение пункта не удалялось, и это был дефект** (владелец
    # 17.10.2026: «шаги потом не удаляются»). Убирались только два фото, а
    # текст «Step N of M» с кнопками `Problem` / `Cancel` оставался — за осмотр
    # из четырнадцати пунктов в топике накапливалось столько же сообщений, и
    # оператор не понимал, какой пункт активен: старые кнопки выглядят рабочими.
    #
    # Найдено воспроизведением: при переходе удалялись `999` (фото оператора) и
    # `401` (пример), а `prompt_message_id = 400` оставался, хотя сессия уже
    # указывала на новое сообщение (`500`). Старый текст терял владельца, но не
    # исчезал.
    #
    # Порядок: сначала удаляем **прошлое** сообщение пункта, затем `_show_item`
    # положит в `prompt_message_id` новое.
    _delete_quiet(chat_id, session.data.pop("prompt_message_id", None))

    if delete_message_id is not None:
        _delete_user_message(chat_id, delete_message_id)

    _delete_quiet(chat_id, session.data.pop("example_message_id", None))
    # `photo_message_id` нигде не заполнялся — оставлено на случай, если
    # появится путь, где фото приходит от бота отдельным сообщением.
    _delete_quiet(chat_id, session.data.pop("photo_message_id", None))

    # 🔴 **Вопрос «Describe what is wrong» тоже убираем.**
    #
    # Он появляется при нажатии `Problem` и раньше **не удалялся нигде**: ни
    # при переходе к следующему пункту, ни при отмене. За осмотр из
    # четырнадцати пунктов с несколькими проблемами в топике оставалась стопка
    # сообщений с кнопкой `Cancel` — и оператор не понимал, какая из них
    # относится к текущему пункту (владелец: «cancel работает криво очень»).
    _delete_quiet(chat_id, session.data.pop("problem_message_id", None))

    session.item_index += 1
    session.problem_comment = ""
    session.step = STEP_ITEM
    put_session(session)

    if session.current_item() is None:
        _finish_inspection(session, chat_id)
        return

    _show_item(session)


# ============================================================
# ПОКАЗ ПУНКТА
# ============================================================

def _show_item(session: Session) -> None:
    """Отправляет фото-пример (если есть) и подпись пункта."""
    bot = _bot()
    item = session.current_item()

    if item is None:
        _finish_inspection(session, session.chat_id)
        return

    caption = item_caption(session, item)
    example = str(item.get("example_photo_url") or "").strip()
    thread_id = session.thread_id

    sent = None

    if example:
        sent = _send_example_photo(session, example, caption)

        if sent:
            session.data["example_message_id"] = sent.get("message_id")
            put_session(session)

    if not sent:
        # Нет примера или он не отправился (ссылка битая, Telegram не принял
        # картинку) — пункт всё равно показываем текстом: без него осмотр
        # остановился бы на первом же пункте с плохой ссылкой.
        sent = bot._send(
            session.chat_id,
            caption,
            thread_id=thread_id,
            reply_markup=item_keyboard(session),
        )

        if sent:
            session.data["prompt_message_id"] = sent.get("message_id")
            put_session(session)


def _send_example_photo(session: Session, url: str, caption: str):
    """
    Отправляет фото-пример по ссылке.

    Именно `send_photo_url`, а не `send_photo`: фото лежит в Storage API, а
    `send_photo` открывает **локальный файл** (`open(image_path, "rb")`) и на
    ссылке молча возвращает `None` — пример не показался бы, и оператор
    оценивал бы «как должно быть» по одному тексту (проверено чтением кода).
    """
    try:
        return tg.send_photo_url(
            session.chat_id,
            url,
            caption=caption,
            message_thread_id=session.thread_id,
            reply_markup=item_keyboard(session),
        )
    except Exception:
        # Битая ссылка на пример не должна ломать осмотр: тот же путь, что при
        # «примера нет».
        return None


# ============================================================
# ЗАВЕРШЕНИЕ
# ============================================================

def _finish_inspection(session: Session, chat_id) -> None:
    """
    Закрывает осмотр, показывает итог и выдаёт публичную ссылку.

    Публичная ссылка выдаётся **только на закрытый осмотр** — иначе API
    откажет. Поэтому порядок именно такой: закрыть, затем поделиться.
    """
    bot = _bot()

    try:
        inspection = complete_inspection(
            session.auth_id, int(session.inspection["id"])
        )
    except InspectionApiUnavailable as error:
        bot._send(chat_id, f"⚠️ {error.message}")
        _close_session(session)
        return
    except InspectionApiError as error:
        # Самая частая содержательная причина — не все пункты заполнены.
        # Осмотр при этом остаётся в базе и виден на веб-версии незакрытым,
        # поэтому говорим об этом прямо, а не «что-то пошло не так».
        bot._send(
            chat_id,
            f"⚠️ Couldn't close the inspection: {error.message}\n"
            "It is saved as a draft — you can finish it on the web version.",
        )
        _close_session(session)
        return

    session.inspection = inspection
    session.step = STEP_DONE
    put_session(session)

    bot._send(chat_id, result_text(session))

    link = _share_link(session)

    if link:
        bot._send(
            chat_id,
            "🔗 Public report — for everyone on the web:\n"
            f"{link}",
        )

    _close_session(session)


def _share_link(session: Session) -> Optional[str]:
    """Публичная ссылка осмотра или None, если её не удалось выдать."""
    bot = _bot()

    try:
        share = create_share(session.auth_id, int(session.inspection["id"]))
    except (InspectionApiError, InspectionApiUnavailable) as error:
        # Итог осмотра уже отправлен — из-за ссылки его терять нельзя.
        bot._send(
            session.chat_id,
            f"⚠️ The inspection is done, but I couldn't create the public link: "
            f"{error.message}",
        )
        return None

    token = str((share or {}).get("public_token") or "").strip()

    if not token:
        return None

    return public_url(token)


def _close_session(session: Session) -> None:
    # 🔴 `problem_message_id` — третье сообщение осмотра, и раньше оно
    # переживало закрытие: после завершённого осмотра в чате оставался вопрос
    # «Describe what is wrong» с кнопкой `Cancel`.
    _delete_quiet(session.chat_id, session.data.get("example_message_id"))
    _delete_quiet(session.chat_id, session.data.get("prompt_message_id"))
    _delete_quiet(session.chat_id, session.data.get("problem_message_id"))
    drop_session(session.chat_id, session.user_id)


# ============================================================
# ОТМЕНА
# ============================================================

def cancel(chat_id, sender) -> bool:
    """
    `/cancel` внутри диалога. True — было что отменять.

    Незакрытый осмотр **не удаляем**: он остаётся черновиком в базе и виден на
    веб-версии. Удаление скрыло бы работу, которую человек уже сделал.
    """
    session = drop_session(chat_id, sender.get("id"))

    if session is None:
        return False

    _delete_quiet(chat_id, session.data.get("example_message_id"))
    _delete_quiet(chat_id, session.data.get("prompt_message_id"))
    # Вопрос «Describe what is wrong» — третье сообщение осмотра. Без этой
    # строки `/cancel` оставлял его в чате с живой на вид кнопкой `Cancel`
    # (владелец: «cancel работает криво очень»).
    _delete_quiet(chat_id, session.data.get("problem_message_id"))

    bot = _bot()
    text = "✖️ Inspection cancelled."

    if session.inspection.get("id"):
        text += (
            "\nThe draft is kept — you can finish it on the web version."
        )

    bot._send(chat_id, text, delete_after=bot.CONFIRM_TTL_SECONDS)

    return True
