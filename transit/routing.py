from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from math import asin, ceil, cos, radians, sin, sqrt
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from schedule.models import RouteLine, ScheduleSegment, ScheduleTransit


logger = logging.getLogger("data_ai.transit")


TRAFFIC_TYPE_SUBWAY = 1
TRAFFIC_TYPE_BUS = 2
TRAFFIC_TYPE_WALK = 3
DIRECT_WALK_THRESHOLD_METERS = 1_200
TMAP_WALK_LINE_THRESHOLD_METERS = 500
EARTH_RADIUS_METERS = 6_371_000.0


@dataclass(frozen=True)
class TransitPoint:
    name: str
    longitude: Decimal
    latitude: Decimal


@dataclass(frozen=True)
class TmapWalkingRoute:
    total_seconds: int
    distance_meters: int | None
    coordinates: list[list[Decimal]]


def find_route(
    origin: TransitPoint,
    destination: TransitPoint,
    route_type: str,
    route_order: int,
) -> tuple[ScheduleTransit, list[RouteLine]]:
    if same_point(origin, destination):
        logger.info(
            "transit fallback=same_point route_type=%s route_order=%s origin=%s destination=%s",
            route_type,
            route_order,
            origin.name,
            destination.name,
        )
        transit = walk_fallback_transit(origin, destination, route_type, route_order, total_minutes=0, distance_meters=0)
        return transit, direct_route_lines(origin, destination, transit)

    walk_distance = distance_meters(origin.longitude, origin.latitude, destination.longitude, destination.latitude)
    if walk_distance <= DIRECT_WALK_THRESHOLD_METERS:
        tmap_route = find_tmap_walking_route_if_enabled(origin, destination, int(round(walk_distance)))
        if tmap_route is not None:
            transit = tmap_walk_transit(origin, destination, route_type, route_order, tmap_route)
            return transit, tmap_route_lines(origin, destination, transit, tmap_route)
        logger.info(
            "transit fallback=direct_walk route_type=%s route_order=%s origin=%s destination=%s distance_meters=%s",
            route_type,
            route_order,
            origin.name,
            destination.name,
            int(round(walk_distance)),
        )
        transit = walk_fallback_transit(
            origin,
            destination,
            route_type,
            route_order,
            total_minutes=max(5, int(ceil(walk_distance / 70 / 60 * 60))),
            distance_meters=int(round(walk_distance)),
        )
        return transit, direct_route_lines(origin, destination, transit)

    odsay_enabled = os.getenv("ODSAY_ENABLED", "false").lower() == "true"
    odsay_api_key = os.getenv("ODSAY_API_KEY", "").strip()
    if odsay_enabled and odsay_api_key:
        try:
            path = search_odsay_path(origin, destination, odsay_api_key)
            transit, route_lines = odsay_path_to_models(path, origin, destination, route_type, route_order)
            logger.info(
                "transit provider=ODSAY route_type=%s route_order=%s origin=%s destination=%s total_minutes=%s transfer_count=%s fallback_used=%s segments=%s",
                route_type,
                route_order,
                origin.name,
                destination.name,
                transit.total_minutes,
                transit.transfer_count,
                transit.fallback_used,
                len(transit.segments),
            )
            return transit, route_lines
        except Exception as exc:
            logger.warning(
                "transit odsay_failed route_type=%s route_order=%s origin=%s destination=%s reason=%s",
                route_type,
                route_order,
                origin.name,
                destination.name,
                exc,
            )
    else:
        logger.info(
            "transit odsay_skipped route_type=%s route_order=%s origin=%s destination=%s enabled=%s has_api_key=%s",
            route_type,
            route_order,
            origin.name,
            destination.name,
            odsay_enabled,
            bool(odsay_api_key),
        )

    tmap_route = find_tmap_walking_route_if_enabled(origin, destination, int(round(walk_distance)))
    if tmap_route is not None:
        transit = tmap_walk_transit(origin, destination, route_type, route_order, tmap_route)
        return transit, tmap_route_lines(origin, destination, transit, tmap_route)

    transit = walk_fallback_transit(
        origin,
        destination,
        route_type,
        route_order,
        total_minutes=max(5, int(round(walk_distance / 900))),
        distance_meters=int(round(walk_distance)),
    )
    logger.info(
        "transit fallback=estimated_walk route_type=%s route_order=%s origin=%s destination=%s distance_meters=%s total_minutes=%s",
        route_type,
        route_order,
        origin.name,
        destination.name,
        int(round(walk_distance)),
        transit.total_minutes,
    )
    return transit, direct_route_lines(origin, destination, transit)


