import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spontaneous.models import (
    Coordinate,
    TransportMode,
    TransportOption,
)

MIN_STAY_MINUTES = 60
log = logging.getLogger("data.spontaneous.routing")


class RoutingApiError(RuntimeError):
    def __init__(
        self,
        provider: str,
        detail: str,
        status_code: int = 502,
    ):
        super().__init__(detail)
        self.provider = provider
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class RouteResult:
    travelMinutes: int
    requestedDepartureAt: datetime
    departureAt: datetime
    arrivalAt: datetime
    mode: TransportMode
    provider: str
    legs: tuple["TransitLeg", ...] = ()


@dataclass(frozen=True)
class TransitLeg:
    mode: str
    route: str | None = None
    routeId: str | None = None
    service: int | None = None
    startName: str | None = None
    startLatitude: float | None = None
    startLongitude: float | None = None
    endName: str | None = None
    endLatitude: float | None = None
    endLongitude: float | None = None
    sectionTime: int | None = None
    stationIds: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitRouteCandidate:
    totalSeconds: int
    legs: tuple[TransitLeg, ...]
    raw: dict


@dataclass(frozen=True)
class SubwayScheduleResult:
    departureAt: datetime
    arrivalAt: datetime
    notificationCode: str | None = None


@dataclass(frozen=True)
class BusRealtimeResult:
    boardingAt: datetime
    sourceMinutes: int


NO_SUBWAY_SCHEDULE = object()


RouteCacheKey = tuple[str, float, float, float, float, str]
RouteResultCache = dict[RouteCacheKey, RouteResult | None]
TravelMinutesCache = dict[tuple[str, float, float, float, float], int | None]


def travel_cache_key(
    mode: TransportMode,
    origin: Coordinate,
    destination: Coordinate,
) -> tuple[str, float, float, float, float]:
    return (
        mode.value,
        round(origin.latitude, 6),
        round(origin.longitude, 6),
        round(destination.latitude, 6),
        round(destination.longitude, 6),
    )


def route_cache_key(
    mode: TransportMode,
    origin: Coordinate,
    destination: Coordinate,
    departure_at: datetime,
) -> RouteCacheKey:
    return (
        mode.value,
        round(origin.latitude, 6),
        round(origin.longitude, 6),
        round(destination.latitude, 6),
        round(destination.longitude, 6),
        departure_at.isoformat(),
    )


def route_result_from_minutes(
    *,
    mode: TransportMode,
    provider: str,
    travel_minutes: int,
    departure_at: datetime,
) -> RouteResult:
    return RouteResult(
        travelMinutes=travel_minutes,
        requestedDepartureAt=departure_at,
        departureAt=departure_at,
        arrivalAt=departure_at + timedelta(minutes=travel_minutes),
        mode=mode,
        provider=provider,
    )


def ceil_seconds_to_minutes(seconds) -> int | None:
    try:
        total_seconds = int(seconds)
    except (TypeError, ValueError):
        return None

    if total_seconds <= 0:
        return None

    return max(
        1,
        (total_seconds + 59) // 60,
    )


def normalize_route_text(value) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def calculate_distance_meters(
    origin_latitude: float | None,
    origin_longitude: float | None,
    destination_latitude: float | None,
    destination_longitude: float | None,
) -> float | None:
    if (
        origin_latitude is None
        or origin_longitude is None
        or destination_latitude is None
        or destination_longitude is None
    ):
        return None

    from math import asin, cos, radians, sin, sqrt

    lat1 = radians(float(origin_latitude))
    lon1 = radians(float(origin_longitude))
    lat2 = radians(float(destination_latitude))
    lon2 = radians(float(destination_longitude))
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        sin(delta_lat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    )
    return 6_371_000 * 2 * asin(sqrt(value))


def parse_optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_nested_values(value, key: str) -> list:
    results = []

    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key == key:
                results.append(item_value)
            results.extend(collect_nested_values(item_value, key))
    elif isinstance(value, list):
        for item in value:
            results.extend(collect_nested_values(item, key))

    return results


def transit_plan_from_payload(payload: dict) -> dict:
    metadata = payload.get("metaData", {})
    plan = metadata.get("plan", {})

    if isinstance(plan, dict):
        return plan

    return {}


def leg_point(leg: dict, field_name: str) -> tuple[str | None, float | None, float | None]:
    point = leg.get(field_name, {})

    if not isinstance(point, dict):
        return None, None, None

    return (
        point.get("name"),
        parse_optional_float(
            point.get("lat") or point.get("y")
        ),
        parse_optional_float(
            point.get("lon") or point.get("x")
        ),
    )


