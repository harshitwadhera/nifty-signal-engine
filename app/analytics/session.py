from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}


def local(value):
    if value.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(IST)


def bounds(value, interval):
    value = local(value)
    opening = datetime.combine(value.date(), time(9, 15), IST)
    closing = datetime.combine(value.date(), time(15, 30), IST)
    if value.weekday() >= 5 or not opening <= value < closing:
        return None
    minutes = int((value - opening).total_seconds() // 60)
    size = INTERVALS[interval]
    start = opening + timedelta(minutes=(minutes // size) * size)
    return start, min(start + timedelta(minutes=size), closing)
