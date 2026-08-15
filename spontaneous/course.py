from typing import Set


def classify_place_role(
    place_themes: Set[str],
) -> str:
    """
    장소 테마 기반 역할 분류

    역할:
    - ACTIVITY : 관광, 산책, 바다
    - MEAL     : 음식점
    - CAFE     : 카페
    - NIGHT_VIEW : 야경
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
    장소 리스트를 역할별로 그룹화

    예:
    {
        "ACTIVITY": [...],
        "MEAL": [...],
        "CAFE": [...]
    }
    """

    grouped = {}

    for place in places:
        role = classify_place_role(
            place.get("themes", set())
        )

        if role not in grouped:
            grouped[role] = []

        grouped[role].append(place)

    return grouped


def calculate_place_score(
    place: dict,
    desired_themes: set[str],
) -> float:
    """
    장소 테마 적합도 점수

    현재:
    테마 매칭 비율만 계산

    예:
    장소:
    {FOOD, SEAFOOD}

    요청:
    {SEAFOOD, SEA}

    결과:
    1 / 2 = 0.5
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

    return len(matched) / len(desired_themes)


def select_best_place(
    places: list[dict],
    desired_themes: set[str],
) -> dict | None:
    """
    역할별 후보 중 가장 적합한 장소 선택
    """

    if not places:
        return None

    return max(
        places,
        key=lambda place:
            calculate_place_score(
                place,
                desired_themes,
            )
    )


def generate_course(
    grouped_places: dict[str, list[dict]],
    desired_themes: set[str],
) -> list[dict]:
    """
    역할 기반 코스 생성

    현재 규칙:

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

        places = grouped_places.get(role)

        if not places:
            continue

        selected = select_best_place(
            places,
            desired_themes,
        )

        if not selected:
            continue

        course.append(
            {
                "order": order,
                "role": role,
                "name": selected.get("name")
                or selected.get("title"),
                "stayMinutes": stay_minutes,
                "themes": list(
                    selected.get(
                        "themes",
                        set()
                    )
                ),
            }
        )

        order += 1

    return course
    