def transit_leg_from_tmap(leg: dict) -> TransitLeg:
    start_name, start_latitude, start_longitude = leg_point(
        leg,
        "start",
    )
    end_name, end_latitude, end_longitude = leg_point(
        leg,
        "end",
    )
    station_ids = tuple(
        str(value)
        for value in collect_nested_values(
            leg.get("passStopList", {}),
            "stationID",
        )
        if value is not None
    )

    return TransitLeg(
        mode=str(leg.get("mode", "")).upper(),
        route=leg.get("route"),
        routeId=str(leg.get("routeId")) if leg.get("routeId") is not None else None,
        service=parse_optional_int(leg.get("service")),
        startName=start_name,
        startLatitude=start_latitude,
        startLongitude=start_longitude,
        endName=end_name,
        endLatitude=end_latitude,
        endLongitude=end_longitude,
        sectionTime=parse_optional_int(leg.get("sectionTime")),
        stationIds=station_ids,
    )


def is_public_transit_leg(leg: TransitLeg) -> bool:
    return leg.mode in {
        "BUS",
        "SUBWAY",
    }


def leg_service_available(raw_leg: dict, parsed_leg: TransitLeg) -> bool:
    if not is_public_transit_leg(parsed_leg):
        return True

    if parsed_leg.service is not None:
        return parsed_leg.service == 1

    lanes = raw_leg.get("Lane") or raw_leg.get("lane") or []

    if isinstance(lanes, dict):
        lanes = [lanes]

    lane_services = [
        parse_optional_int(lane.get("service"))
        for lane in lanes
        if isinstance(lane, dict)
    ]

    if lane_services:
        return any(service == 1 for service in lane_services)

    return False


def transit_itinerary_available(itinerary: dict) -> bool:
    raw_legs = itinerary.get("legs", [])

    if not isinstance(raw_legs, list):
        return False

    parsed_legs = [
        transit_leg_from_tmap(leg)
        for leg in raw_legs
        if isinstance(leg, dict)
    ]
    transit_legs = [
        leg
        for leg in parsed_legs
        if is_public_transit_leg(leg)
    ]

    if not transit_legs:
        return False

    for raw_leg, parsed_leg in zip(raw_legs, parsed_legs, strict=False):
        if not leg_service_available(raw_leg, parsed_leg):
            return False

    return True


def tmap_candidate_from_itinerary(
    itinerary: dict,
) -> TransitRouteCandidate | None:
    total_seconds = itinerary.get("totalTime")

    if total_seconds is None:
        total_seconds = (
            itinerary
            .get("fare", {})
            .get("totalTime")
        )

    try:
        total_seconds = int(total_seconds)
    except (TypeError, ValueError):
        return None

    if total_seconds <= 0:
        return None

    raw_legs = itinerary.get("legs", [])

    if not isinstance(raw_legs, list):
        raw_legs = []

    return TransitRouteCandidate(
        totalSeconds=total_seconds,
        legs=tuple(
            transit_leg_from_tmap(leg)
            for leg in raw_legs
            if isinstance(leg, dict)
        ),
        raw=itinerary,
    )


def select_available_tmap_itinerary(
    payload: dict,
) -> TransitRouteCandidate | None:
    candidates = available_tmap_itineraries(
        payload
    )

    if not candidates:
        return None

    return candidates[0]


def available_tmap_itineraries(
    payload: dict,
) -> list[TransitRouteCandidate]:
    itineraries = transit_plan_from_payload(payload).get(
        "itineraries",
        [],
    )

    if not isinstance(itineraries, list):
        return []

    candidates = []

    for itinerary in itineraries:
        if not isinstance(itinerary, dict):
            continue

        if not transit_itinerary_available(itinerary):
            continue

        candidate = tmap_candidate_from_itinerary(
            itinerary
        )

        if candidate is not None:
            candidates.append(candidate)

    return candidates


def korea_subway_day(departure_at: datetime) -> int | None:
    if departure_at.weekday() == 5:
        return 2

    if departure_at.weekday() == 6:
        return 3

    try:
        import holidays
    except ImportError:
        return None

    if departure_at.date() in holidays.country_holidays("KR"):
        return 3

    return 1


def parse_provider_clock(
    time_text: str,
) -> tuple[int, int, int] | None:
    match = re.search(
        r"(\d{1,2}):?([0-5]\d)(?::?([0-5]\d))?",
        str(time_text or ""),
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)

    if hour > 23:
        return None

    return hour, minute, second


def combine_provider_time(
    base: datetime,
    time_text: str,
    not_before: datetime | None = None,
) -> datetime | None:
    parsed = parse_provider_clock(time_text)

    if parsed is None:
        return None

    hour, minute, second = parsed
    candidate = base.replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )

    if not_before is not None and candidate < not_before:
        candidate += timedelta(days=1)

    return candidate


def extract_first_nested_value(
    value,
    *keys: str,
):
    for key in keys:
        values = collect_nested_values(
            value,
            key,
        )

        if values:
            return values[0]

    return None


