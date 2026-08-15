from typing import Set
from math import asin, cos, radians, sin, sqrt




EARTH_RADIUS_METERS = 6371000


def calculate_distance_meters(
    origin: dict,
    destination: dict,
) -> float:
    """
    두 좌표 거리 계산
    """

    lat1 = radians(
        origin["latitude"]
    )

    lon1 = radians(
        origin["longitude"]
    )

    lat2 = radians(
        destination["latitude"]
    )

    lon2 = radians(
        destination["longitude"]
    )


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

    if "SEA" in place_themes or "WALK" in place_themes:
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

        if (
            "SEA" in themes
            or "WALK" in themes
        ):
            return 1.0


    if role == "NIGHT_VIEW":

        if "NIGHT_VIEW" in themes:
            return 1.0


    return 0.0



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


    final_score = (
        theme_score * 0.4
        +
        type_score * 0.2
        +
        distance_score * 0.4
    )


    return final_score



def select_best_place(
    places: list[dict],
    desired_themes: set[str],
    role: str,
    current_location,
) -> dict | None:
    """
    역할별 후보 중
    가장 적합한 장소 선택
    """

    if not places:
        return None


    return max(
        places,
        key=lambda place:
            calculate_place_score(
                place,
                desired_themes,
                role,
                current_location,
            )
    )



def generate_course(
    grouped_places: dict[str, list[dict]],
    desired_themes: set[str],
    current_location,
) -> list[dict]:
    """
    코스 생성

    기본 패턴:

    ACTIVITY
        ↓
    MEAL
        ↓
    CAFE
        ↓
    NIGHT_VIEW

    """

    course = []

    order = 1


    patterns = [
        ("ACTIVITY", 60),
        ("MEAL", 90),
        ("CAFE", 60),
        ("NIGHT_VIEW", 40),
    ]


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
            current_location,
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
                            current_location,
                        ),
                        4
                    ),
            }
        )


        order += 1


    return course