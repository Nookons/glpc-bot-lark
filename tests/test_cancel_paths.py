# -*- coding: utf-8 -*-
"""`/cancel` отменяет **все** открытые диалоги, а не только часть.

🔴 **Что было.** Команда чистила осмотр, смену статуса и ожидающее фото, но не
трогала дерево приёма ошибок: бот отвечал «Nothing to cancel», а следующий
текст продолжал старый отчёт. Для владельца это выглядит как «Cancel не
работает» — тот же класс, что отмена осмотра без закрытия черновика на сервере
(18.11.2026).

У приёма нет серверной записи до `Confirm`, поэтому отмена — это удаление
сессии и карточки из чата; проверяем именно их.
"""

from __future__ import annotations

import pytest

import equipment_intake.flow as flow
import telegram_bot as bot
from equipment_intake.engine import Session
from equipment_intake.tree_config import DEFAULT_TREE

CHAT_ID = -500
USER_ID = 100


@pytest.fixture()
def sent(monkeypatch):
    """Перехватывает отправку и удаление сообщений."""
    messages: list[dict] = []
    deleted: list[tuple] = []

    def fake_send(chat_id, text, reply_to_message_id=None, **kwargs):
        messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return {"message_id": len(messages) + 100}

    def fake_delete(chat_id, message_id):
        deleted.append((chat_id, message_id))
        return True

    monkeypatch.setattr(bot.tg, "send_message", fake_send)
    monkeypatch.setattr(bot, "_delete_quiet", fake_delete)
    # Осмотра в этом сценарии нет: его `cancel` не должен перехватить команду.
    monkeypatch.setattr(bot, "inspection", None, raising=False)

    flow.reset_state()
    yield {"messages": messages, "deleted": deleted}
    flow.reset_state()


def _intake_session() -> Session:
    session = Session(tree=DEFAULT_TREE)
    session.data["menu_message_id"] = 777
    flow.put_session(CHAT_ID, USER_ID, session)
    return session


def test_cancel_drops_the_intake_dialog(sent) -> None:
    """`/cancel` закрывает дерево приёма и убирает его карточку."""
    _intake_session()

    handled = bot._handle_cancel(CHAT_ID, {"id": USER_ID}, None)

    assert handled is None or handled is True
    assert flow.get_session(CHAT_ID, USER_ID) is None
    assert (CHAT_ID, 777) in sent["deleted"], "карточка приёма осталась в чате"
    assert sent["messages"], "бот не ответил на /cancel"
    assert "Cancelled" in sent["messages"][-1]["text"]
    assert "Nothing to cancel" not in sent["messages"][-1]["text"]


def test_drop_session_returns_the_session() -> None:
    """Сброс сессии возвращает объект: иначе нечего удалять из чата."""
    _intake_session()

    dropped = flow.drop_session(CHAT_ID, USER_ID)

    assert dropped is not None
    assert flow.drop_session(CHAT_ID, USER_ID) is None