def parse_odsay_schedule_payload(
    payload: dict,
    requested_departure_at: datetime,
) -> SubwayScheduleResult | None:
    notification_code = extract_first_nested_value(
        payload,
        "notificationCode",
    )
    departure_time = extract_first_nested_value(
        payload,
        "departureTime",
        "startTime",
    )
    arrival_time = extract_first_nested_value(
        payload,
        "arrivalTime",
        "endTime",
    )

    if not departure_time or not arrival_time:
        return None

    departure_clock = parse_provider_clock(
        str(departure_time)
    )

    if (
        str(notification_code or "") == "3"
        and departure_clock is not None
        and (
            departure_clock[0],
            departure_clock[1],
            departure_clock[2],
        )
        < (
            requested_departure_at.hour,
            requested_departure_at.minute,
            requested_departure_at.second,
        )
    ):
        return None

    departure_at = combine_provider_time(
        requested_departure_at,
        str(departure_time),
        not_before=requested_departure_at,
    )

    if departure_at is None:
        return None

    if str(notification_code or "") == "3" and departure_at < requested_departure_at:
        return None

    if departure_at < requested_departure_at:
        return None

    arrival_at = combine_provider_time(
        departure_at,
        str(arrival_time),
        not_before=departure_at,
    )

    if arrival_at is None:
        return None

    return SubwayScheduleResult(
        departureAt=departure_at,
        arrivalAt=arrival_at,
        notificationCode=str(notification_code) if notification_code is not None else None,
    )


def odsay_cache_key(
    prefix: str,
    *parts,
) -> tuple:
    return (
        prefix,
        *(
            str(part)
            for part in parts
        ),
    )


def search_odsay_subway_station_id(
    name: str | None,
    latitude: float | None,
    longitude: float | None,
    cache: dict | None = None,
) -> str | None:
    if not name:
        return None

    cache_key = odsay_cache_key(
        "odsay_station",
        name,
        latitude,
        longitude,
    )

    if cache is not None and cache_key in cache:
        return cache[cache_key]

    enabled = os.getenv("ODSAY_ENABLED", "false").lower() == "true"
    api_key = os.getenv("ODSAY_API_KEY", "").strip()

    if not enabled or not api_key:
        if cache is not None:
            cache[cache_key] = None
        return None

    base_url = os.getenv(
        "ODSAY_BASE_URL",
        "https://api.odsay.com",
    ).rstrip("/")
    query = urlencode(
        {
            "apiKey": api_key,
            "stationName": name,
            "CID": 7000,
            "stationClass": 2,
            "displayCnt": 10,
            "lang": 0,
        }
    )

    try:
        with urlopen(
            f"{base_url}/v1/api/searchStation?{query}",
            timeout=8,
        ) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )
    except Exception as exc:
        log.info(
            "routing provider=ODSAY_SUBWAY station=unavailable error=%r",
            exc,
        )
        if cache is not None:
            cache[cache_key] = None
        return None

    if "error" in payload:
        parsed_error = parse_odsay_error(
            payload
        )
        log.info(
            "routing provider=ODSAY_SUBWAY station=unavailable status=%s",
            parsed_error[0] if parsed_error else "EXTERNAL_ROUTING_API_ERROR",
        )
        if cache is not None:
            cache[cache_key] = None
        return None

    stations = (
        payload
        .get("result", {})
        .get("station", [])
    )

    if isinstance(stations, dict):
        stations = [stations]

    best_station = None
    best_distance = None

    for station in stations:
        if not isinstance(station, dict):
            continue

        station_latitude = parse_optional_float(
            station.get("y")
        )
        station_longitude = parse_optional_float(
            station.get("x")
        )
        distance = calculate_distance_meters(
            latitude,
            longitude,
            station_latitude,
            station_longitude,
        )
        city_text = str(
            station.get("cityName")
            or station.get("laneCity")
            or ""
        )

        if "부산" not in city_text and distance is not None and distance > 1_500:
            continue

        if distance is None:
            distance = 0

        if best_distance is None or distance < best_distance:
            best_station = station
            best_distance = distance

    station_id = None

    if best_station is not None:
        station_id = best_station.get("stationID")

    if station_id is not None:
        station_id = str(station_id)

    if cache is not None:
        cache[cache_key] = station_id

    return station_id


