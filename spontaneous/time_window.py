"""Request-time validation, separate from start/visit-time recommendation ranking."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")
START_TIME_PAST_TOLERANCE_MINUTES = 5
MAX_RETURN_HOUR_NEXT_DAY = 3


class SpontaneousTimeWindowError(ValueError):
    """Detailed internal reason; HTTP callers keep INVALID_TIME_RANGE public."""

    def __init__(self, failure_reason: str):
        super().__init__(failure_reason)
        self.failure_reason = failure_reason


def current_kst_time() -> datetime:
    """Single clock boundary, replaceable in tests without changing API fields."""
    return datetime.now(KOREA_TIMEZONE)


def validate_spontaneous_time_window(
    start_at: datetime,
    return_by: datetime,
    now: datetime | None = None,
) -> None:
    """Allow today's departure (up to five minutes old) and return by 03:00.

    All dates/bounds are evaluated in Asia/Seoul. An explicit offset is required
    for every instant, including an injected now. Inputs are neither modified
    nor clamped: ranking and routed timelines continue to use the supplied start.
    """
    now = current_kst_time() if now is None else now
    if any(value.tzinfo is None or value.utcoffset() is None
           for value in (start_at, return_by, now)):
        raise SpontaneousTimeWindowError("SPONTANEOUS_TIMEZONE_REQUIRED")

    try:
        start_kst = start_at.astimezone(KOREA_TIMEZONE)
        return_kst = return_by.astimezone(KOREA_TIMEZONE)
        now_kst = now.astimezone(KOREA_TIMEZONE)
    except (OverflowError, ValueError) as exc:
        raise SpontaneousTimeWindowError("INVALID_TIME_RANGE") from exc

    if return_kst <= start_kst:
        raise SpontaneousTimeWindowError("INVALID_TIME_RANGE")
    if start_kst.date() != now_kst.date():
        raise SpontaneousTimeWindowError("SPONTANEOUS_START_DATE_NOT_TODAY")
    if start_kst < now_kst - timedelta(minutes=START_TIME_PAST_TOLERANCE_MINUTES):
        raise SpontaneousTimeWindowError("SPONTANEOUS_START_TIME_IN_PAST")

    latest_return = datetime.combine(
        start_kst.date() + timedelta(days=1),
        time(hour=MAX_RETURN_HOUR_NEXT_DAY),
        tzinfo=KOREA_TIMEZONE,
    )
    if return_kst > latest_return:
        raise SpontaneousTimeWindowError("SPONTANEOUS_RETURN_TIME_TOO_LATE")
