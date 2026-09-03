# 현재 위치 기준 권역 ㅊㅊ로직

from math import asin, cos, radians, sin, sqrt

from spontaneous.destinations import DESTINATION_ZONES, DestinationZone
from spontaneous.models import Coordinate
from spontaneous.course import (
    can_cover_required_themes_for_role,
    get_required_roles,
    get_required_themes_by_role,
    group_places_by_role,
    has_required_theme_coverage,
)
from spontaneous.places import filter_course_candidates, infer_place_themes

EARTH_RADIUS_METERS = 6_371_000
DESTINATION_PRERANK_THEME_WEIGHT = 0.8
DESTINATION_PRERANK_DISTANCE_WEIGHT = 0.2
DESTINATION_FINAL_THEME_WEIGHT = 0.7
DESTINATION_FINAL_TRAVEL_WEIGHT = 0.2
DESTINATION_FINAL_STAY_WEIGHT = 0.1
MAX_DESTINATION_RECOMMENDATIONS = 5


def distance_meters(origin: Coordinate, destination: Coordinate) -> float:
    lat1 = radians(origin.latitude)
    lon1 = radians(origin.longitude)
    lat2 = radians(destination.latitude)
    lon2 = radians(destination.longitude)

    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    haversine = (
        sin(delta_lat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    )

    central_angle = 2 * asin(sqrt(haversine))

    return EARTH_RADIUS_METERS * central_angle





def sort_destinations_by_distance(
    current_location: Coordinate,
) -> list[tuple[DestinationZone, float]]:
    results: list[tuple[DestinationZone, float]] = []

    for zone in DESTINATION_ZONES:
        zone_location = Coordinate(
            latitude=zone.center_latitude,
            longitude=zone.center_longitude,
        )

        distance = distance_meters(
            current_location,
            zone_location,
        )

        results.append((zone, distance))

    results.sort(key=lambda item: item[1])

    return results


def select_route_check_candidates(
    current_location: Coordinate,
    limit: int = 6,
) -> list[tuple[DestinationZone, float]]:
    sorted_zones = sort_destinations_by_distance(current_location)

    return sorted_zones[:limit]



def collect_zone_theme_evidence(
    places: list[dict],
) -> set[str]:
    evidence: set[str] = set()

    for place in filter_course_candidates(
        places
    ):
        evidence.update(
            infer_place_themes(place)
        )

    return evidence


def coarse_course_place(
    place: dict,
) -> dict:
    return {
        "name": place.get("title"),
        "contentId": place.get("contentid"),
        "contentTypeId": str(
            place.get("contenttypeid", "")
        ),
        "themes": infer_place_themes(place),
    }


def has_coarse_course_viability(
    places: list[dict],
    desired_themes: list[str],
) -> bool:
    if not places:
        return False

    desired_set = {
        theme.upper()
        for theme in desired_themes
    }

    if not desired_set:
        return True

    coarse_places = [
        coarse_course_place(place)
        for place in filter_course_candidates(
            places
        )
    ]

    if not coarse_places:
        return False

    grouped_places = group_places_by_role(
        coarse_places
    )
    required_roles = get_required_roles(
        desired_set
    )
    selected_roles = set(
        grouped_places.keys()
    )

    if not required_roles.issubset(
        selected_roles
    ):
        return False

    required_themes_by_role = get_required_themes_by_role(
        desired_set
    )

    for role, required_themes in required_themes_by_role.items():
        role_places = grouped_places.get(
            role,
            [],
        )

        if not can_cover_required_themes_for_role(
            role_places,
            role,
            required_themes,
        ):
            return False

    return has_required_theme_coverage(
        coarse_places,
        desired_set,
    )


def calculate_zone_theme_score(
    places: list[dict],
    desired_themes: list[str],
) -> float:
    if not desired_themes:
        return 0.0

    desired_set = {theme.upper() for theme in desired_themes}
    matched = desired_set.intersection(
        collect_zone_theme_evidence(places)
    )

    return len(matched) / len(desired_set)


def calculate_distance_score(distance_meters_value: float) -> float:
    distance_km = distance_meters_value / 1000

    return 1 / (1 + distance_km / 10)

def calculate_destination_score(
    zone: DestinationZone,
    start_location: Coordinate,
    theme_score: float,
) -> tuple[float, float, float]:
    zone_location = Coordinate(
        latitude=zone.center_latitude,
        longitude=zone.center_longitude,
    )

    distance = distance_meters(
        start_location,
        zone_location,
    )

    distance_score = calculate_distance_score(distance)

    final_score = (
        theme_score * DESTINATION_PRERANK_THEME_WEIGHT
        + distance_score * DESTINATION_PRERANK_DISTANCE_WEIGHT
    )

    return final_score, theme_score, distance
def calculate_travel_time_score(minutes: int) -> float:
    return 1 / (1 + minutes / 30)


def calculate_stay_time_score(minutes: int) -> float:
    return min(minutes / 180, 1.0)

def calculate_final_destination_score(
    theme_score: float,
    travel_minutes: int,
    stay_minutes: int,
) -> float:
    travel_score = calculate_travel_time_score(
        travel_minutes
    )

    stay_score = calculate_stay_time_score(
        stay_minutes
    )

    final_score = (
        theme_score * DESTINATION_FINAL_THEME_WEIGHT
        + travel_score * DESTINATION_FINAL_TRAVEL_WEIGHT
        + stay_score * DESTINATION_FINAL_STAY_WEIGHT
    )

    return final_score