def search_odsay_subway_schedule(
    start_station_id: str,
    end_station_id: str,
    departure_at: datetime,
    cache: dict | None = None,
):
    day = korea_subway_day(
        departure_at
    )

    if day is None:
        log.info(
            "routing provider=ODSAY_SUBWAY schedule=skipped reason=holiday_calendar_unavailable"
        )
        return None

    cache_key = odsay_cache_key(
        "odsay_schedule",
        start_station_id,
        end_station_id,
        departure_at.isoformat(),
    )

    if cache is not None and cache_key in cache:
        return cache[cache_key]

    enabled = os.getenv("ODSAY_ENABLED", "false").lower() == "true"
    api_key = os.getenv("ODSAY_API_KEY", "").strip()

    if not enabled or not api_key:
        if cache is not None:
            cache[cache_key] = None
        return None

    base_url = os.getenv(
        "ODSAY_BASE_URL",
        "https://api.odsay.com",
    ).rstrip("/")
    query = urlencode(
        {
            "apiKey": api_key,
            "SID": start_station_id,
            "EID": end_station_id,
            "DAY": day,
            "TIME": departure_at.strftime("%H%M"),
            "MODE": 1,
        }
    )

    try:
        with urlopen(
            f"{base_url}/v1/api/subwayPathSchedule?{query}",
            timeout=8,
        ) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )
    except HTTPError as exc:
        try:
            payload = json.loads(
                exc.read().decode("utf-8")
            )
            parsed_error = parse_odsay_error(
                payload
            )
        except Exception:
            parsed_error = None

        if parsed_error and parsed_error[0] == "ODSAY_QUOTA_EXCEEDED":
            log.info("routing provider=ODSAY_SUBWAY schedule=quota")
            if cache is not None:
                cache[cache_key] = None
            return None

        log.info(
            "routing provider=ODSAY_SUBWAY schedule=unavailable status=%s",
            exc.code,
        )
        if cache is not None:
            cache[cache_key] = None
        return None
    except Exception as exc:
        log.info(
            "routing provider=ODSAY_SUBWAY schedule=unavailable error=%r",
            exc,
        )
        if cache is not None:
            cache[cache_key] = None
        return None

    if "error" in payload:
        parsed_error = parse_odsay_error(
            payload
        )
        log.info(
            "routing provider=ODSAY_SUBWAY schedule=unavailable status=%s",
            parsed_error[0] if parsed_error else "EXTERNAL_ROUTING_API_ERROR",
        )
        if cache is not None:
            cache[cache_key] = None
        return None

    schedule = parse_odsay_schedule_payload(
        payload,
        departure_at,
    )
    has_provider_times = bool(
        extract_first_nested_value(
            payload,
            "departureTime",
            "startTime",
        )
        and extract_first_nested_value(
            payload,
            "arrivalTime",
            "endTime",
        )
    )

    log.info(
        "routing provider=ODSAY_SUBWAY schedule=%s notificationCode=%s",
        "matched" if schedule else "no_schedule",
        schedule.notificationCode if schedule else extract_first_nested_value(payload, "notificationCode"),
    )

    if schedule is None and has_provider_times:
        if cache is not None:
            cache[cache_key] = NO_SUBWAY_SCHEDULE
        return NO_SUBWAY_SCHEDULE

    if cache is not None:
        cache[cache_key] = schedule

    return schedule


def busan_bims_base_url() -> str:
    return os.getenv(
        "BUSAN_BIMS_BASE_URL",
        "https://apis.data.go.kr/6260000/BusanBIMS",
    ).rstrip("/")


def parse_xml_items(payload: bytes) -> list[dict]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    items = []

    for item in root.findall(".//item"):
        items.append(
            {
                child.tag: child.text
                for child in item
            }
        )

    return items


def busan_bims_get(
    operation: str,
    params: dict,
    cache: dict | None = None,
) -> list[dict]:
    api_key = os.getenv("BUSAN_BUS_API_KEY", "").strip()

    if not api_key:
        return []

    cache_key = (
        "busan_bims",
        operation,
        tuple(sorted(params.items())),
    )

    if cache is not None and cache_key in cache:
        return cache[cache_key]

    query = urlencode(
        {
            "serviceKey": api_key,
            **params,
        }
    )
    url = f"{busan_bims_base_url()}/{operation}?{query}"

    try:
        with urlopen(
            url,
            timeout=8,
        ) as response:
            items = parse_xml_items(
                response.read()
            )
    except Exception as exc:
        log.info(
            "routing provider=BUSAN_BIMS status=unavailable operation=%s error=%r",
            operation,
            exc,
        )
        items = []

    if cache is not None:
        cache[cache_key] = items

    return items


