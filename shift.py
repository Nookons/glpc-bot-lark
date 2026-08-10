from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


WARSAW_TZ = ZoneInfo("Europe/Warsaw")

DAY_START = 6   # 06:00
DAY_END = 18    # 18:00


def get_current_shift(dt: datetime = None):
    """
    Возвращает:
        (shift_date: str 'YYYY-MM-DD', shift_name: 'day' | 'night')

    shift_date — дата НАЧАЛА смены.

    Все расчёты выполняются по времени Europe/Warsaw.
    """

    if dt is None:
        dt = datetime.now(WARSAW_TZ)
    elif dt.tzinfo is None:
        # Если передали datetime без timezone,
        # считаем его временем Варшавы.
        dt = dt.replace(tzinfo=WARSAW_TZ)

    hour = dt.hour

    # Дневная смена: 06:00 — 18:00
    if DAY_START <= hour < DAY_END:
        return dt.strftime("%Y-%m-%d"), "day"

    # Ночная смена
    # 18:00 — 06:00
    #
    # Например:
    # 10.08 20:00 → night, 10.08
    # 11.08 02:00 → night, 10.08

    if hour < DAY_START:
        shift_start_date = dt - timedelta(days=1)
    else:
        shift_start_date = dt

    return shift_start_date.strftime("%Y-%m-%d"), "night"