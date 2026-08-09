from datetime import datetime, timedelta

DAY_START = 6   # 06:00
DAY_END = 18    # 18:00


def get_current_shift(dt: datetime = None):
    """
    Возвращает (shift_date: str 'YYYY-MM-DD', shift_name: 'day'|'night')
    shift_date — дата НАЧАЛА смены.
    """
    dt = dt or datetime.now()
    hour = dt.hour

    if DAY_START <= hour < DAY_END:
        return dt.strftime("%Y-%m-%d"), "day"
    else:
        # ночная смена: если время после полуночи (00:00-06:00),
        # она относится к смене, начавшейся ВЧЕРА в 18:00
        if hour < DAY_START:
            shift_start_date = dt - timedelta(days=1)
        else:
            shift_start_date = dt
        return shift_start_date.strftime("%Y-%m-%d"), "night"