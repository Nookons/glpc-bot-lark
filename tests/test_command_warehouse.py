# -*- coding: utf-8 -*-
"""Склад в командах `/week` и `/digest` — аргументом, как у `/stats`.

🔴 **Что было.** Обе команды не принимали `args`, поэтому `/week sp3` молча
отвечал отчётом GLP-C: склад брался из текущего топика (`current_warehouse`), а в
топике Stats это GLP-C. Владелец видел цифры чужого склада без предупреждения
(аудит 17.11.2026).
"""

from __future__ import annotations

import telegram_bot as bot

CHAT_ID = -500


def test_week_uses_the_warehouse_from_arguments(monkeypatch) -> None:
    seen: dict = {}

    monkeypatch.setattr(bot, "current_warehouse", lambda: "GLP-C")
    monkeypatch.setattr(bot, "_send", lambda *a, **k: None)
    monkeypatch.setattr(
        bot.analytics, "weekly_text", lambda warehouse=None: seen.update(w=warehouse) or "x"
    )

    bot._handle_week(CHAT_ID, "sp3", None)

    assert seen["w"] == "SMALL-P3"


def test_week_without_arguments_keeps_the_topic_warehouse(monkeypatch) -> None:
    seen: dict = {}

    monkeypatch.setattr(bot, "current_warehouse", lambda: "GLP-C")
    monkeypatch.setattr(bot, "_send", lambda *a, **k: None)
    monkeypatch.setattr(
        bot.analytics, "weekly_text", lambda warehouse=None: seen.update(w=warehouse) or "x"
    )

    bot._handle_week(CHAT_ID, "", None)

    assert seen["w"] == "GLP-C"


def test_digest_uses_the_warehouse_from_arguments(monkeypatch) -> None:
    seen: dict = {}

    monkeypatch.setattr(bot, "current_warehouse", lambda: "GLP-C")
    monkeypatch.setattr(bot, "_send", lambda *a, **k: None)
    monkeypatch.setattr(
        bot.digests, "build_digest", lambda warehouse=None: seen.update(w=warehouse) or "x"
    )

    bot._handle_digest(CHAT_ID, "glpc", None)

    assert seen["w"] == "GLP-C"