def search_odsay_path(origin: TransitPoint, destination: TransitPoint, api_key: str) -> dict[str, Any]:
    base_url = os.getenv("ODSAY_BASE_URL", "https://api.odsay.com").rstrip("/")
    query = urlencode(
        {
            "SX": origin.longitude,
            "SY": origin.latitude,
            "EX": destination.longitude,
            "EY": destination.latitude,
            "apiKey": api_key,
        }
    )
    url = f"{base_url}/v1/api/searchPubTransPathT?{query}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("ODSAY request failed") from exc
    if not isinstance(payload, dict) or "error" in payload:
        raise RuntimeError("ODSAY response invalid")
    paths = as_list(as_dict(payload.get("result")).get("path"))
    if not paths:
        raise RuntimeError("ODSAY path missing")
    return min(paths, key=lambda item: int_value(as_dict(item.get("info")), "totalTime"))


def odsay_path_to_models(
    path: dict[str, Any],
    origin: TransitPoint,
    destination: TransitPoint,
    route_type: str,
    route_order: int,
) -> tuple[ScheduleTransit, list[RouteLine]]:
    info = as_dict(path.get("info"))
    sub_paths = as_list(path.get("subPath"))
    warnings: list[str] = []
    segments: list[ScheduleSegment] = []
    route_lines: list[RouteLine] = []
    previous_line_end = [origin.longitude, origin.latitude]

    for index, raw_sub_path in enumerate(sub_paths, start=1):
        sub_path = as_dict(raw_sub_path)
        mode = map_mode(int_value(sub_path, "trafficType"))
        line_name = line_name_for(sub_path)
        start_name = first_text(sub_path, "startName", "startStationName") or origin.name
        end_name = first_text(sub_path, "endName", "endStationName") or destination.name
        duration_minutes = int_value(sub_path, "sectionTime")
        distance = first_int(sub_path, "distance", "sectionDistance")
        segment = ScheduleSegment(
            order=index,
            mode=mode,
            lineName=line_name,
            startStationId=station_id(sub_path, True),
            startStationName=start_name,
            endStationId=station_id(sub_path, False),
            endStationName=end_name,
            instruction=instruction_for(mode, line_name, start_name, end_name),
            durationMinutes=duration_minutes,
            distanceMeters=distance,
            stationCount=station_count(sub_path),
            waitMinutes=0,
            realtimeStatus="UNAVAILABLE",
        )
        segments.append(segment)

        coordinates = coordinate_pairs_from_sub_path(sub_path)
        fallback_used = False
        if mode == "WALK":
            line_start = coordinates[0] if coordinates else previous_line_end
            line_end = coordinates[-1] if coordinates else coordinates_from_name(end_name, destination, origin, use_destination=True)
            tmap_route = find_tmap_walking_route_if_enabled(
                TransitPoint(start_name, line_start[0], line_start[1]),
                TransitPoint(end_name, line_end[0], line_end[1]),
                distance,
            )
            if tmap_route is not None:
                coordinates = tmap_route.coordinates
                distance = tmap_route.distance_meters
                duration_minutes = max(1, int(ceil(tmap_route.total_seconds / 60)))
                segment.duration_minutes = duration_minutes
                segment.distance_meters = distance
                line_start = coordinates[0]
                line_end = coordinates[-1]
            if not coordinates:
                fallback_used = True
                line_start = previous_line_end
                line_end = coordinates_from_name(end_name, destination, origin, use_destination=True)
                coordinates = [line_start, line_end]
            elif tmap_route is None and should_use_tmap_walking(distance):
                fallback_used = True
            warnings.append("도보 경로는 실시간 보행 장애 정보를 반영하지 않습니다.")
        elif not coordinates:
            fallback_used = True
            line_start = previous_line_end
            line_end = coordinates_from_name(end_name, destination, origin, use_destination=False)
            coordinates = [line_start, line_end]

        route_lines.append(
            RouteLine(
                dayNo=0,
                routeOrder=route_order,
                lineOrder=index,
                mode=mode,
                lineName=line_name,
                startName=start_name,
                endName=end_name,
                durationMinutes=duration_minutes,
                distanceMeters=distance,
                instruction=segment.instruction,
                fallbackUsed=fallback_used,
                coordinates=coordinates,
            )
        )
        if coordinates:
            previous_line_end = coordinates[-1]

    transit = ScheduleTransit(
        routeType=route_type,
        routeOrder=route_order,
        originName=origin.name,
        destinationName=destination.name,
        summary=f"{origin.name} -> {destination.name}",
        totalMinutes=int_value(info, "totalTime"),
        walkMinutes=sum(segment.duration_minutes for segment in segments if segment.mode == "WALK"),
        waitMinutes=0,
        transferCount=max(0, sum(1 for segment in segments if segment.mode in {"BUS", "SUBWAY"}) - 1),
        fareAmount=first_int(info, "payment"),
        provider="ODSAY",
        realtimeStatus="UNAVAILABLE",
        fallbackUsed=any(line.fallback_used for line in route_lines),
        segments=segments,
        warnings=list(dict.fromkeys(warnings)),
        route_lines=[line.model_dump(mode="json", by_alias=True) for line in route_lines],
    )
    return transit, route_lines


