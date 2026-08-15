# 현재 위치 기준 권역 ㅊㅊ로직

from math import asin, cos, radians, sin, sqrt

from spontaneous.destinations import DESTINATION_ZONES, DestinationZone
from spontaneous.models import Coordinate
from spontaneous.models import TransportOption, TransportMode
from transit.routing import find_route

from decimal import Decimal

from transit.routing import TransitPoint

EARTH_RADIUS_METERS = 6_371_000


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



def calculate_theme_match_score(
    zone: DestinationZone,
    desired_themes: list[str],
) -> float:
    if not desired_themes:
        return 0.0

    desired_set = {theme.upper() for theme in desired_themes}
    matched = desired_set.intersection(zone.themes)

    return len(matched) / len(desired_set)


def calculate_theme_match_score(
    zone: DestinationZone,
    desired_themes: list[str],
) -> float:
    if not desired_themes:
        return 0.0

    desired_set = {theme.upper() for theme in desired_themes}
    matched = desired_set.intersection(zone.themes)

    return len(matched) / len(desired_set)


def calculate_distance_score(distance_meters_value: float) -> float:
    distance_km = distance_meters_value / 1000

    return 1 / (1 + distance_km / 10)

def calculate_destination_score(
    zone: DestinationZone,
    current_location: Coordinate,
    desired_themes: list[str],
) -> tuple[float, float, float]:
    theme_score = calculate_theme_match_score(
        zone,
        desired_themes,
    )

    zone_location = Coordinate(
        latitude=zone.center_latitude,
        longitude=zone.center_longitude,
    )

    distance = distance_meters(
        current_location,
        zone_location,
    )

    distance_score = calculate_distance_score(distance)

    final_score = (
        theme_score * 0.8
        + distance_score * 0.2
    )

    return final_score, theme_score, distance



def coordinate_to_transit_point(
    coordinate: Coordinate,
    name: str,
) -> TransitPoint:
    return TransitPoint(
        name=name,
        longitude=Decimal(str(coordinate.longitude)),
        latitude=Decimal(str(coordinate.latitude)),
    )



def build_public_transit_option(
    origin: TransitPoint,
    destination: TransitPoint,
) -> TransportOption:
    transit, _ = find_route(
        origin,
        destination,
        "SPONTANEOUS_OUTBOUND",
        1,
    )

    return TransportOption(
        mode=TransportMode.PUBLIC_TRANSIT,
        available=transit.provider == "ODSAY",
        outboundMinutes=transit.total_minutes if transit.provider == "ODSAY" else None,
        returnMinutes=None,
        expectedReturnAt=None,
        unavailableReason=None if transit.provider == "ODSAY" else "NO_ROUTE",
    )


def zone_to_transit_point(
    zone: DestinationZone,
) -> TransitPoint:
    return TransitPoint(
        name=zone.name,
        longitude=Decimal(str(zone.center_longitude)),
        latitude=Decimal(str(zone.center_latitude)),
    )