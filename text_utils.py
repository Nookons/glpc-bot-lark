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


def utf16_length(text) -> int:
    """
    Длина строки в UTF-16 code units — так её считают Telegram и Lark.

    `len()` в Python считает code points. Символы вне BMP (эмодзи, некоторые
    иероглифы) занимают в UTF-16 ДВЕ единицы, поэтому строка из 3000 эмодзи
    имеет 6000 единиц и уже не влезает в лимит 4096, хотя `len()` показывал бы
    3000. Без этой меры обрезка не спасает, и сообщение теряется.
    """
    text = "" if text is None else str(text)

    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate(text, limit: int = VALUE_LIMIT, suffix: str = "…") -> str:
    """
    Обрезает текст так, чтобы результат влезал в limit UTF-16 единиц.

    limit меряется в UTF-16 code units (как у Telegram/Lark), а не в символах
    Python: иначе сообщение из эмодзи превышает реальный лимит API.
    """
    text = "" if text is None else str(text)

    if limit <= 0:
        return text

    if utf16_length(text) <= limit:
        return text

    budget = max(0, limit - utf16_length(suffix))
    kept: list = []
    used = 0

    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1

        if used + width > budget:
            break

        kept.append(ch)
        used += width

    return "".join(kept).rstrip() + suffix


def ascii_digits(value, allow_sign: bool = False) -> bool:
    """
    Состоит ли значение ТОЛЬКО из ASCII-цифр (необязательно со знаком).

    Зачем: `str.isdigit()` и `int()` принимают Unicode-цифры. Из-за этого
    «٣٧٨٠» проходит проверку «номер робота — это цифры» и превращается в
    int() как 3780 — то есть в ДРУГОЙ номер. Ошибка приписывается роботу,
    которого не сообщали. Проверять надо ASCII явно.
    """
    text = str(value or "").strip()

    if allow_sign:
        text = text.lstrip("-").lstrip("+")

    return bool(text) and text.isascii() and text.isdecimal()


def to_int(value):
    """int(value), если это ASCII-целое, иначе None (без исключений)."""
    text = str(value or "").strip()

    if not text:
        return None

    body = text.lstrip("-").lstrip("+")

    if not body.isascii() or not body.isdecimal():
        return None

    try:
        return int(text)
    except ValueError:
        return None
