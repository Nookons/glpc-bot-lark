from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

DAY_START = 6   # 06:00
DAY_END = 18    # 18:00

def get_current_shift(dt: datetime = None):
    if dt is None:
        dt = datetime.now(WARSAW_TZ)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW_TZ)

    hour = dt.hour

    if DAY_START <= hour < DAY_END:
        return dt.strftime("%Y-%m-%d"), "day"

    if hour < DAY_START:
        shift_start_date = dt - timedelta(days=1)
    else:
        shift_start_date = dt

    return shift_start_date.strftime("%Y-%m-%d"), "night"