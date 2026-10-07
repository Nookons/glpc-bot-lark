"""
Telegram-обвязка производственного дерева решений.

Функционал вызывается производственным ботом после получения фотографии.

Данные сессии держатся в памяти процесса (как и остальные pending-меню
бота) с TTL, поэтому база и production-таблицы не затрагиваются.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import telegram_api as tg

from .engine import EngineError, Session
from .keyboards import (
    CB_BACK,
    CB_CANCEL,
    CB_CONFIRM,
    CB_CRUMB,
    CB_DONE,
    CB_EDIT,
    CB_PREFIX,
    CB_RESTART,
    CB_SELECT,
    CB_TOGGLE,
    keyboard_with_breadcrumb,
)
from .tree_config import CANCEL_LABEL, load_tree
from .types import NodeType


logger = logging.getLogger(__name__)


# ============================================================
# ФЛАГ И ХРАНИЛИЩЕ
# ============================================================

# Сколько живёт сессия без действий.
SESSION_TTL_SECONDS = 1800

_SESSIONS: Dict[Tuple[int, int], Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _key(chat_id, user_id) -> Tuple[int, int]:
    return (int(chat_id), int(user_id))


def put_session(chat_id, user_id, session: Session) -> None:
    with _LOCK:
        _SESSIONS[_key(chat_id, user_id)] = {
            "session": session,
            "expires": time.time() + SESSION_TTL_SECONDS,
        }


def get_session(chat_id, user_id) -> Optional[Session]:
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = _SESSIONS.get(key)

        if entry and entry["expires"] < time.time():
            _SESSIONS.pop(key, None)
            entry = None

    if not entry:
        return None

    return entry["session"]


def drop_session(chat_id, user_id) -> None:
    with _LOCK:
        _SESSIONS.pop(_key(chat_id, user_id), None)


def sweep_sessions() -> int:
    """Убирает просроченные сессии, чтобы словарь не рос."""
    now = time.time()

    with _LOCK:
        stale = [
            key for key, entry in _SESSIONS.items()
            if entry["expires"] < now
        ]

        for key in stale:
            _SESSIONS.pop(key, None)

    return len(stale)


def reset_state() -> None:
    """Сброс хранилища — только для тестов."""
    with _LOCK:
        _SESSIONS.clear()


# ============================================================
# ОТОБРАЖЕНИЕ
# ============================================================

# Импортируем лениво: telegram_bot импортирует этот модуль, поэтому
# обратная ссылка на уровне модуля дала бы цикл.
#
# ВАЖНО: бот запускается как `python3 telegram_bot.py`, то есть его
# рабочая копия живёт в sys.modules под именем «__main__», а не
# «telegram_bot». Если сделать просто `import telegram_bot`, Python
# загрузит ВТОРУЮ копию модуля: у неё пустое состояние маршрутизации
# топиков (_routes), поэтому _reply_thread() вернёт None, и в dry-run
# sendPhoto будет заблокирован — фото сотрудника удалится, а дерево не
# придёт. Поэтому сначала берём уже работающий __main__, и только если
# это не тот файл (например, нас импортировали из теста) — обычный импорт.
def _bot():
    import sys

    main = sys.modules.get("__main__")

    if main is not None and getattr(main, "__file__", None):
        same_file = os.path.abspath(main.__file__) == os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "telegram_bot.py")
        )

        if same_file and hasattr(main, "_reply_thread"):
            return main

    import telegram_bot

    return telegram_bot


def _progress_bar(session: Session) -> str:
    """
    Номер текущего уровня.

    Глубина дерева заранее неизвестна (ветки разной длины, дерево может
    прийти из БД), поэтому показываем уровень, а не «N из M» — иначе
    счётчик врал бы на коротких и длинных ветках.
    """
    level = len(session.path) + 1

    return f" · level {level}"


def step_caption(session: Session) -> str:
    """
    Подпись к фото: путь, вопрос и — главное — что бот чего-то ждёт.

    Текстовые шаги подписаны явно и заметно: иначе сотрудник видит вопрос
    без кнопок и не понимает, что нужно сделать. Без эмодзи — в длинном
    пути они мешают читать сам вопрос.
    """
    node = session.current_node()

    # Финал: явный Final-узел или ветка закончилась листом (current_node None).
    if session.is_completed or (node is not None and node.type == NodeType.FINAL):
        return summary_caption(session)

    if node is None:
        return session.breadcrumb_text()

    lines: List[str] = []

    if session.path:
        lines.append(session.breadcrumb_text())
        lines.append("")

    lines.append(f"{node.title}{_progress_bar(session)}")

    if node.description:
        lines.append(node.description)

    waiting = _waiting_hint(session)

    if waiting:
        lines.append("")
        lines.append(waiting)
        return "\n".join(lines)

    if node.type == NodeType.MULTI:
        lines.append(f"Selected: {len(session.draft)}")

    return "\n".join(lines)


def _waiting_hint(session: Session) -> str:
    """
    Явное приглашение к вводу для текстовых шагов.

    Возвращает пустую строку для шагов с кнопками: там подсказка не нужна
    и только занимала бы место.
    """
    node = session.current_node()

    if node is None:
        return ""

    if node.is_stub and not node.options:
        example = node.stub_hint or "Describe the problem in words"

        return (
            "Send your answer in one message.\n"
            f"{example}"
        )

    if node.type == NodeType.NUMBER:
        example = node.placeholder or "Enter a number"

        if node.min_value is not None or node.max_value is not None:
            low = "−∞" if node.min_value is None else node.min_value
            high = "∞" if node.max_value is None else node.max_value
            example += f" ({low}…{high})"

        return "Send your answer in one message.\n" + example

    if node.type == NodeType.INPUT:
        example = node.placeholder or "Describe the problem in words"

        return "Send your answer in one message.\n" + example

    return ""


def summary_caption(session: Session) -> str:
    """Финальный экран: полный выбранный путь парами «поле — значение»."""
    lines = [session.summary_title(), ""]

    for row in session.summary():
        lines.append(f"{row['field']}: {row['value']}")

    lines.append("")
    lines.append("Check and confirm.")

    return "\n".join(lines)


def render(session: Session, chat_id, message_id, edit: bool = True) -> None:
    """Рисует текущий шаг на сообщении с фото, плавно заменяя контент."""
    caption = step_caption(session)
    keyboard = keyboard_with_breadcrumb(session)

    if not edit:
        return

    edit_caption(chat_id, message_id, caption, session, reply_markup=keyboard)


def edit_caption(chat_id, message_id, caption, session=None, reply_markup=None):
    """
    Меняет подпись и кнопки сообщения с фото.

    Почему не tg.edit_message_caption: в тестовом боте включён DRY_RUN, и
    telegram_api пропускает ответ только если в payload есть
    message_thread_id (по нему проверяется топик из белого списка). Общая
    функция thread_id не передаёт, поэтому перерисовки молча терялись бы.
    Здесь добавляем топик из сессии — и дерево видно в Telegram.

    Вне dry-run вызов идёт тем же путём и ведёт себя как обычно, поэтому
    отдельная реализация безопасна и для боевого режима.
    """
    thread_id = None

    if session is not None:
        thread_id = session.data.get("thread_id")

    from telegram_api import TELEGRAM_CAPTION_LIMIT
    from text_utils import truncate

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": truncate(caption, TELEGRAM_CAPTION_LIMIT),
    }

    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return tg.call("editMessageCaption", payload, timeout=15)


# ============================================================
# ТОЧКИ ВХОДА (вызывает telegram_bot.py)
# ============================================================

def _delivered(result) -> bool:
    """
    Реально ли Telegram принял отправку.

    Важный случай: в dry-run заблокированный вызов возвращает заглушку
    {"message_id": 0, "dry_run": True}. Она «правдива», поэтому проверять
    только `if not sent` нельзя — иначе бот удалит фото сотрудника, ничего
    не показав взамен. Считаем доставкой только настоящий message_id.
    """
    if not isinstance(result, dict):
        return False

    return bool(result.get("message_id"))


def _active_tree():
    """
    Дерево в работе: базовое + правки, добавленные редакторами.

    Наложение применяется на каждый показ, поэтому новая ветка,
    добавленная из бота, видна сразу — перезапуск не нужен. Если overlay
    недоступен (нет файла, ошибка чтения), отдаём базовое дерево: приём
    ошибок не должен ломаться из-за правок.
    """
    base = load_tree(os.environ.get("EQUIPMENT_INTAKE_TREE_SOURCE", "").strip() or None)

    try:
        from .storage import apply_overlay

        return apply_overlay(base)
    except Exception:
        logger.exception("equipment intake: не удалось наложить правки редакторов")
        return base


def start(chat_id, sender, image_path: str, message_id, warehouse: str = "") -> bool:
    """
    Показывает дерево сразу после загрузки фото.

    Возвращает True, если перехватили обработку: тогда production-флоу
    меню типа ошибки не выполняется.
    """
    tree = _active_tree()
    session = Session(tree=tree)
    session.data.update({
        "image": image_path,
        "chat_id": chat_id,
        "warehouse": warehouse,
        "message_id": message_id,
        "thread_id": _bot()._reply_thread(chat_id),
        "note": "",
    })

    sent = tg.send_photo(
        chat_id,
        image_path,
        caption=step_caption(session),
        message_thread_id=session.data["thread_id"],
        reply_markup=keyboard_with_breadcrumb(session),
    )

    if not _delivered(sent):
        # Дерево не показано. Фото сотрудника НЕ удаляем и не оставляем
        # его без ответа: иначе в чате не остаётся ни фото, ни меню.
        logger.warning(
            "equipment intake: sendPhoto не доставлено (thread=%s) — "
            "фото сотрудника оставлено, откатываюсь на обычный флоу",
            session.data.get("thread_id"),
        )
        return False

    session.data["menu_message_id"] = sent.get("message_id")
    put_session(chat_id, sender.get("id"), session)

    # Старое сообщение сотрудника убираем только после успешной отправки.
    _bot()._delete_user_message(chat_id, message_id)

    return True


def handle_callback(chat_id, sender, parts, message_id, callback_id) -> bool:
    """Нажатие кнопки дерева. False — это не наша кнопка."""
    if not parts or parts[0] != CB_PREFIX:
        return False

    if callback_id:
        tg.answer_callback_query(callback_id)

    session = get_session(chat_id, sender.get("id"))

    if session is None:
        if callback_id:
            tg.answer_callback_query(
                callback_id,
                "This menu is outdated — send the photo again",
            )
        _bot()._delete_quiet(chat_id, message_id)
        return True

    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    try:
        if action in (CB_SELECT, CB_TOGGLE):
            session.select(arg)
        elif action == CB_DONE:
            session.done_multi()
        elif action == CB_BACK:
            session.back()
        elif action == CB_CRUMB:
            session.navigate_to(int(arg or "0"))
        elif action == CB_RESTART:
            session.restart()
        elif action == CB_EDIT:
            session.back()
        elif action == CB_CANCEL:
            drop_session(chat_id, sender.get("id"))
            _bot()._delete_quiet(chat_id, message_id)
            return True
        elif action == CB_CONFIRM:
            _confirm(chat_id, sender, session, message_id)
            return True
    except EngineError as error:
        if callback_id:
            tg.answer_callback_query(callback_id, str(error)[:200])
        return True
    except ValueError:
        if callback_id:
            tg.answer_callback_query(callback_id, "Invalid action")
        return True

    _redraw(chat_id, session, message_id)
    return True


def handle_text(chat_id, sender, text: str, message_id) -> bool:
    """
    Текстовый ввод для шагов Input/Number.

    False — сейчас ждём не ответ дерева (обычный разбор ошибки).
    """
    session = get_session(chat_id, sender.get("id"))

    if session is None or not session.is_text_step():
        return False

    if text.strip() == CANCEL_LABEL:
        drop_session(chat_id, sender.get("id"))
        _bot()._delete_user_message(chat_id, message_id)
        return True

    try:
        session.submit_text(text)
    except EngineError as error:
        _bot()._send(
            chat_id,
            f"⚠️ {error}. Please try again.",
            reply_to_message_id=message_id,
            delete_after=10,
        )
        return True

    _bot()._delete_user_message(chat_id, message_id)
    _redraw(chat_id, session, session.data.get("menu_message_id"))
    return True


def _redraw(chat_id, session: Session, message_id) -> None:
    if message_id is None:
        return

    render(session, chat_id, message_id, edit=True)


def _employee_context(telegram_id):
    """
    (имя из привязки, строка сотрудника) за один проход.

    `get_employee_context` читает обе таблицы по одному разу. Если модуль
    подменён (тесты) и такого помощника в нём нет, откатываемся на имя: без
    card_id отчёт всё равно сохраняется.
    """
    import telegram_store

    context = getattr(telegram_store, "get_employee_context", None)

    if context is None:
        return telegram_store.get_employee_name(telegram_id), None

    return context(telegram_id)


def save_status_text(delivery: Dict[str, Any]) -> str:
    """
    Status sentences about what happened to the report, in plain words.

    A refused warehouse is reported first and in plain words: the operator has
    to know the report was not filed, and why. Everything else is status.
    """
    if delivery.get("glpc_error"):
        status = f"⚠️ Not filed in the journal: {delivery['glpc_error']}"
    elif delivery.get("glpc_saved"):
        status = "Journal entry saved."
    else:
        status = "Journal entry not saved; check bot logs."

    if delivery.get("database_saved"):
        status += " Details saved to Supabase."
    else:
        status += " Details save failed."
    if delivery.get("lark_delivered"):
        status += " Lark card sent."
    else:
        status += " Lark card was not delivered; check LARK_HOOK_ERROR settings."
    if delivery.get("device_queued"):
        # **Говорим, ГДЕ разбирать.** Прежний текст «Device added to the add
        # queue.» был правдив, но бесполезен: человек читал его и не знал, куда
        # идти. Владелец: «текст бота правдив, но не говорит, где разбирать».
        #
        # Кнопки разбора появились 07.10.2026 (`review.decide`), поэтому путь
        # теперь существует и его можно назвать точно.
        status += (
            " The device is not in the registry.\n"
            "Open Equipment → Needs review in the app to accept or reject it."
        )

    return status


def _card_identity(session: Session) -> str:
    """
    One compact line: what was reported, and its number when there is one.

    The Telegram card is deliberately brief — the full path already goes to the
    Lark group and to the journal. This line keeps the two facts an operator
    rereads in the chat ("what", "which one") without duplicating the card.
    """
    from .report_writer import identifier, intake_category

    answers = session.answers()
    category = intake_category(answers.get("object"))
    label = str(answers.get("device_type") or "").strip()
    number = identifier(answers)

    parts = [part for part in (category, label, number) if part]

    return " · ".join(parts)


def confirmation_card(session: Session, delivery: Dict[str, Any]) -> str:
    """
    Краткая карточка-подтверждение, остающаяся в Telegram.

    Она заменяет сообщение с деревом (сообщение сотрудника удаляется при
    старте), поэтому путь целиком здесь не повторяется: сотрудника интересует
    «сохранилось или нет» и что именно он отправил. Полная сводка уходит в
    Lark-группу и в журнал.
    """
    lines = [
        "✅ Error report saved" if delivery.get("glpc_saved")
        else "⚠️ Error report not saved",
        save_status_text(delivery),
    ]

    identity = _card_identity(session)

    if identity:
        lines.append("")
        lines.append(identity)

    return "\n".join(lines)


def _confirm(chat_id, sender, session: Session, message_id) -> None:
    """
    Confirm: сохраняем сырые данные в Supabase и независимо отправляем
    карточку через Lark webhook.

    Пишем не только выбор, но и контекст: кто сообщил, где, что написал
    словами. Через 1–2 недели по этим записям видно, какие формулировки и
    модули повторяются — из них делаются готовые варианты кнопок.
    """
    # **Незавершённую сессию не пишем.**
    #
    # Дефект, найденный 07.10.2026: `_confirm` брал `session.result()` и сразу
    # писал строку, **не проверяя полноту**. Если подтверждение приходило до
    # конца пути (кнопка Confirm висит на финальном экране, но callback можно
    # отправить и раньше — например, повторным нажатием из старого сообщения),
    # в базу уходила запись с **пустым описанием и без устройства**.
    #
    # Проверено на живой базе: **11 таких строк** с 04.10, у всех
    # `issue_description = 'Reported error: '` и `solving_time = 0`. В отчёте
    # они попадали как ошибки без типа и устройства и портили и топ устройств, и
    # среднее время.
    #
    # Отказ мягкий: сообщаем человеку, что путь не закончен, и оставляем сессию
    # живой — он допишет ответ и подтвердит снова. Терять уже введённое нельзя.
    if not session.is_completed:
        node = session.current_node()
        missing = node.title if node is not None else "the last step"

        _bot()._send(
            chat_id,
            f"⚠️ Finish «{missing}» first, then confirm.",
            reply_to_message_id=message_id,
            delete_after=10,
        )
        return

    result = session.result()
    result["warehouse"] = session.data.get("warehouse") or ""
    result["note"] = session.data.get("note") or ""
    result["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    result["thread_id"] = session.data.get("thread_id")
    result["user_id"] = sender.get("id")
    result["username"] = sender.get("username") or ""
    result["message_id"] = session.data.get("message_id")
    result["chat_id"] = chat_id

    # Имя и card_id берём одним проходом: база может быть недоступна, и тогда
    # отчёт всё равно должен сохраниться — как раньше вёл себя только `employee`.
    # `get_employee_context` читает каждую таблицу один раз, поэтому `add_by`
    # не стоит второго запроса в базу.
    try:
        employee_name, employee = _employee_context(sender.get("id"))
        result["employee"] = employee_name or ""

        card_id = (employee or {}).get("card_id")

        if card_id not in ("", None):
            result["employee_card_id"] = card_id
    except Exception:
        logger.exception(
            "equipment intake: не удалось получить имя/card_id сотрудника"
        )
        result["employee"] = ""

    # Delivery to Lark is attempted independently from Supabase persistence.
    try:
        from .integrations import persist_and_send

        delivery = persist_and_send(result, session.data.get("image") or "")
    except Exception:
        logger.exception("equipment intake: integration flow failed")
        delivery = {
            "database_saved": False,
            "device_queued": False,
            "lark_delivered": False,
            "glpc_saved": False,
            "glpc_error": None,
        }

    result["integration"] = delivery

    drop_session(chat_id, sender.get("id"))

    # A short card stays in the topic: the employee's own message was deleted at
    # the start of the flow, and the status-only text says whether the report was
    # filed and what it was about.
    edit_caption(chat_id, message_id, confirmation_card(session, delivery), session, reply_markup=None)

    logger.info("Equipment intake confirmed; integration results=%s", json.dumps(delivery, ensure_ascii=False))
