import json
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
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        return None

    if not isinstance(payload, dict) or "error" in payload:
        return None

    result = payload.get("result", {})
    paths = result.get("path", [])

    if not paths:
        return None

    valid_minutes: list[int] = []

    for path in paths:
        info = path.get("info", {})
        total_time = info.get("totalTime")

        if isinstance(total_time, int):
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