def walk_fallback_transit(
    origin: TransitPoint,
    destination: TransitPoint,
    route_type: str,
    route_order: int,
    total_minutes: int,
    distance_meters: int,
) -> ScheduleTransit:
    return ScheduleTransit(
        routeType=route_type,
        routeOrder=route_order,
        originName=origin.name,
        destinationName=destination.name,
        summary=f"{origin.name} -> {destination.name}",
        totalMinutes=total_minutes,
        walkMinutes=total_minutes,
        waitMinutes=0,
        transferCount=0,
        fareAmount=None,
        provider=None,
        realtimeStatus="UNAVAILABLE",
        fallbackUsed=False,
        segments=[
            ScheduleSegment(
                order=1,
                mode="WALK",
                lineName=None,
                startStationId=None,
                startStationName=origin.name,
                endStationId=None,
                endStationName=destination.name,
                instruction=f"{origin.name}에서 {destination.name}까지 도보 이동",
                durationMinutes=total_minutes,
                distanceMeters=distance_meters,
                stationCount=None,
                waitMinutes=0,
                realtimeStatus="UNAVAILABLE",
            )
        ],
        warnings=["도보 경로는 실시간 보행 장애 정보를 반영하지 않습니다."],
    )


def tmap_walk_transit(
    origin: TransitPoint,
    destination: TransitPoint,
    route_type: str,
    route_order: int,
    route: TmapWalkingRoute,
) -> ScheduleTransit:
    total_minutes = max(1, int(ceil(route.total_seconds / 60)))
    return ScheduleTransit(
        routeType=route_type,
        routeOrder=route_order,
        originName=origin.name,
        destinationName=destination.name,
        summary=f"{origin.name} -> {destination.name}",
        totalMinutes=total_minutes,
        walkMinutes=total_minutes,
        waitMinutes=0,
        transferCount=0,
        fareAmount=None,
        provider=None,
        realtimeStatus="UNAVAILABLE",
        fallbackUsed=False,
        segments=[
            ScheduleSegment(
                order=1,
                mode="WALK",
                lineName=None,
                startStationId=None,
                startStationName=origin.name,
                endStationId=None,
                endStationName=destination.name,
                instruction=f"{origin.name}에서 {destination.name}까지 도보 이동",
                durationMinutes=total_minutes,
                distanceMeters=route.distance_meters,
                stationCount=None,
                waitMinutes=0,
                realtimeStatus="UNAVAILABLE",
            )
        ],
        warnings=["도보 경로는 실시간 보행 장애 정보를 반영하지 않습니다."],
    )


def direct_route_lines(
    origin: TransitPoint,
    destination: TransitPoint,
    transit: ScheduleTransit,
) -> list[RouteLine]:
    return [
        RouteLine(
            dayNo=0,
            routeOrder=transit.route_order,
            lineOrder=1,
            mode="WALK",
            lineName=None,
            startName=origin.name,
            endName=destination.name,
            durationMinutes=transit.total_minutes,
            distanceMeters=transit.segments[0].distance_meters if transit.segments else None,
            instruction="도보 이동",
            fallbackUsed=True,
            coordinates=[[origin.longitude, origin.latitude], [destination.longitude, destination.latitude]],
        )
    ]


