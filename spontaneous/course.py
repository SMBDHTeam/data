from datetime import datetime, timedelta
from typing import Set
from math import asin, cos, radians, sin, sqrt

from spontaneous.models import Coordinate, TransportMode
from spontaneous.places import SEAFOOD_MENU_KEYWORDS
from spontaneous.routing import TravelMinutesCache, search_travel_minutes


EARTH_RADIUS_METERS = 6371000
PLACE_SCORE_THEME_WEIGHT = 0.35
PLACE_SCORE_TYPE_WEIGHT = 0.15
PLACE_SCORE_MENU_RELEVANCE_WEIGHT = 0.30
PLACE_SCORE_DISTANCE_WEIGHT = 0.20
ROLE_STAY_MINUTES = {
    "ACTIVITY": 60,
    "MEAL": 90,
    "CAFE": 60,
    "NIGHT_VIEW": 40,
}
DEFAULT_ROLE_ORDER = [
    "ACTIVITY",
    "MEAL",
    "CAFE",
    "NIGHT_VIEW",
]
THEME_REQUIRED_ROLE_MAP = {
    "SEA": {"ACTIVITY"},
    "WALK": {"ACTIVITY"},
    "NATURE": {"ACTIVITY"},
    "CULTURE": {"ACTIVITY"},
    "ACTIVITY": {"ACTIVITY"},
    "HEALING": {"ACTIVITY"},
    "SHOPPING": {"ACTIVITY"},
    "SEAFOOD": {"MEAL"},
    "FOOD": {"MEAL"},
    "CAFE": {"CAFE"},
    "NIGHT_VIEW": {"NIGHT_VIEW"},
}
COURSE_VERIFIABLE_THEMES = set(
    THEME_REQUIRED_ROLE_MAP.keys()
)
ACTIVITY_ROLE_THEMES = {
    "SEA",
    "WALK",
    "NATURE",
    "CULTURE",
    "ACTIVITY",
    "HEALING",
    "SHOPPING",
}
MIN_OPTIONAL_ROLE_BUFFER_MINUTES = 30


def calculate_distance_meters(
    origin: dict,
    destination: dict,
) -> float:
    """
    두 좌표 거리 계산
    """

    if hasattr(origin, "latitude"):
        lat1 = radians(origin.latitude)
        lon1 = radians(origin.longitude)
    else:
        lat1 = radians(origin["latitude"])
        lon1 = radians(origin["longitude"])


    if hasattr(destination, "latitude"):
        lat2 = radians(destination.latitude)
        lon2 = radians(destination.longitude)
    else:
        lat2 = radians(destination["latitude"])
        lon2 = radians(destination["longitude"])


    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1


    value = (
        sin(delta_lat / 2) ** 2
        +
        cos(lat1)
        *
        cos(lat2)
        *
        sin(delta_lon / 2) ** 2
    )


    central_angle = 2 * asin(
        sqrt(value)
    )


    return EARTH_RADIUS_METERS * central_angle



def calculate_distance_score(
    distance_meters: float,
) -> float:
    """
    가까울수록 높은 점수

    0km = 1
    10km 이상 = 낮음
    """

    km = distance_meters / 1000

    return 1 / (
        1 + km / 10
    )



def classify_place_role(
    place_themes: Set[str],
) -> str:
    """
    장소 테마 기반 역할 분류

    역할:
    ACTIVITY : 관광 / 산책 / 바다
    MEAL     : 음식
    CAFE     : 카페
    NIGHT_VIEW : 야경
    """

    if "SEAFOOD" in place_themes or "FOOD" in place_themes:
        return "MEAL"

    if "CAFE" in place_themes:
        return "CAFE"

    if "NIGHT_VIEW" in place_themes:
        return "NIGHT_VIEW"

    if ACTIVITY_ROLE_THEMES.intersection(
        place_themes
    ):
        return "ACTIVITY"

    return "ETC"



