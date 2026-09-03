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
        minutes = search_public_transit_minutes(
            origin,
            destination,
        )
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

def get_transport_option(
    origin: Coordinate,
    destination: Coordinate,
    mode: TransportMode,
    start_at: datetime,
    return_by: datetime,
    cache: TravelMinutesCache | None = None,
) -> TransportOption:
    total_available_minutes = int(
        (return_by - start_at).total_seconds() // 60
    )

    try:
        outbound_minutes = search_travel_minutes(
            mode,
            origin,
            destination,
            cache=cache,
        )

        return_minutes = search_travel_minutes(
            mode,
            destination,
            origin,
            cache=cache,
        )
    except RoutingApiError as exc:
        return TransportOption(
            mode=mode,
            available=False,
            unavailableReason=exc.detail,
        )

    if outbound_minutes is None or return_minutes is None:
        return TransportOption(
            mode=mode,
            available=False,
            unavailableReason="NO_ROUTE",
        )

    available_stay_minutes = (
        total_available_minutes
        - outbound_minutes
        - return_minutes
    )

    if available_stay_minutes < MIN_STAY_MINUTES:
        return TransportOption(
            mode=mode,
            available=False,
            outboundMinutes=outbound_minutes,
            returnMinutes=return_minutes,
            availableStayMinutes=available_stay_minutes,
            unavailableReason="INSUFFICIENT_STAY_TIME",
        )

    return TransportOption(
        mode=mode,
        available=True,
        outboundMinutes=outbound_minutes,
        returnMinutes=return_minutes,
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
