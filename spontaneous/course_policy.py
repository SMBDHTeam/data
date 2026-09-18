"""Shared stop-count policy for spontaneous destination and course planning."""

from datetime import datetime


MAX_COURSE_STOPS = 4
COURSE_STOP_POLICY = (
    (120, 1, 2),
    (240, 2, 3),
    (None, 3, MAX_COURSE_STOPS),
)


def course_stop_range(onsite_minutes: int) -> tuple[int, int]:
    """Return the inclusive stop target for time actually available onsite."""
    return next(
        (minimum, maximum)
        for upper, minimum, maximum in COURSE_STOP_POLICY
        if upper is None or onsite_minutes < upper
    )


def calculate_onsite_minutes(
    start_at: datetime,
    return_by: datetime,
    outbound_minutes: int,
    return_minutes: int,
) -> int:
    """Subtract both destination travel legs from the full trip window."""
    total_minutes = int((return_by - start_at).total_seconds() // 60)
    return max(0, total_minutes - outbound_minutes - return_minutes)