def group_places_by_role(
    places: list[dict],
) -> dict[str, list[dict]]:
    """
    장소를 역할별로 그룹화

    결과:

    {
        ACTIVITY: [],
        MEAL: [],
        CAFE: []
    }
    """

    grouped = {}

    for place in places:

        role = classify_place_role(
            place.get(
                "themes",
                set()
            )
        )

        if role not in grouped:
            grouped[role] = []

        grouped[role].append(place)

    return grouped



def calculate_theme_score(
    place: dict,
    desired_themes: set[str],
) -> float:
    """
    사용자 원하는 테마와
    장소 테마 매칭 점수

    예:

    원하는:
    SEAFOOD, SEA

    장소:
    SEAFOOD, FOOD

    결과:
    0.5
    """

    place_themes = place.get(
        "themes",
        set()
    )

    if not place_themes:
        return 0.0

    if not desired_themes:
        return 0.0

    matched = desired_themes.intersection(
        place_themes
    )


    return len(matched) / len(
        desired_themes
    )



def calculate_place_type_score(
    place: dict,
    role: str,
) -> float:
    """
    역할 적합도 점수
    """

    themes = place.get(
        "themes",
        set()
    )


    if role == "MEAL":

        if "FOOD" in themes:
            return 1.0


    if role == "CAFE":

        if "CAFE" in themes:
            return 1.0


    if role == "ACTIVITY":

        if ACTIVITY_ROLE_THEMES.intersection(
            themes
        ):
            return 1.0


    if role == "NIGHT_VIEW":

        if "NIGHT_VIEW" in themes:
            return 1.0


    return 0.0



def get_place_themes(
    place: dict,
) -> set[str]:
    return {
        str(theme).upper()
        for theme in place.get(
            "themes",
            set(),
        )
    }


def count_keyword_matches(
    text: str,
    keywords: list[str],
) -> int:
    return len(
        {
            keyword
            for keyword in keywords
            if keyword in text
        }
    )


def calculate_menu_relevance_score(
    place: dict,
    desired_themes: set[str],
    role: str,
) -> float:
    if role != "MEAL":
        return 0.0

    if "SEAFOOD" not in desired_themes:
        return 0.0

    place_themes = get_place_themes(
        place
    )

    if "SEAFOOD" not in place_themes:
        return 0.0

    detail = place.get(
        "_foodDetail",
        {},
    ) or {}

    firstmenu = str(
        detail.get(
            "firstmenu",
            "",
        )
    )
    treatmenu = str(
        detail.get(
            "treatmenu",
            "",
        )
    )

    firstmenu_matches = count_keyword_matches(
        firstmenu,
        SEAFOOD_MENU_KEYWORDS,
    )
    treatmenu_matches = count_keyword_matches(
        treatmenu,
        SEAFOOD_MENU_KEYWORDS,
    )

    if firstmenu_matches == 0 and treatmenu_matches == 0:
        return 0.2

    return min(
        1.0,
        firstmenu_matches * 0.45
        + min(treatmenu_matches, 4) * 0.12,
    )


def calculate_place_score(
    place: dict,
    desired_themes: set[str],
    role: str,
    current_location: dict,
) -> float:

    theme_score = calculate_theme_score(
        place,
        desired_themes,
    )

    type_score = calculate_place_type_score(
        place,
        role,
    )

    distance = calculate_distance_meters(
        current_location,
        {
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
        },
    )

    distance_score = calculate_distance_score(
        distance
    )

    menu_relevance_score = calculate_menu_relevance_score(
        place,
        desired_themes,
        role,
    )


    final_score = (
        theme_score * PLACE_SCORE_THEME_WEIGHT
        +
        type_score * PLACE_SCORE_TYPE_WEIGHT
        +
        menu_relevance_score * PLACE_SCORE_MENU_RELEVANCE_WEIGHT
        +
        distance_score * PLACE_SCORE_DISTANCE_WEIGHT
    )


    return final_score



