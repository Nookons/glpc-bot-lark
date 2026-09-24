"""
Разбор ISO-времени, которое приходит из PostgREST/Supabase.

Postgres отдаёт доли секунды с обрезкой незначащих нулей: ".02344" (5 цифр),
".0234" (4), ".02" (2) и т.д. `datetime.fromisoformat` понимает только 3 или
6 цифр и падает на остальных — из-за этого:

  * heartbeat лиза мог выглядеть «живым» вечно (просроченный лиз никто не
    забирал после падения инстанса);
  * история робота и карточка статуса оставались без даты.

Поэтому нормализуем дробную часть до 6 цифр сами.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


_FRACTION = re.compile(r"\.(\d+)")

# Значения приходят либо с "Z", либо с "+HH:MM"/"+HHMM"/"+HH".
_ZULU = re.compile(r"Z$")
_TZ_SHORT = re.compile(r"([+-]\d{2})(\d{2})$")
_TZ_HOUR = re.compile(r"([+-]\d{2})$")


def parse_iso(value):
    """
    datetime (всегда с tzinfo) из ISO-строки или None, если разобрать нельзя.

    None означает «время непонятное», а не «время в прошлом»: вызывающий код
    сам решает, что безопаснее — считать объект живым или мёртвым.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()

    if not text:
        return None

    if _ZULU.search(text):
        text = text[:-1] + "+00:00"

    match = _FRACTION.search(text)

    if match:
        digits = (match.group(1) + "000000")[:6]
        text = text[:match.start(1)] + digits + text[match.end(1):]

    short = _TZ_SHORT.search(text)

    if short:
        text = text[:short.start(1)] + f"{short.group(1)}:{short.group(2)}"
    else:
        hour = _TZ_HOUR.search(text)

        if hour:
            text = text[:hour.start(1)] + f"{hour.group(1)}:00"

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment
