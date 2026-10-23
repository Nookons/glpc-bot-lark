"""
Карточка завершённого осмотра для Lark-группы склада.

**Зачем отдельный модуль, а не «ещё одна карточка в боте».** Здесь три вещи,
которых в боте нет и быть не должно: сборка `interactive`-карточки, выбор
цвета по вердикту и **текстовый запасной вариант** — Lark отвечает не всегда,
а сообщение о выполненном осмотре терять нельзя.

## Требование владельца (23.10.2026)

Дословно: «после инспекции отправлялась карточка в Lark куда отправляются
ошибки по хуку что была выполнена инспекция вот такого оборудования таким то
работником краткий вывод всё хорошо, были найдены проблемы и ссылка публичная
на инспекцию».

То есть карточка отвечает на **четыре** вопроса и ни на один больше:

| Вопрос | Где в карточке |
|---|---|
| Какое оборудование | заголовок: `🤖 Robot 3780 · GLP-C` |
| Кто выполнял | строка `By`: имя работника из `done_by` |
| Короткий вывод | вердикт: всё хорошо / были проблемы (со счётом) |
| Публичная ссылка | кнопка «Open report» + сама ссылка текстом |

## 🔴 Почему ссылка в карточке — **кнопка и текст**

Вставлять в таблицу (требование владельца: «чтобы вставлять в таблицу») нужно
**текст**, поэтому ссылка печатается строкой: её видно, её можно выделить и
скопировать. Кнопка `Open report` нужна отдельно — по ней удобно открыть отчёт
с телефона. Один вид ссылки не заменяет второй: кнопку нельзя скопировать, а
длинный URL в тексте не нажмёшь на телефоне так же удобно.

## Почему карточка **не** падает вместе с осмотром

Отправка в Lark — **внешняя сеть**, и она может не ответить (лимит вебхука,
неверная переменная, сбой на стороне Lark). Осмотр к этому моменту уже закрыт и
сохранён, ссылка уже выдана оператору. Поэтому сбой отправки **логируется**, но
не отменяет ничего: человек свою работу сделал, и данные на сервере есть.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from logging_config import setup_logging
from time_utils import parse_iso

logger = setup_logging()

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

#: Подписи категорий оборудования: те же, что в боте (`inspection.CATEGORIES`).
#: Дублируются намеренно: модуль карточки не должен зависеть от `inspection`,
#: иначе импорт потянет за собой сессии, API и Telegram-клавиатуры.
CATEGORY_LABELS = {
    "robot": ("🤖", "Robot"),
    "charging": ("🔌", "Charging station"),
    "workstation": ("🏭", "Workstation"),
}

#: Подписи вердиктов осмотра и цвет шапки.
#:
#: Цвет — часть смысла, а не украшение: `ok` зелёный, `warning` жёлтый,
#: `failed` красный. В группе ошибок красная шапка читается раньше текста.
VERDICTS = {
    "ok": {
        "color": "green",
        "emoji": "✅",
        "label": "all good",
        "line": "No problems found",
    },
    "warning": {
        "color": "yellow",
        "emoji": "⚠️",
        "label": "problems found",
        "line": "Problems found — see the report",
    },
    "failed": {
        "color": "red",
        "emoji": "❌",
        "label": "failed",
        "line": "Critical problems found",
    },
}

#: Предел длины одной строки в карточке. Lark режет `lark_md` сам, но резка
#: посреди слова выглядит как обрыв данных, поэтому обрезаем осознанно.
VALUE_LIMIT = 120


def _clip(value, limit: int = VALUE_LIMIT) -> str:
    """Строка для карточки: без пустоты и с честным многоточием."""
    text = str(value or "").strip()

    if len(text) <= limit:
        return text

    return text[: limit - 1].rstrip() + "…"


def _warsaw_time(value) -> str:
    """Время в Варшаве — так его видит владелец (в базе UTC)."""
    moment = parse_iso(value)

    if moment is None:
        return ""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=WARSAW_TZ)

    return moment.astimezone(WARSAW_TZ).strftime("%d.%m.%Y %H:%M")


def _equipment_line(equipment: dict) -> str:
    """`3780 · RT_KUBOT_MINI_HAIFLEX · C2` — что именно осматривали.

    🔴 **Номер, а не внутренний id.** `equipment_id` — строка базы (`1339`),
    которой на корпусе робота нет. Оператор ищет единицу по номеру, и в группе
    ошибок читают тоже номер.
    """
    code = _clip(equipment.get("code") or "—", 32)
    model = _clip(
        (equipment.get("equipment_type") or {}).get("type") or "", 40
    )
    zone = _clip(equipment.get("sub_warehouse") or "", 12)

    parts = [code]

    if model:
        parts.append(model)

    if zone:
        parts.append(f"zone {zone}")

    return " · ".join(parts)


def _problem_items(items: list) -> list:
    """Пункты с проблемой, в порядке чек-листа."""
    return [
        item
        for item in items
        if str(item.get("verdict") or "").lower() in ("warning", "failed")
    ]


def verdict_summary(inspection: dict) -> dict:
    """Вердикт осмотра и его подпись для карточки.

    **Вердикт считаем по пунктам, если сервер его не вернул.** Так бывает у
    старых записей и при чтении по одному пункту; пустой вердикт в карточке
    выглядел бы как «осмотр без результата».
    """
    raw = str(inspection.get("verdict") or "").strip().lower()

    if raw not in VERDICTS:
        failed = any(
            str(item.get("verdict") or "").lower() == "failed"
            for item in inspection.get("items") or []
        )
        warned = any(
            str(item.get("verdict") or "").lower() == "warning"
            for item in inspection.get("items") or []
        )
        raw = "failed" if failed else ("warning" if warned else "ok")

    return {"key": raw, **VERDICTS[raw]}


def inspection_title(inspection: dict) -> str:
    """Заголовок карточки: `✅ 🤖 Robot 3780 · GLP-C`."""
    summary = verdict_summary(inspection)
    equipment = inspection.get("equipment") or {}
    emoji, label = CATEGORY_LABELS.get(
        str(equipment.get("category") or "").lower(), ("🔧", "Equipment")
    )
    code = _clip(equipment.get("code") or "—", 32)
    warehouse = _clip(inspection.get("warehouse") or "", 20)
    tail = f" · {warehouse}" if warehouse else ""

    return f"{summary['emoji']} {emoji} {label} {code}{tail}"


def build_inspection_card(
    inspection: dict,
    employee_name: str,
    public_link: str = None,
) -> dict:
    """Интерактивная карточка Lark о выполненном осмотре.

    `inspection` — ответ API (`GET /v1/inspections/{id}`) или объект из
    `complete_inspection`: нужны `verdict`, `items`, `equipment`, `warehouse`,
    `done_at`, `done_by`.
    """
    summary = verdict_summary(inspection)
    equipment = inspection.get("equipment") or {}
    items = inspection.get("items") or []
    problems = _problem_items(items)
    done = _warsaw_time(inspection.get("done_at"))

    checked = sum(1 for item in items if item.get("verdict"))
    total = len(items)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": summary["color"],
            "title": {"tag": "plain_text", "content": inspection_title(inspection)},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**Inspection completed** — {summary['emoji']} "
                        f"**{summary['label']}**\n"
                        f"**Equipment** {_equipment_line(equipment)}\n"
                        f"**Checked** {checked} of {total} points"
                    ),
                },
            },
            {"tag": "hr"},
        ],
    }

    # **Короткий вывод.** Владелец просил «краткий вывод всё хорошо, были
    # найдены проблемы». Поэтому здесь одно предложение и, если проблемы есть,
    # их список: без списка «были найдены проблемы» не говорит, **какие**, и
    # карточку пришлось бы открывать ради этого.
    if problems:
        lines = [f"**{summary['line']}:** {len(problems)}"]

        for item in problems[:8]:
            mark = "❌" if str(item.get("verdict")).lower() == "failed" else "⚠️"
            title = _clip(item.get("title") or item.get("code") or "—", 60)
            note = _clip(item.get("comment") or "", 60)
            lines.append(f"{mark} {title}" + (f" — {note}" if note else ""))

        if len(problems) > 8:
            lines.append(f"…and {len(problems) - 8} more")

        verdict_text = "\n".join(lines)
    else:
        verdict_text = f"**{summary['line']}** ✅"

    card["elements"].append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": verdict_text},
    })

    # **Ссылка: и кнопкой, и текстом.**
    #
    # Кнопку нельзя скопировать в таблицу, а длинный URL в тексте неудобно
    # открывать с телефона. Владелец просил «вставлять в таблицу» — значит,
    # текст обязателен; а кнопка нужна затем, чтобы отчёт открывался одним
    # касанием. Один вид ссылки не заменяет второй.
    if public_link:
        card["elements"].append({"tag": "hr"})
        card["elements"].append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📄 Open report"},
                "type": "primary",
                "url": public_link,
            }],
        })
        card["elements"].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**Public link** (copy it into the table)\n{public_link}",
            },
        })

    footer = f"👤 {_clip(employee_name or '—', 60)}"

    if done:
        footer += f" · {done}"

    card["elements"].append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": footer}],
    })

    return card


def build_inspection_text(
    inspection: dict,
    employee_name: str,
    public_link: str = None,
) -> str:
    """Текстовый запасной вариант, если карточка не прошла.

    🔴 **Не «на всякий случай».** Вебхук Lark отклоняет карточки при неверной
    схеме или лимите, а сообщение об осмотре терять нельзя: по нему в группе
    ошибок узнают, что оборудование проверено. Текст проходит там, где
    `interactive` не прошёл (тот же приём у статусов и сменных отчётов).
    """
    summary = verdict_summary(inspection)
    equipment = inspection.get("equipment") or {}
    items = inspection.get("items") or []
    problems = _problem_items(items)
    checked = sum(1 for item in items if item.get("verdict"))
    done = _warsaw_time(inspection.get("done_at"))

    lines = [
        inspection_title(inspection),
        f"Equipment: {_equipment_line(equipment)}",
        f"Result: {summary['emoji']} {summary['label']}",
        f"Checked: {checked} of {len(items)} points",
    ]

    if problems:
        lines.append(f"Problems ({len(problems)}):")

        for item in problems[:8]:
            title = _clip(item.get("title") or item.get("code") or "—", 60)
            note = _clip(item.get("comment") or "", 60)
            lines.append(f"  • {title}" + (f" — {note}" if note else ""))
    else:
        lines.append("Problems: none")

    if public_link:
        lines.append(f"Public link: {public_link}")

    lines.append(f"By: {employee_name or '—'}" + (f" · {done}" if done else ""))

    return "\n".join(lines)