def select_best_place(
    places: list[dict],
    desired_themes: set[str],
    role: str,
    current_location,
    required_themes: set[str] | None = None,
) -> dict | None:
    """
    역할별 후보 중
    가장 적합한 장소 선택
    """

    if not places:
        return None

    candidates = places
    required_themes = required_themes or set()

    if required_themes:
        fully_matched = [
            place
            for place in places
            if required_themes.issubset(
                get_place_themes(place)
            )
        ]

        if fully_matched:
            candidates = fully_matched
        else:
            partially_matched = [
                place
                for place in places
                if required_themes.intersection(
                    get_place_themes(place)
                )
            ]

            if partially_matched:
                candidates = partially_matched

    return max(
        candidates,
        key=lambda place:
            calculate_place_score(
                place,
                desired_themes,
                role,
                current_location,
            )
    )


def get_required_roles(
    desired_themes: set[str],
) -> set[str]:
    roles: set[str] = set()

    for theme in desired_themes:
        roles.update(
            THEME_REQUIRED_ROLE_MAP.get(
                theme.upper(),
                set(),
            )
        )

    return roles


def get_verifiable_requested_themes(
    desired_themes: set[str],
) -> set[str]:
    return {
        theme.upper()
        for theme in desired_themes
        if theme.upper() in COURSE_VERIFIABLE_THEMES
    }


def get_required_themes_by_role(
    desired_themes: set[str],
) -> dict[str, set[str]]:
    themes_by_role: dict[str, set[str]] = {}

    for theme in get_verifiable_requested_themes(
        desired_themes
    ):
        roles = THEME_REQUIRED_ROLE_MAP.get(
            theme,
            set(),
        )

        for role in DEFAULT_ROLE_ORDER:
            if role in roles:
                themes_by_role.setdefault(
                    role,
                    set(),
                ).add(theme)
                break

    return themes_by_role


def build_course_role_plan(
    desired_themes: set[str],
    available_minutes: int | None = None,
) -> list[tuple[str, int]]:
    required_roles = get_required_roles(
        desired_themes
    )
    plan: list[tuple[str, int]] = []
    planned_roles: set[str] = set()
    remaining_minutes = available_minutes

    def append_role(
        role: str,
        required: bool,
    ) -> None:
        nonlocal remaining_minutes

        if role in planned_roles:
            return

        stay_minutes = ROLE_STAY_MINUTES[role]

        if (
            not required
            and remaining_minutes is not None
            and remaining_minutes
            < stay_minutes + MIN_OPTIONAL_ROLE_BUFFER_MINUTES
        ):
            return

        plan.append(
            (
                role,
                stay_minutes,
            )
        )
        planned_roles.add(role)

        if remaining_minutes is not None:
            remaining_minutes -= stay_minutes

    for role in DEFAULT_ROLE_ORDER:
        if role in required_roles:
            append_role(
                role,
                required=True,
            )

    for role in DEFAULT_ROLE_ORDER:
        if role not in required_roles:
            append_role(
                role,
                required=False,
            )

    return plan


def has_required_course_roles(
    course: list[dict],
    required_roles: set[str],
) -> bool:
    selected_roles = {
        item.get("role")
        for item in course
    }

    return required_roles.issubset(
        selected_roles
    )


def get_course_themes(
    course: list[dict],
) -> set[str]:
    course_themes: set[str] = set()

    for item in course:
        course_themes.update(
            get_place_themes(item)
        )

    return course_themes


def get_missing_required_themes(
    course: list[dict],
    desired_themes: set[str],
) -> set[str]:
    required_themes = get_verifiable_requested_themes(
        desired_themes
    )

    return required_themes - get_course_themes(
        course
    )


def has_required_theme_coverage(
    course: list[dict],
    desired_themes: set[str],
) -> bool:
    return not get_missing_required_themes(
        course,
        desired_themes,
    )



