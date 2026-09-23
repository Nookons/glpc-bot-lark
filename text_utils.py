"""
Мелкие утилиты для текстов, уходящих пользователю.

Telegram отклоняет сообщения длиннее 4096 символов, Lark-карточки тоже имеют
пределы. Сотрудник может прислать очень длинное описание — обрезаем его сами,
чтобы ответ и карточка не терялись из-за ошибки API.
"""

from __future__ import annotations


TELEGRAM_TEXT_LIMIT = 4000
LARK_TEXT_LIMIT = 4000

# Длина одного значения в карточке/строке (описание ошибки, заметка).
VALUE_LIMIT = 500


def truncate(text, limit: int = VALUE_LIMIT, suffix: str = "…") -> str:
    """Обрезает текст до limit символов, добавляя многоточие."""
    text = "" if text is None else str(text)

    if limit <= 0 or len(text) <= limit:
        return text

    return text[: max(0, limit - len(suffix))].rstrip() + suffix