def select_busan_bims_realtime(
    leg: TransitLeg,
    requested_departure_at: datetime,
    now: datetime | None = None,
    cache: dict | None = None,
) -> BusRealtimeResult | None:
    now = now or datetime.now(
        requested_departure_at.tzinfo
    )

    if leg.mode != "BUS" or not leg.startName:
        return None

    stop_candidates = busan_bims_get(
        "busStopList",
        {
            "bstopnm": leg.startName,
            "numOfRows": 20,
            "pageNo": 1,
        },
        cache=cache,
    )

    matched_stop = None

    for candidate in stop_candidates:
        distance = calculate_distance_meters(
            leg.startLatitude,
            leg.startLongitude,
            parse_optional_float(
                candidate.get("gpsy")
                or candidate.get("gpsym")
                or candidate.get("lat")
            ),
            parse_optional_float(
                candidate.get("gpsx")
                or candidate.get("gpsxm")
                or candidate.get("lon")
            ),
        )

        if distance is not None and distance > 250:
            continue

        matched_stop = candidate
        break

    if matched_stop is None:
        return None

    stop_id = (
        matched_stop.get("bstopid")
        or matched_stop.get("bstopId")
    )

    if not stop_id:
        return None

    arrivals = busan_bims_get(
        "busStopArrByBstopid",
        {
            "bstopid": stop_id,
            "numOfRows": 50,
            "pageNo": 1,
        },
        cache=cache,
    )
    route_text = normalize_route_text(
        leg.route
    )
    available_arrivals: list[BusRealtimeResult] = []

    for arrival in arrivals:
        line_text = normalize_route_text(
            arrival.get("lineno")
            or arrival.get("lineNo")
        )

        if route_text and line_text and route_text not in line_text and line_text not in route_text:
            continue

        for field_name in ("min1", "min2"):
            minutes = parse_optional_int(
                arrival.get(field_name)
            )

            if minutes is None or minutes < 0:
                continue

            boarding_at = now + timedelta(
                minutes=minutes
            )

            if boarding_at >= requested_departure_at:
                available_arrivals.append(
                    BusRealtimeResult(
                        boardingAt=boarding_at,
                        sourceMinutes=minutes,
                    )
                )

    if not available_arrivals:
        log.info("routing provider=BUSAN_BIMS realtime=not_covered")
        return None

    result = sorted(
        available_arrivals,
        key=lambda arrival: arrival.boardingAt,
    )[0]
    log.info(
        "routing provider=BUSAN_BIMS realtime=matched minutes=%s",
        result.sourceMinutes,
    )
    return result


def refine_public_transit_route(
    route: RouteResult,
    candidate: TransitRouteCandidate,
    departure_at: datetime,
    cache: dict | None = None,
) -> RouteResult | None:
    provider_parts = ["TMAP_TRANSIT"]
    subway_legs = [
        leg
        for leg in candidate.legs
        if leg.mode == "SUBWAY"
    ]
    bus_legs = [
        leg
        for leg in candidate.legs
        if leg.mode == "BUS"
    ]

    if subway_legs:
        leg = subway_legs[0]
        start_station_id = (
            leg.stationIds[0]
            if leg.stationIds
            else search_odsay_subway_station_id(
                leg.startName,
                leg.startLatitude,
                leg.startLongitude,
                cache=cache,
            )
        )
        end_station_id = (
            leg.stationIds[-1]
            if len(leg.stationIds) > 1
            else search_odsay_subway_station_id(
                leg.endName,
                leg.endLatitude,
                leg.endLongitude,
                cache=cache,
            )
        )

        if start_station_id and end_station_id:
            schedule = search_odsay_subway_schedule(
                start_station_id,
                end_station_id,
                departure_at,
                cache=cache,
            )

            if schedule is NO_SUBWAY_SCHEDULE:
                log.info("routing provider=ODSAY_SUBWAY schedule=no_route")
                return None

            if schedule is not None:
                provider_parts.append("ODSAY_SUBWAY")

                if len(subway_legs) == 1 and not bus_legs:
                    travel_minutes = int(
                        (
                            schedule.arrivalAt
                            - schedule.departureAt
                        ).total_seconds()
                        + 59
                    ) // 60
                    return RouteResult(
                        travelMinutes=max(1, travel_minutes),
                        requestedDepartureAt=route.requestedDepartureAt,
                        departureAt=schedule.departureAt,
                        arrivalAt=schedule.arrivalAt,
                        mode=route.mode,
                        provider="+".join(provider_parts),
                        legs=route.legs,
                    )

    if bus_legs:
        realtime = select_busan_bims_realtime(
            bus_legs[0],
            departure_at,
            cache=cache,
        )

        if realtime is not None:
            provider_parts.append("BUSAN_BIMS")

            if len(bus_legs) == 1 and not subway_legs:
                return RouteResult(
                    travelMinutes=route.travelMinutes,
                    requestedDepartureAt=route.requestedDepartureAt,
                    departureAt=realtime.boardingAt,
                    arrivalAt=realtime.boardingAt + timedelta(minutes=route.travelMinutes),
                    mode=route.mode,
                    provider="+".join(provider_parts),
                    legs=route.legs,
                )

    if provider_parts != ["TMAP_TRANSIT"]:
        return RouteResult(
            travelMinutes=route.travelMinutes,
            requestedDepartureAt=route.requestedDepartureAt,
            departureAt=route.departureAt,
            arrivalAt=route.arrivalAt,
            mode=route.mode,
            provider="+".join(provider_parts),
            legs=route.legs,
        )

    return route