def tmap_route_lines(
    origin: TransitPoint,
    destination: TransitPoint,
    transit: ScheduleTransit,
    route: TmapWalkingRoute,
) -> list[RouteLine]:
    return [
        RouteLine(
            dayNo=0,
            routeOrder=transit.route_order,
            lineOrder=1,
            mode="WALK",
            lineName="도보 이동",
            startName=origin.name,
            endName=destination.name,
            durationMinutes=transit.total_minutes,
            distanceMeters=route.distance_meters,
            instruction="도보 이동",
            fallbackUsed=False,
            coordinates=route.coordinates,
        )
    ]


def find_tmap_walking_route_if_enabled(
    origin: TransitPoint,
    destination: TransitPoint,
    distance_meters: int | None,
) -> TmapWalkingRoute | None:
    if not should_use_tmap_walking(distance_meters):
        return None

    enabled_override = os.getenv("TMAP_WALKING_ENABLED")
    app_key = os.getenv("SKT_API_KEY", "").strip()
    enabled = bool(app_key)
    if enabled_override is not None and enabled_override.strip():
        enabled = enabled_override.lower() == "true"
    if not enabled or not app_key:
        logger.info(
            "transit tmap_skipped origin=%s destination=%s enabled=%s has_app_key=%s distance_meters=%s",
            origin.name,
            destination.name,
            enabled,
            bool(app_key),
            distance_meters,
        )
        return None

    try:
        route = search_tmap_walking_route(origin, destination, app_key)
        logger.info(
            "transit provider=TMAP origin=%s destination=%s total_seconds=%s distance_meters=%s coordinate_count=%s",
            origin.name,
            destination.name,
            route.total_seconds,
            route.distance_meters,
            len(route.coordinates),
        )
        return route
    except Exception as exc:
        logger.warning(
            "transit tmap_failed origin=%s destination=%s distance_meters=%s reason=%s",
            origin.name,
            destination.name,
            distance_meters,
            exc,
        )
        return None


def should_use_tmap_walking(distance_meters: int | None) -> bool:
    return distance_meters is not None and distance_meters > TMAP_WALK_LINE_THRESHOLD_METERS


