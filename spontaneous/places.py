import json
import os
import re
from urllib.parse import urlencode
from urllib.request import urlopen

from spontaneous.destinations import DestinationZone
from datetime import datetime, time

TOUR_API_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"



COURSE_CONTENT_TYPE_IDS = {
    "12",  # 관광지
    "14",  # 문화시설
    "15",  # 축제/공연
    "28",  # 레포츠
    "38",  # 쇼핑
    "39",  # 음식점
}


def parse_optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_valid_coordinates(
    place: dict,
) -> bool:
    latitude = parse_optional_float(
        place.get("mapy")
    )
    longitude = parse_optional_float(
        place.get("mapx")
    )

    if latitude is None or longitude is None:
        return False

    return (
        -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def filter_course_places(
    places: list[dict],
) -> list[dict]:
    return [
        place
        for place in places
        if (
            str(place.get("contenttypeid")) in COURSE_CONTENT_TYPE_IDS
            and has_valid_coordinates(place)
        )
    ]



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


def infer_place_themes(
    place: dict,
) -> set[str]:
    themes: set[str] = set()

    content_type_id = str(
        place.get("contenttypeid", "")
    )

    title = str(
        place.get("title", "")
    ).upper()

    if content_type_id == "12":
        themes.add("WALK")

    elif content_type_id == "14":
        themes.add("CULTURE")

    elif content_type_id == "15":
        themes.add("CULTURE")

    elif content_type_id == "28":
        themes.add("ACTIVITY")

    elif content_type_id == "38":
        themes.add("SHOPPING")

    elif content_type_id == "39":
        cat3 = str(
            place.get("cat3", "")
        )

        if cat3 == "A05020900":
            themes.add("CAFE")
        else:
            themes.add("FOOD")

    seafood_keywords = [
        "횟집",
        "회센터",
        "회타운",
        "수산",
        "해산물",
        "자갈치",
    ]

    sea_keywords = [
        "해변",
        "해수욕장",
        "바다",
        "해안",
        "비치",
    ]

    if (
        content_type_id in {"38", "39"}
        and any(
            keyword in title
            for keyword in seafood_keywords
        )
    ):
        themes.add("SEAFOOD")

    if (
        content_type_id in {"12", "28"}
        and any(
            keyword in title
            for keyword in sea_keywords
        )
    ):
        themes.add("SEA")

    return themes


def filter_places_by_themes(
    places: list[dict],
    desired_themes: list[str],
) -> list[dict]:
    if not desired_themes:
        return places

    desired_set = {
        theme.upper()
        for theme in desired_themes
    }

    matched_places = []

    for place in places:
        place_themes = infer_place_themes(place)

        if desired_set.intersection(place_themes):
            matched_places.append(place)

    return matched_places


def search_food_detail(
    content_id: str,
) -> dict:
    service_key = os.getenv("TOUR_API_KEY")

    if not service_key:
        raise RuntimeError("TOUR_API_KEY is missing")

    params = {
        "serviceKey": service_key,
        "MobileOS": "ETC",
        "MobileApp": "BusanTour",
        "_type": "json",
        "contentId": content_id,
        "contentTypeId": "39",
    }

    url = (
        f"{TOUR_API_BASE_URL}/detailIntro2"
        f"?{urlencode(params)}"
    )

    try:
        with urlopen(url, timeout=10) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

    except Exception:
        return {}

    try:
        items = (
            data["response"]
            ["body"]
            ["items"]
            ["item"]
        )

        if not items:
            return {}

        return items[0]

    except (KeyError, IndexError, TypeError):
        return {}


def enrich_food_themes(
    place: dict,
    themes: set[str],
) -> set[str]:
    """
    음식점 상세 정보를 이용해 테마 보강
    """

    content_type_id = str(
        place.get("contenttypeid", "")
    )

    # 음식점만 상세 조회
    if content_type_id != "39":
        return themes

    content_id = place.get(
        "contentid"
    )

    if not content_id:
        return themes

    try:
        detail = search_food_detail(
            content_id
        )

    except Exception:
        return themes


    seafood_keywords = [
        "회",
        "횟집",
        "해산물",
        "수산",
        "우럭",
        "광어",
        "참치",
        "초밥",
        "물회",
        "전복",
        "장어",
    ]


    menu_text = " ".join(
        [
            str(detail.get("firstmenu", "")),
            str(detail.get("treatmenu", "")),
        ]
    )


    if any(
        keyword in menu_text
        for keyword in seafood_keywords
    ):
        themes.add("SEAFOOD")


    return themes


def convert_to_course_place(
    place: dict,
) -> dict:

    themes = infer_place_themes(
        place
    )

    themes = enrich_food_themes(
        place,
        themes,
    )

    latitude = parse_optional_float(
        place.get("mapy")
    )
    longitude = parse_optional_float(
        place.get("mapx")
    )

    return {
        "name": place.get("title"),
        "contentId": place.get("contentid"),
        "contentTypeId": str(
            place.get("contenttypeid", "")
        ),

        "latitude": latitude,
        "longitude": longitude,

        "themes": themes,

        "raw": place,
    }

def filter_course_candidates(
    places: list[dict],
) -> list[dict]:
    return [
        place
        for place in places
        if (
            str(place.get("contenttypeid"))
            in COURSE_CONTENT_TYPE_IDS
            and has_valid_coordinates(place)
        )
    ]


def parse_open_time(
    open_time_text: str,
) -> tuple[time, time] | None:
    """
    TourAPI 영업시간 문자열 파싱

    예:
    11:30~22:00

    결과:
    (11:30,22:00)
    """

    if not open_time_text:
        return None


    text = (
        open_time_text
        .replace("<br>", " ")
        .replace("<br/>", " ")
        .replace("<br />", " ")
        .strip()
    )

    matches = re.findall(
        r"([0-2]?\d):([0-5]\d)",
        text,
    )

    if len(matches) < 2:
        return None

    def to_time(match: tuple[str, str]) -> time | None:
        hour = int(match[0])
        minute = int(match[1])

        if hour == 24 and minute == 0:
            return time(23, 59)

        if hour > 23:
            return None

        return time(hour, minute)

    start = to_time(matches[0])
    end = to_time(matches[1])

    if start is None or end is None:
        return None

    return (
        start,
        end
    )

def is_open_now(
    open_time_text: str,
    current_time: datetime,
) -> bool:
    """
    현재 시간 영업 여부
    """

    parsed = parse_open_time(
        open_time_text
    )

    if not parsed:
        # 정보 없으면 일단 허용
        return True


    start, end = parsed


    now = current_time.time()

    if start <= end:
        return (
            start <= now <= end
        )

    return (
        now >= start
        or now <= end
    )


def is_food_open(
    place: dict,
    current_time: datetime,
) -> bool:

    if str(
        place.get("contenttypeid")
    ) != "39":
        return True


    detail = search_food_detail(
        place.get("contentid")
    )


    open_time = detail.get(
        "opentimefood",
        ""
    )


    return is_open_now(
        open_time,
        current_time,
    )


def filter_open_places(
    places: list[dict],
    current_datetime: datetime,
) -> list[dict]:
    """
    현재 영업 가능한 장소만 반환
    """

    result = []

    for place in places:

        if is_food_open(
            place,
            current_datetime,
        ):
            result.append(place)

    return result


def is_course_place_open_for_visit(
    place: dict,
    arrival_at: datetime,
    departure_at: datetime,
) -> bool:
    if str(
        place.get("contentTypeId")
    ) != "39":
        return True

    content_id = place.get(
        "contentId"
    )

    if not content_id:
        return True

    detail = search_food_detail(
        content_id
    )

    open_time = detail.get(
        "opentimefood",
        ""
    )

    return (
        is_open_now(open_time, arrival_at)
        and is_open_now(open_time, departure_at)
    )