def generate_course(
    grouped_places: dict[str, list[dict]],
    desired_themes: set[str],
    current_location,
    role_plan: list[tuple[str, int]] | None = None,
    required_themes_by_role: dict[str, set[str]] | None = None,
) -> list[dict]:
    """
    코스 생성

    desired theme로 만든 role plan 순서에 따라 stop을 선택한다.
    """

    course = []
    order = 1

    if hasattr(current_location, "latitude"):
        cursor_location = {
            "latitude": current_location.latitude,
            "longitude": current_location.longitude,
        }
    else:
        cursor_location = dict(current_location)


    if role_plan is None:
        patterns = [
            (
                role,
                ROLE_STAY_MINUTES[role],
            )
            for role in DEFAULT_ROLE_ORDER
        ]
    else:
        patterns = role_plan


    for role, stay_minutes in patterns:

        places = grouped_places.get(
            role
        )


        if not places:
            continue


        selected = select_best_place(
            places,
            desired_themes,
            role,
            cursor_location,
            required_themes=(
                required_themes_by_role or {}
            ).get(
                role,
                set(),
            ),
        )


        if not selected:
            continue


        course.append(
            {
                "order": order,
                "role": role,
                "name":
                    selected.get("name")
                    or selected.get("title"),
                "contentId": selected.get("contentId"),
                "latitude": selected.get("latitude"),
                "longitude": selected.get("longitude"),
                "contentTypeId": selected.get("contentTypeId"),

                "stayMinutes": stay_minutes,

                "themes": list(
                    selected.get(
                        "themes",
                        set()
                    )
                ),

                "score":
                    round(
                        calculate_place_score(
                            selected,
                            desired_themes,
                            role,
                            cursor_location,
                        ),
                        4
                    ),
            }
        )

        cursor_location = {
            "latitude": selected.get("latitude"),
            "longitude": selected.get("longitude"),
        }


        order += 1


    return course

def calculate_course_travel_minutes(
    course: list[dict],
    current_location: Coordinate,
    transport_mode: TransportMode,
    cache: TravelMinutesCache | None = None,
) -> list[dict]:
    """
    현재 위치 -> 장소1 -> 장소2 -> ... -> 현재 위치
    각 구간의 실제 이동시간을 계산한다.
    """

    if not course:
        return []

    result = []

    previous_location = current_location

    for item in course:
        place_location = Coordinate(
            latitude=item["latitude"],
            longitude=item["longitude"],
        )

        travel_minutes = search_travel_minutes(
            transport_mode,
            previous_location,
            place_location,
            cache=cache,
        )

        item_with_travel = {
            **item,
            "travelMinutesFromPrevious": travel_minutes,
        }

        result.append(item_with_travel)

        previous_location = place_location

    return_minutes = search_travel_minutes(
        transport_mode,
        previous_location,
        current_location,
        cache=cache,
    )

    if result:
        result[-1]["returnTravelMinutes"] = return_minutes

    return result


def has_complete_travel_minutes(
    course: list[dict],
) -> bool:
    if not course:
        return False

    for item in course:
        if item.get("travelMinutesFromPrevious") is None:
            return False

    return course[-1].get("returnTravelMinutes") is not None


def apply_course_timeline(
    course: list[dict],
    start_at: datetime,
) -> dict:
    """
    Adds arrival/departure timestamps and computes the final return time.
    """

    current_time = start_at
    timeline = []

    for item in course:
        travel_minutes = item.get(
            "travelMinutesFromPrevious"
        )

        if travel_minutes is None:
            raise ValueError("NO_ROUTE")

        arrival_at = current_time + timedelta(
            minutes=travel_minutes
        )

        departure_at = arrival_at + timedelta(
            minutes=item["stayMinutes"]
        )

        item_with_timeline = {
            **item,
            "arrivalAt": arrival_at.isoformat(),
            "departureAt": departure_at.isoformat(),
        }

        timeline.append(item_with_timeline)
        current_time = departure_at

    return_travel_minutes = timeline[-1].get(
        "returnTravelMinutes"
    )

    if return_travel_minutes is None:
        raise ValueError("NO_ROUTE")

    estimated_return_at = current_time + timedelta(
        minutes=return_travel_minutes
    )

    return {
        "course": timeline,
        "returnTravelMinutes": return_travel_minutes,
        "estimatedReturnAt": estimated_return_at,
    }


def normalize_course_orders(
    course: list[dict],
) -> list[dict]:
    return [
        {
            **item,
            "order": index + 1,
        }
        for index, item in enumerate(course)
    ]


def public_course_stop(
    stop: dict,
) -> dict:
    return {
        key: value
        for key, value in stop.items()
        if key != "raw" and not key.startswith("_")
    }
