import json
import logging
import os
from datetime import datetime

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


def parse_route_minutes(value) -> int | None:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None

    if minutes <= 0:
        return None

    return minutes

def search_public_transit_minutes(
    origin: Coordinate,
    destination: Coordinate,
) -> int | None:
    enabled = os.getenv("ODSAY_ENABLED", "false").lower() == "true"
    api_key = os.getenv("ODSAY_API_KEY", "").strip()

    if not enabled or not api_key:
        return None

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
        log.warning(
            "ODsay HTTP error. status=%s reason=%s",
            exc.code,
            exc.reason,
        )

        try:
            error_body = exc.read().decode("utf-8")
            log.debug("ODsay error body: %s", error_body)
        except Exception:
            pass

        return None

    except URLError as exc:
        log.warning("ODsay URL error. error=%r", exc)
        return None

    except TimeoutError as exc:
        log.warning("ODsay timeout. error=%r", exc)
        return None

    except json.JSONDecodeError as exc:
        log.warning("ODsay JSON decode error. error=%r", exc)
        return None

    except Exception as exc:
        log.warning("ODsay unknown error. error=%r", exc)
        return None

    if not isinstance(payload, dict):
        return None

    if "error" in payload:
        log.info("ODsay route error response. error=%s", payload.get("error"))
        return None

    result = payload.get("result", {})
    paths = result.get("path", [])

    if not paths:
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
        return None


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
        return None

    features = result.get("features", [])

    if not features:
        return None

    total_seconds = None

    for feature in features:
        properties = feature.get("properties", {})

        if "totalTime" in properties:
            total_seconds = properties["totalTime"]
            break

    if not isinstance(total_seconds, int):
        return None

    return max(
        1,
        (total_seconds + 59) // 60,
    )


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
        return None

    features = result.get("features", [])

    if not features:
        return None

    total_seconds = None

    for feature in features:
        properties = feature.get("properties", {})

        if "totalTime" in properties:
            total_seconds = properties["totalTime"]
            break

    if not isinstance(total_seconds, int):
        return None


    return max(
        1,
        (total_seconds + 59) // 60,
    )


def search_travel_minutes(
    mode: TransportMode,
    origin: Coordinate,
    destination: Coordinate,
) -> int | None:
    """
    이동수단에 따라 실제 이동시간 조회 함수를 선택한다.
    """

    if mode == TransportMode.PUBLIC_TRANSIT:
        return search_public_transit_minutes(
            origin,
            destination,
        )

    if mode == TransportMode.WALK:
        return search_walking_minutes(
            origin,
            destination,
        )

    if mode == TransportMode.CAR:
        return search_car_minutes(
            origin,
            destination,
        )

    # BICYCLE은 아직 미구현
    return None




def get_transport_options(
    origin: Coordinate,
    destination: Coordinate,
    start_at: datetime,
    return_by: datetime,
) -> list[TransportOption]:

    options: list[TransportOption] = []

   
    total_available_minutes = int(
        (return_by - start_at).total_seconds() // 60
    )


    public_transit_minutes = search_public_transit_minutes(
        origin,
        destination,
    )

    public_transit_return_minutes = search_public_transit_minutes(
        destination,
        origin,
    )

    if (
        public_transit_minutes is not None
        and public_transit_return_minutes is not None
    ):
        available_stay_minutes = (
            total_available_minutes
            - public_transit_minutes
            - public_transit_return_minutes
        )

       
        if available_stay_minutes < MIN_STAY_MINUTES:
            options.append(
                TransportOption(
                    mode=TransportMode.PUBLIC_TRANSIT,
                    available=False,
                    outboundMinutes=public_transit_minutes,
                    returnMinutes=public_transit_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason="INSUFFICIENT_STAY_TIME",
                )
            )
        else:
            options.append(
                TransportOption(
                    mode=TransportMode.PUBLIC_TRANSIT,
                    available=True,
                    outboundMinutes=public_transit_minutes,
                    returnMinutes=public_transit_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason=None,
                )
            )

    else:
        options.append(
            TransportOption(
                mode=TransportMode.PUBLIC_TRANSIT,
                available=False,
                unavailableReason="NO_ROUTE",
            )
        )

   

    walking_minutes = search_walking_minutes(
        origin,
        destination,
    )

    walking_return_minutes = search_walking_minutes(
        destination,
        origin,
    )

    if (
        walking_minutes is not None
        and walking_return_minutes is not None
    ):
        available_stay_minutes = (
            total_available_minutes
            - walking_minutes
            - walking_return_minutes
        )

        if available_stay_minutes < MIN_STAY_MINUTES:
            options.append(
                TransportOption(
                    mode=TransportMode.WALK,
                    available=False,
                    outboundMinutes=walking_minutes,
                    returnMinutes=walking_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason="INSUFFICIENT_STAY_TIME",
                )
            )
        else:
            options.append(
                TransportOption(
                    mode=TransportMode.WALK,
                    available=True,
                    outboundMinutes=walking_minutes,
                    returnMinutes=walking_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason=None,
                )
            )

    else:
        options.append(
            TransportOption(
                mode=TransportMode.WALK,
                available=False,
                unavailableReason="NO_ROUTE",
            )
        )

   

    options.append(
        TransportOption(
            mode=TransportMode.BICYCLE,
            available=False,
            unavailableReason="NOT_IMPLEMENTED",
        )
    )

   

    car_minutes = search_car_minutes(
        origin,
        destination,
    )

    car_return_minutes = search_car_minutes(
        destination,
        origin,
    )

    if (
        car_minutes is not None
        and car_return_minutes is not None
    ):
        available_stay_minutes = (
            total_available_minutes
            - car_minutes
            - car_return_minutes
        )

        if available_stay_minutes < MIN_STAY_MINUTES:
            options.append(
                TransportOption(
                    mode=TransportMode.CAR,
                    available=False,
                    outboundMinutes=car_minutes,
                    returnMinutes=car_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason="INSUFFICIENT_STAY_TIME",
                )
            )
        else:
            options.append(
                TransportOption(
                    mode=TransportMode.CAR,
                    available=True,
                    outboundMinutes=car_minutes,
                    returnMinutes=car_return_minutes,
                    availableStayMinutes=available_stay_minutes,
                    unavailableReason=None,
                )
            )

    else:
        options.append(
            TransportOption(
                mode=TransportMode.CAR,
                available=False,
                unavailableReason="NO_ROUTE",
            )
        )

    return options


def get_travel_minutes_for_mode(
    origin: Coordinate,
    destination: Coordinate,
    mode: TransportMode,
) -> int | None:
    if mode == TransportMode.PUBLIC_TRANSIT:
        return search_public_transit_minutes(origin, destination)
    if mode == TransportMode.WALK:
        return search_walking_minutes(origin, destination)
    if mode == TransportMode.CAR:
        return search_car_minutes(origin, destination)
    return None


def get_best_travel_minutes(
    options: list[TransportOption],
) -> int | None:
    available_minutes = [
        option.outboundMinutes
        for option in options
        if (
            option.available
            and option.outboundMinutes is not None
        )
    ]

    if not available_minutes:
        return None

    return min(available_minutes)

def get_best_stay_minutes(
    options: list[TransportOption],
) -> int | None:
    available_stay_minutes = [
        option.availableStayMinutes
        for option in options
        if (
            option.available
            and option.availableStayMinutes is not None
        )
    ]

    if not available_stay_minutes:
        return None

    return max(available_stay_minutes)
