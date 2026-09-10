"""Internal contextual ranking; never supplies places, routes or opening hours."""

from datetime import datetime
from enum import Enum
from typing import Iterable

from spontaneous.models import TransportMode
from spontaneous.time_window import KOREA_TIMEZONE


class TimeProfile(str, Enum):
    DAYTIME = "DAYTIME"
    AFTERNOON = "AFTERNOON"
    SUNSET = "SUNSET"
    NIGHT = "NIGHT"
    LATE_NIGHT = "LATE_NIGHT"


# The existing service has no separate early-morning recommendation policy.
# Keep 00:00-08:59 contextual only; transport/opening-hours checks still decide
# feasibility. Bounds are inclusive starts, exclusive ends in Korea local time.
EARLY_MORNING_PROFILE = TimeProfile.LATE_NIGHT
TIME_PROFILE_HOUR_RANGES = (
    (0, 9, EARLY_MORNING_PROFILE),
    (9, 14, TimeProfile.DAYTIME),
    (14, 17, TimeProfile.AFTERNOON),
    (17, 19, TimeProfile.SUNSET),
    (19, 22, TimeProfile.NIGHT),
    (22, 24, TimeProfile.LATE_NIGHT),
)
TIME_PROFILE_THEME_WEIGHTS = {
    TimeProfile.DAYTIME: {
        "CULTURE": 0.20, "NATURE": 0.20, "ACTIVITY": 0.15, "CAFE": 0.15,
        "NIGHT_VIEW": -0.10,
    },
    TimeProfile.AFTERNOON: {
        "CAFE": 0.25, "SEA": 0.20, "WALK": 0.20, "NATURE": 0.15,
    },
    TimeProfile.SUNSET: {
        "SEA": 0.25, "WALK": 0.20, "FOOD": 0.15, "NIGHT_VIEW": 0.20,
        "CULTURE": -0.10, "SHOPPING": -0.10,
    },
    TimeProfile.NIGHT: {
        "NIGHT_VIEW": 0.25, "SEA": 0.15, "FOOD": 0.15,
        "CULTURE": -0.10, "SHOPPING": -0.10, "CAFE": -0.10,
    },
    TimeProfile.LATE_NIGHT: {
        "NIGHT_VIEW": 0.25, "SEA": 0.15, "FOOD": 0.15,
        "CAFE": -0.15, "CULTURE": -0.15, "SHOPPING": -0.15, "ACTIVITY": -0.10,
    },
}
CAR_SCENIC_PROFILES = frozenset({TimeProfile.SUNSET, TimeProfile.NIGHT, TimeProfile.LATE_NIGHT})
CAR_SCENIC_THEMES = frozenset({"SEA", "NIGHT_VIEW", "NATURE", "WALK"})
CAR_SCENIC_BONUS = 0.03
EXPLICIT_THEME_TIME_SCALE = 0.20
EMPTY_THEME_TIME_SCALE = 1.50
DESTINATION_FINAL_TIME_SCALE = 0.50


def resolve_time_profile(start_at: datetime | None) -> TimeProfile | None:
    """Resolve an instant in KST, without consulting the clock.

    Request-time validation rejects naive API inputs. Internal callers without
    an aware visit time still receive neutral ranking, never an assumed offset.
    This function does not validate today's departure policy: a valid course may
    visit a stop after midnight, and that visit must still resolve LATE_NIGHT.
    """
    if start_at is None or start_at.tzinfo is None or start_at.utcoffset() is None:
        return None
    hour = start_at.astimezone(KOREA_TIMEZONE).hour
    return next(profile for lower, upper, profile in TIME_PROFILE_HOUR_RANGES
                if lower <= hour < upper)


def calculate_time_fit_bonus(
    themes: Iterable[str],
    desired_themes: Iterable[str],
    profile: TimeProfile | None,
    transport_mode: TransportMode | None,
) -> float:
    """Average theme signals to avoid rewarding tag count; add CAR bonus once.

    With explicit themes the range is [-0.03, 0.056], versus existing user
    theme coefficients of 0.8/0.7. Without themes it is [-0.225, 0.42], so time
    can guide an otherwise distance-dominated recommendation. Requested theme
    coverage is ranked first by callers, and a matching place is never penalized.
    """
    if profile is None:
        return 0.0
    themes = {theme.upper() for theme in themes}
    desired = {theme.upper() for theme in desired_themes}
    if not themes:
        return 0.0
    weights = TIME_PROFILE_THEME_WEIGHTS[profile]
    signal = sum(max(0.0, weights.get(theme, 0.0)) if theme in desired
                 else weights.get(theme, 0.0) for theme in sorted(themes)) / len(themes)
    if transport_mode == TransportMode.CAR and profile in CAR_SCENIC_PROFILES:
        if themes & CAR_SCENIC_THEMES:
            signal += CAR_SCENIC_BONUS
    if themes & desired:
        signal = max(0.0, signal)
    return signal * (EXPLICIT_THEME_TIME_SCALE if desired else EMPTY_THEME_TIME_SCALE)
