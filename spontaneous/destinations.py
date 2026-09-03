# 부산 권역 목록

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class DestinationZone:
    destination_id: str
    name: str
    anchor_name: str
    center_latitude: float
    center_longitude: float
    radius_meters: int


DESTINATION_ZONES: List[DestinationZone] = [
    DestinationZone(
        destination_id="BUSAN_GWANGALLI",
        name="광안리·민락",
        anchor_name="광안리해변 테마거리",
        center_latitude=35.1532,
        center_longitude=129.1187,
        radius_meters=3000,
    ),

    DestinationZone(
        destination_id="BUSAN_HAEUNDAE",
        name="해운대·청사포",
        anchor_name="해운대온천센터",
        center_latitude=35.1631,
        center_longitude=129.1635,
        radius_meters=4000,
    ),

    DestinationZone(
        destination_id="BUSAN_YEONGDO",
        name="영도·흰여울",
        anchor_name="영도 흰여울해안터널",
        center_latitude=35.0787,
        center_longitude=129.0445,
        radius_meters=3500,
    ),

    DestinationZone(
        destination_id="BUSAN_NAMPO",
        name="남포·자갈치",
        anchor_name="부산 자갈치시장",
        center_latitude=35.0967,
        center_longitude=129.0306,
        radius_meters=2500,
    ),

    DestinationZone(
        destination_id="BUSAN_SEOMYEON",
        name="서면·전포",
        anchor_name="토요코인호텔 부산서면",
        center_latitude=35.1578,
        center_longitude=129.0592,
        radius_meters=2500,
    ),

    DestinationZone(
        destination_id="BUSAN_DADAEPO",
        name="다대포",
        anchor_name="다대포 꿈의 낙조분수",
        center_latitude=35.0467,
        center_longitude=128.9658,
        radius_meters=3000,
    ),

    DestinationZone(
        destination_id="BUSAN_SONGJEONG",
        name="송정·기장",
        anchor_name="송정서핑학교",
        center_latitude=35.1786,
        center_longitude=129.1997,
        radius_meters=4500,
    ),

    DestinationZone(
        destination_id="BUSAN_DONGNAE",
        name="동래·온천장",
        anchor_name="303화덕 동래온천장점",
        center_latitude=35.2205,
        center_longitude=129.0811,
        radius_meters=3000,
    ),
]

def find_destination_zone(
    destination_id: str,
) -> DestinationZone | None:
    for zone in DESTINATION_ZONES:
        if zone.destination_id == destination_id:
            return zone

    return None
