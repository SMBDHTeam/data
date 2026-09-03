from unittest import TestCase, main

from spontaneous.destinations import DESTINATION_ZONES
from spontaneous.models import Coordinate
from spontaneous.service import distance_meters


TOURAPI_VERIFIED_ANCHORS = {
    "BUSAN_GWANGALLI": ("광안리해변 테마거리", 35.1551657503, 129.1221363273),
    "BUSAN_HAEUNDAE": ("해운대온천센터", 35.1634406103, 129.1645768542),
    "BUSAN_YEONGDO": ("영도 흰여울해안터널", 35.0779590845, 129.0453201165),
    "BUSAN_NAMPO": ("부산 자갈치시장", 35.0966511661, 129.0306042201),
    "BUSAN_SEOMYEON": ("토요코인호텔 부산서면", 35.1580060259, 129.0640801887),
    "BUSAN_DADAEPO": ("다대포 꿈의 낙조분수", 35.0463945293, 128.9680471374),
    "BUSAN_SONGJEONG": ("송정서핑학교", 35.1810226785, 129.2024952959),
    "BUSAN_DONGNAE": ("303화덕 동래온천장점", 35.2171413152, 129.0774746236),
}

TOURAPI_RADIUS_PLACE_COUNTS = {
    "BUSAN_GWANGALLI": 50,
    "BUSAN_HAEUNDAE": 50,
    "BUSAN_YEONGDO": 50,
    "BUSAN_NAMPO": 50,
    "BUSAN_SEOMYEON": 48,
    "BUSAN_DADAEPO": 3,
    "BUSAN_SONGJEONG": 50,
    "BUSAN_DONGNAE": 40,
}

TOURAPI_CONTENT_ID_OVERLAPS = {
    ("BUSAN_HAEUNDAE", "BUSAN_SONGJEONG"): (1, 99),
    ("BUSAN_YEONGDO", "BUSAN_NAMPO"): (24, 76),
}


class DestinationZoneEvidenceTest(TestCase):
    def test_each_destination_zone_has_provider_anchor_evidence(self):
        self.assertEqual(
            {zone.destination_id for zone in DESTINATION_ZONES},
            set(TOURAPI_VERIFIED_ANCHORS),
        )

        for zone in DESTINATION_ZONES:
            expected_name, _, _ = TOURAPI_VERIFIED_ANCHORS[zone.destination_id]
            self.assertEqual(zone.anchor_name, expected_name)

    def test_destination_zone_config_does_not_embed_theme_scores(self):
        for zone in DESTINATION_ZONES:
            self.assertFalse(hasattr(zone, "themes"))
            self.assertFalse(hasattr(zone, "theme_scores"))

    def test_centers_are_close_to_live_tourapi_anchor_coordinates(self):
        for zone in DESTINATION_ZONES:
            _, anchor_latitude, anchor_longitude = TOURAPI_VERIFIED_ANCHORS[
                zone.destination_id
            ]

            distance = distance_meters(
                Coordinate(
                    latitude=zone.center_latitude,
                    longitude=zone.center_longitude,
                ),
                Coordinate(
                    latitude=anchor_latitude,
                    longitude=anchor_longitude,
                ),
            )

            self.assertLessEqual(
                distance,
                600,
                f"{zone.destination_id} center is {distance:.0f}m from anchor",
            )

    def test_radius_evidence_has_live_tourapi_place_counts(self):
        self.assertEqual(
            set(TOURAPI_RADIUS_PLACE_COUNTS),
            {zone.destination_id for zone in DESTINATION_ZONES},
        )

        for zone in DESTINATION_ZONES:
            self.assertGreaterEqual(
                TOURAPI_RADIUS_PLACE_COUNTS[zone.destination_id],
                1,
            )

    def test_radius_overlap_evidence_is_limited_to_known_adjacent_zones(self):
        self.assertEqual(
            TOURAPI_CONTENT_ID_OVERLAPS,
            {
                ("BUSAN_HAEUNDAE", "BUSAN_SONGJEONG"): (1, 99),
                ("BUSAN_YEONGDO", "BUSAN_NAMPO"): (24, 76),
            },
        )


if __name__ == "__main__":
    main()