def search_tmap_walking_route(
    origin: TransitPoint,
    destination: TransitPoint,
    app_key: str,
) -> TmapWalkingRoute:
    base_url = os.getenv("TMAP_BASE_URL", "https://apis.openapi.sk.com").rstrip("/")
    url = f"{base_url}/tmap/routes/pedestrian?version=1&format=json"
    body = json.dumps(
        {
            "startX": str(origin.longitude),
            "startY": str(origin.latitude),
            "endX": str(destination.longitude),
            "endY": str(destination.latitude),
            "reqCoordType": "WGS84GEO",
            "resCoordType": "WGS84GEO",
            "startName": "start",
            "endName": "end",
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": app_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("TMAP request failed") from exc

    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise RuntimeError("TMAP response invalid")

    coordinates = tmap_line_string_coordinates(features)
    if len(coordinates) < 2:
        raise RuntimeError("TMAP coordinates missing")

    total_seconds = tmap_total_seconds(features)
    if total_seconds <= 0:
        raise RuntimeError("TMAP total time missing")

    return TmapWalkingRoute(
        total_seconds=total_seconds,
        distance_meters=tmap_distance_meters(features),
        coordinates=coordinates,
    )


def tmap_line_string_coordinates(features: list[Any]) -> list[list[Decimal]]:
    coordinates: list[list[Decimal]] = []
    for feature in features:
        feature_dict = as_dict(feature)
        geometry = as_dict(feature_dict.get("geometry"))
        if geometry.get("type") != "LineString":
            continue
        raw_coordinates = geometry.get("coordinates")
        if not isinstance(raw_coordinates, list):
            continue
        for item in raw_coordinates:
            coordinate = tmap_coordinate(item)
            if coordinate is None:
                continue
            if coordinates and coordinates[-1][0] == coordinate[0] and coordinates[-1][1] == coordinate[1]:
                continue
            coordinates.append(coordinate)
    return coordinates


def tmap_coordinate(value: Any) -> list[Decimal] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    return [Decimal(str(value[0])), Decimal(str(value[1]))]


def tmap_total_seconds(features: list[Any]) -> int:
    for feature in features:
        properties = as_dict(as_dict(feature).get("properties"))
        total_time = properties.get("totalTime")
        if total_time not in (None, ""):
            return int(float(total_time))
    return 0


def tmap_distance_meters(features: list[Any]) -> int | None:
    for feature in features:
        properties = as_dict(as_dict(feature).get("properties"))
        total_distance = properties.get("totalDistance")
        if total_distance not in (None, ""):
            return int(float(total_distance))
    return None


def distance_meters(start_lon: Decimal, start_lat: Decimal, end_lon: Decimal, end_lat: Decimal) -> float:
    lon1 = float(start_lon)
    lat1 = float(start_lat)
    lon2 = float(end_lon)
    lat2 = float(end_lat)
    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    start_lat_rad = radians(lat1)
    end_lat_rad = radians(lat2)
    haversine = sin(delta_lat / 2) ** 2 + cos(start_lat_rad) * cos(end_lat_rad) * sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * asin(sqrt(haversine))


def same_point(origin: TransitPoint, destination: TransitPoint) -> bool:
    return origin.longitude == destination.longitude and origin.latitude == destination.latitude


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def int_value(source: dict[str, Any], key: str) -> int:
    raw = source.get(key)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        return int(float(raw))
    return 0


def first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return int_value(source, key)
    return None


def first_text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def map_mode(traffic_type: int) -> str:
    if traffic_type == TRAFFIC_TYPE_SUBWAY:
        return "SUBWAY"
    if traffic_type == TRAFFIC_TYPE_BUS:
        return "BUS"
    if traffic_type == TRAFFIC_TYPE_WALK:
        return "WALK"
    return "UNKNOWN"


def line_name_for(sub_path: dict[str, Any]) -> str | None:
    lanes = as_list(sub_path.get("lane"))
    if not lanes:
        return None
    lane = lanes[0]
    return first_text(lane, "name", "busNo", "busID")


def instruction_for(mode: str, line_name: str | None, start_name: str, end_name: str) -> str:
    if mode == "WALK":
        return f"{start_name}에서 {end_name}까지 도보 이동"
    if line_name:
        return f"{start_name}에서 {line_name} 승차 후 {end_name}에서 하차"
    return f"{start_name}에서 {end_name}까지 {mode} 이동"


def station_id(sub_path: dict[str, Any], start: bool) -> str | None:
    if start:
        return first_text(sub_path, "startID", "startId", "startStationID", "startStationId", "startLocalStationID", "startLocalStationId", "startArsID", "startArsId")
    return first_text(sub_path, "endID", "endId", "endStationID", "endStationId", "endLocalStationID", "endLocalStationId", "endArsID", "endArsId")


def station_count(sub_path: dict[str, Any]) -> int | None:
    count = first_int(sub_path, "stationCount")
    if count is not None:
        return count
    stations = coordinate_station_list(sub_path)
    return max(0, len(stations) - 1) if stations else None


def coordinate_pairs_from_sub_path(sub_path: dict[str, Any]) -> list[list[Decimal]]:
    stations = coordinate_station_list(sub_path)
    coordinates: list[list[Decimal]] = []
    for station in stations:
        x = first_text(station, "x", "lon", "longitude")
        y = first_text(station, "y", "lat", "latitude")
        if x is None or y is None:
            continue
        coordinates.append([Decimal(x), Decimal(y)])
    return coordinates


def coordinate_station_list(sub_path: dict[str, Any]) -> list[dict[str, Any]]:
    pass_stop = as_dict(sub_path.get("passStopList"))
    stations = as_list(pass_stop.get("stations"))
    if stations:
        return stations
    return as_list(pass_stop.get("stationList"))


def coordinates_from_name(
    end_name: str,
    destination: TransitPoint,
    origin: TransitPoint,
    use_destination: bool,
) -> list[Decimal]:
    point = destination if use_destination or end_name == destination.name else origin
    return [point.longitude, point.latitude]
