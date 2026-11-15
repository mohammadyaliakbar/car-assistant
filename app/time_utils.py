from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=3, minutes=30), "IRST")


def to_local(dt: datetime) -> datetime:
    """datetime بدون timezone به‌عنوان UTC در نظر گرفته می‌شود."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def format_local(dt: datetime) -> str:
    return to_local(dt).strftime("%Y-%m-%d %H:%M:%S") + " (به وقت ایران)"
