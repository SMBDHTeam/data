# 부산 권역 목록

from dataclasses import dataclass
from typing import FrozenSet, List


@dataclass(frozen=True)
class DestinationZone:
    destination_id: str
    name: str
    center_latitude: float
    center_longitude: float
    radius_meters: int
    themes: FrozenSet[str]


DESTINATION_ZONES: List[DestinationZone] = [
    DestinationZone(
        destination_id="BUSAN_GWANGALLI",
        name="광안리·민락",
        center_latitude=35.1532,
        center_longitude=129.1187,
        radius_meters=3000,
        themes=frozenset({
            "SEA",
            "SEAFOOD",
            "FOOD",
            "CAFE",
            "WALK",
            "NIGHT_VIEW",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_HAEUNDAE",
        name="해운대·청사포",
        center_latitude=35.1631,
        center_longitude=129.1635,
        radius_meters=4000,
        themes=frozenset({
            "SEA",
            "SEAFOOD",
            "CAFE",
            "WALK",
            "NIGHT_VIEW",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_YEONGDO",
        name="영도·흰여울",
        center_latitude=35.0787,
        center_longitude=129.0445,
        radius_meters=3500,
        themes=frozenset({
            "SEA",
            "CAFE",
            "WALK",
            "CULTURE",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_NAMPO",
        name="남포·자갈치",
        center_latitude=35.0967,
        center_longitude=129.0306,
        radius_meters=2500,
        themes=frozenset({
            "SEAFOOD",
            "FOOD",
            "CULTURE",
            "SHOPPING",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_SEOMYEON",
        name="서면·전포",
        center_latitude=35.1578,
        center_longitude=129.0592,
        radius_meters=2500,
        themes=frozenset({
            "FOOD",
            "CAFE",
            "SHOPPING",
            "NIGHT_VIEW",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_DADAEPO",
        name="다대포",
        center_latitude=35.0467,
        center_longitude=128.9658,
        radius_meters=3000,
        themes=frozenset({
            "SEA",
            "WALK",
            "NATURE",
            "HEALING",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_SONGJEONG",
        name="송정·기장",
        center_latitude=35.1786,
        center_longitude=129.1997,
        radius_meters=4500,
        themes=frozenset({
            "SEA",
            "SEAFOOD",
            "FOOD",
            "CAFE",
            "WALK",
        }),
    ),

    DestinationZone(
        destination_id="BUSAN_DONGNAE",
        name="동래·온천장",
        center_latitude=35.2205,
        center_longitude=129.0811,
        radius_meters=3000,
        themes=frozenset({
            "HEALING",
            "CULTURE",
            "FOOD",
            "WALK",
        }),
    ),
]

def find_destination_zone(
    destination_id: str,
) -> DestinationZone | None:
    for zone in DESTINATION_ZONES:
        if zone.destination_id == destination_id:
            return zone

    return None