def search_tmap_transit_route(
    origin: Coordinate,
    destination: Coordinate,
    departure_at: datetime,
    cache: dict | None = None,
) -> RouteResult | None:
    api_key = os.getenv("SKT_API_KEY", "").strip()

    if not api_key:
        raise RoutingApiError(
            provider="TMAP_TRANSIT",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=503,
        )

    base_url = os.getenv(
        "TMAP_TRANSIT_BASE_URL",
        os.getenv(
            "TMAP_BASE_URL",
            "https://apis.openapi.sk.com",
        ),
    ).rstrip("/")
    url = f"{base_url}/transit/routes"
    payload = {
        "startX": str(origin.longitude),
        "startY": str(origin.latitude),
        "endX": str(destination.longitude),
        "endY": str(destination.latitude),
        "count": 10,
        "lang": 0,
        "format": "json",
        "searchDttm": departure_at.strftime("%Y%m%d%H%M"),
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(
                response.read().decode("utf-8"),
                strict=False,
            )
    except HTTPError as exc:
        log.warning(
            "routing provider=TMAP_TRANSIT status=external_error http=%s",
            exc.code,
        )
        raise RoutingApiError(
            provider="TMAP_TRANSIT",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning(
            "routing provider=TMAP_TRANSIT status=external_error error=%r",
            exc,
        )
        raise RoutingApiError(
            provider="TMAP_TRANSIT",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc

    candidates = available_tmap_itineraries(
        result
    )

    if not candidates:
        log.info(
            "routing provider=TMAP_TRANSIT requestedDepartureAt=%s service=unavailable",
            departure_at.isoformat(),
        )
        return None

    for candidate in candidates:
        travel_minutes = ceil_seconds_to_minutes(
            candidate.totalSeconds
        )

        if travel_minutes is None:
            continue

        route = RouteResult(
            travelMinutes=travel_minutes,
            requestedDepartureAt=departure_at,
            departureAt=departure_at,
            arrivalAt=departure_at + timedelta(minutes=travel_minutes),
            mode=TransportMode.PUBLIC_TRANSIT,
            provider="TMAP_TRANSIT",
            legs=candidate.legs,
        )
        route = refine_public_transit_route(
            route,
            candidate,
            departure_at,
            cache=cache,
        )

        if route is None:
            continue

        log.info(
            "routing provider=%s requestedDepartureAt=%s service=available travelMinutes=%s",
            route.provider,
            departure_at.isoformat(),
            route.travelMinutes,
        )
        return route

    log.info(
        "routing provider=TMAP_TRANSIT requestedDepartureAt=%s service=unavailable",
        departure_at.isoformat(),
    )
    return None


def parse_route_minutes(value) -> int | None:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None

    if minutes <= 0:
        return None

    return minutes


def parse_odsay_error(payload: dict) -> tuple[str, int] | None:
    errors = payload.get("error")

    if isinstance(errors, dict):
        errors = [errors]

    if not isinstance(errors, list) or not errors:
        return None

    first_error = errors[0]

    if not isinstance(first_error, dict):
        return ("EXTERNAL_ROUTING_API_ERROR", 502)

    code = str(first_error.get("code", "")).strip()
    message = str(first_error.get("message", ""))
    message_lower = message.lower()

    if code == "429" or "quota" in message_lower:
        return ("ODSAY_QUOTA_EXCEEDED", 503)

    if (
        "auth" in message_lower
        or "authentication" in message_lower
        or "apikey" in message_lower
    ):
        return ("ODSAY_AUTH_FAILED", 503)

    return ("EXTERNAL_ROUTING_API_ERROR", 502)


def raise_odsay_error(
    detail: str,
    status_code: int,
) -> None:
    log.warning(
        "routing provider=ODsay status=%s",
        detail.lower(),
    )
    raise RoutingApiError(
        provider="ODsay",
        detail=detail,
        status_code=status_code,
    )


def search_public_transit_minutes(
    origin: Coordinate,
    destination: Coordinate,
) -> int | None:
    enabled = os.getenv("ODSAY_ENABLED", "false").lower() == "true"
    api_key = os.getenv("ODSAY_API_KEY", "").strip()

    if not enabled:
        raise_odsay_error(
            "EXTERNAL_ROUTING_API_ERROR",
            503,
        )

    if not api_key:
        raise_odsay_error(
            "ODSAY_AUTH_FAILED",
            503,
        )

    base_url = os.getenv(
        "ODSAY_BASE_URL",
        "https://api.odsay.com",
    ).rstrip("/")

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

    request = Request(
        url,
        headers={
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as exc:
        detail = "EXTERNAL_ROUTING_API_ERROR"
        status_code = 502
        try:
            error_body = exc.read().decode("utf-8")
            payload = json.loads(error_body)
            parsed_error = parse_odsay_error(payload)
            if parsed_error:
                detail, status_code = parsed_error
        except Exception:
            pass

        if exc.code == 429:
            detail = "ODSAY_QUOTA_EXCEEDED"
            status_code = 503

        raise_odsay_error(
            detail,
            status_code,
        )

    except URLError as exc:
        log.warning(
            "routing provider=ODsay status=external_error error=%r",
            exc,
        )
        raise RoutingApiError(
            provider="ODsay",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc

    except TimeoutError as exc:
        log.warning(
            "routing provider=ODsay status=external_error error=%r",
            exc,
        )
        raise RoutingApiError(
            provider="ODsay",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc

    except json.JSONDecodeError as exc:
        log.warning(
            "routing provider=ODsay status=external_error error=%r",
            exc,
        )
        raise RoutingApiError(
            provider="ODsay",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc

    except Exception as exc:
        log.warning(
            "routing provider=ODsay status=external_error error=%r",
            exc,
        )
        raise RoutingApiError(
            provider="ODsay",
            detail="EXTERNAL_ROUTING_API_ERROR",
            status_code=502,
        ) from exc

    if not isinstance(payload, dict):
        raise_odsay_error(
            "EXTERNAL_ROUTING_API_ERROR",
            502,
        )

    if "error" in payload:
        parsed_error = parse_odsay_error(payload)

        if parsed_error:
            detail, status_code = parsed_error
        else:
            detail = "EXTERNAL_ROUTING_API_ERROR"
            status_code = 502

        raise_odsay_error(
            detail,
            status_code,
        )

    result = payload.get("result", {})
    paths = result.get("path", [])

    if not paths:
        log.info("routing provider=ODsay status=no_route")
        return None

    valid_minutes: list[int] = []

    for path in paths:
        info = path.get("info", {})
        total_time = parse_route_minutes(
            info.get("totalTime")
        )

        if total_time is not None:
            valid_minutes.append(total_time)

    if not valid_minutes:
        log.info("routing provider=ODsay status=no_route")
        return None


    log.info(
        "routing provider=ODsay status=success minutes=%s",
        min(valid_minutes),
    )
    return min(valid_minutes)



def search_walking_minutes(
    origin: Coordinate,
    destination: Coordinate,
) -> int | None:
    enabled = os.getenv(
        "TMAP_WALKING_ENABLED",
        "false",
    ).lower() == "true"

    api_key = os.getenv("SKT_API_KEY", "").strip()

    if not enabled or not api_key:
        return None

    base_url = os.getenv(
        "TMAP_BASE_URL",
        "https://apis.openapi.sk.com",
    ).rstrip("/")

    url = (
        f"{base_url}"
        "/tmap/routes/pedestrian"
        "?version=1&format=json"
    )

    payload = {
        "startX": str(origin.longitude),
        "startY": str(origin.latitude),
        "endX": str(destination.longitude),
        "endY": str(destination.latitude),
        "startName": "출발지",
        "endName": "목적지",
    }

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(
                response.read().decode("utf-8"),
                strict=False,
            )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        log.warning("routing provider=TMAP status=external_error mode=WALK")
        return None

    features = result.get("features", [])

    if not features:
        log.info("routing provider=TMAP status=no_route mode=WALK")
        return None

    total_seconds = None

    for feature in features:
        properties = feature.get("properties", {})

        if "totalTime" in properties:
            total_seconds = properties["totalTime"]
            break

    if not isinstance(total_seconds, int):
        log.info("routing provider=TMAP status=no_route mode=WALK")
        return None

    minutes = max(
        1,
        (total_seconds + 59) // 60,
    )
    log.info(
        "routing provider=TMAP status=success mode=WALK minutes=%s",
        minutes,
    )
    return minutes


def search_car_minutes(
    origin: Coordinate,
    destination: Coordinate,
) -> int | None:
    api_key = os.getenv("SKT_API_KEY", "").strip()

    if not api_key:
        return None

    base_url = os.getenv(
        "TMAP_BASE_URL",
        "https://apis.openapi.sk.com",
    ).rstrip("/")

    url = (
        f"{base_url}"
        "/tmap/routes"
        "?version=1&format=json"
    )

    payload = {
        "startX": str(origin.longitude),
        "startY": str(origin.latitude),
        "endX": str(destination.longitude),
        "endY": str(destination.latitude),
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
    }

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(
                response.read().decode("utf-8"),
                strict=False,
            )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        log.warning("routing provider=TMAP status=external_error mode=CAR")
        return None

    features = result.get("features", [])

    if not features:
        log.info("routing provider=TMAP status=no_route mode=CAR")
        return None

    total_seconds = None

    for feature in features:
        properties = feature.get("properties", {})

        if "totalTime" in properties:
            total_seconds = properties["totalTime"]
            break

    if not isinstance(total_seconds, int):
        log.info("routing provider=TMAP status=no_route mode=CAR")
        return None


    minutes = max(
        1,
        (total_seconds + 59) // 60,
    )
    log.info(
        "routing provider=TMAP status=success mode=CAR minutes=%s",
        minutes,
    )
    return minutes


def search_travel_minutes(
    mode: TransportMode,
    origin: Coordinate,
    destination: Coordinate,
    cache: TravelMinutesCache | None = None,
) -> int | None:
    """
    이동수단에 따라 실제 이동시간 조회 함수를 선택한다.
    """

    cache_key = travel_cache_key(
        mode,
        origin,
        destination,
    )

    if cache is not None and cache_key in cache:
        log.info(
            "routing provider=cache status=hit mode=%s",
            mode.value,
        )
        return cache[cache_key]

    if mode == TransportMode.PUBLIC_TRANSIT:
        log.warning(
            "routing provider=PUBLIC_TRANSIT status=departure_time_required"
        )
        minutes = None
    elif mode == TransportMode.WALK:
        minutes = search_walking_minutes(
            origin,
            destination,
        )
    elif mode == TransportMode.CAR:
        minutes = search_car_minutes(
            origin,
            destination,
        )
    else:
        minutes = None

    if cache is not None:
        cache[cache_key] = minutes

    return minutes


def search_route(
    mode: TransportMode,
    origin: Coordinate,
    destination: Coordinate,
    departure_at: datetime,
    cache: RouteResultCache | None = None,
) -> RouteResult | None:
    cache_key = route_cache_key(
        mode,
        origin,
        destination,
        departure_at,
    )

    if cache is not None and cache_key in cache:
        log.info(
            "routing provider=cache status=hit mode=%s",
            mode.value,
        )
        return cache[cache_key]

    provider = ""

    if mode == TransportMode.PUBLIC_TRANSIT:
        route = search_tmap_transit_route(
            origin,
            destination,
            departure_at,
            cache=cache,
        )
        if cache is not None:
            cache[cache_key] = route
        return route
    elif mode == TransportMode.WALK:
        provider = "TMAP"
        minutes = search_walking_minutes(
            origin,
            destination,
        )
    elif mode == TransportMode.CAR:
        provider = "TMAP"
        minutes = search_car_minutes(
            origin,
            destination,
        )
    else:
        minutes = None

    if minutes is None:
        route = None
    else:
        route = route_result_from_minutes(
            mode=mode,
            provider=provider,
            travel_minutes=minutes,
            departure_at=departure_at,
        )

    if cache is not None:
        cache[cache_key] = route

    return route

def get_transport_option(
    origin: Coordinate,
    destination: Coordinate,
    mode: TransportMode,
    start_at: datetime,
    return_by: datetime,
    cache: RouteResultCache | None = None,
) -> TransportOption:
    total_available_minutes = int(
        (return_by - start_at).total_seconds() // 60
    )

    try:
        outbound_route = search_route(
            mode,
            origin,
            destination,
            start_at,
            cache=cache,
        )

        if outbound_route is None:
            return TransportOption(
                mode=mode,
                available=False,
                unavailableReason="NO_ROUTE",
            )

        earliest_return_departure_at = (
            outbound_route.arrivalAt
            + timedelta(minutes=MIN_STAY_MINUTES)
        )

        return_route = search_route(
            mode,
            destination,
            origin,
            earliest_return_departure_at,
            cache=cache,
        )
    except RoutingApiError as exc:
        return TransportOption(
            mode=mode,
            available=False,
            unavailableReason=exc.detail,
        )

    if return_route is None:
        return TransportOption(
            mode=mode,
            available=False,
            unavailableReason="NO_ROUTE",
        )

    available_stay_minutes = (
        total_available_minutes
        - outbound_route.travelMinutes
        - return_route.travelMinutes
    )

    if available_stay_minutes < MIN_STAY_MINUTES:
        return TransportOption(
            mode=mode,
            available=False,
            outboundMinutes=outbound_route.travelMinutes,
            returnMinutes=return_route.travelMinutes,
            availableStayMinutes=available_stay_minutes,
            unavailableReason="INSUFFICIENT_STAY_TIME",
        )

    return TransportOption(
        mode=mode,
        available=True,
        outboundMinutes=outbound_route.travelMinutes,
        returnMinutes=return_route.travelMinutes,
        availableStayMinutes=available_stay_minutes,
        unavailableReason=None,
    )


def get_travel_minutes_for_mode(
    origin: Coordinate,
    destination: Coordinate,
    mode: TransportMode,
    cache: TravelMinutesCache | None = None,
) -> int | None:
    return search_travel_minutes(
        mode,
        origin,
        destination,
        cache=cache,
    )
