import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

from spontaneous.destinations import DestinationZone


TOUR_API_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"


def search_places_by_zone(
    zone: DestinationZone,
) -> list[dict]:
    service_key = os.getenv("TOUR_API_KEY")

    if not service_key:
        raise RuntimeError("TOUR_API_KEY is missing")

    params = {
        "serviceKey": service_key,
        "MobileOS": "ETC",
        "MobileApp": "BusanTour",
        "_type": "json",
        "mapX": zone.center_longitude,
        "mapY": zone.center_latitude,
        "radius": zone.radius_meters,
        "numOfRows": 50,
        "pageNo": 1,
        "arrange": "E",
    }

    url = (
        f"{TOUR_API_BASE_URL}/locationBasedList2"
        f"?{urlencode(params)}"
    )

    with urlopen(url, timeout=10) as response:
        data = json.loads(
            response.read().decode("utf-8")
        )

    body = data["response"]["body"]

    items = body.get("items")

    if not items:
        return []

    return items.get("item", [])