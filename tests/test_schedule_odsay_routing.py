from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from transit.routing import TransitPoint, find_route, odsay_path_to_models


class ScheduleOdsayRoutingTest(TestCase):
    def setUp(self):
        self.origin = TransitPoint("부산광역시청", Decimal("129.075"), Decimal("35.180"))
        self.destination = TransitPoint("해운대", Decimal("129.160"), Decimal("35.160"))

    def test_long_route_does_not_fall_back_to_tmap_walking_when_odsay_is_disabled(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "ODSAY_ENABLED": "false",
                    "ODSAY_API_KEY": "",
                    "TMAP_WALKING_ENABLED": "true",
                    "SKT_API_KEY": "test-key",
                },
                clear=False,
            ),
            patch("transit.routing.find_tmap_walking_route_if_enabled") as tmap,
        ):
            transit, route_lines = find_route(self.origin, self.destination, "INBOUND", 1)

        self.assertEqual(transit.provider, "UNRESOLVED")
        self.assertTrue(transit.fallback_used)
        self.assertEqual(transit.walk_minutes, 0)
        self.assertEqual(transit.segments, [])
        self.assertEqual(route_lines, [])
        tmap.assert_not_called()

    def test_long_route_does_not_fall_back_to_tmap_walking_when_odsay_fails(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "ODSAY_ENABLED": "true",
                    "ODSAY_API_KEY": "test-key",
                    "TMAP_WALKING_ENABLED": "true",
                    "SKT_API_KEY": "test-key",
                },
                clear=False,
            ),
            patch("transit.routing.search_odsay_path", side_effect=RuntimeError("ODSAY unavailable")),
            patch("transit.routing.find_tmap_walking_route_if_enabled") as tmap,
        ):
            transit, route_lines = find_route(self.origin, self.destination, "INBOUND", 1)

        self.assertEqual(transit.provider, "UNRESOLVED")
        self.assertEqual(transit.walk_minutes, 0)
        self.assertEqual(route_lines, [])
        tmap.assert_not_called()

    def test_walk_without_coordinates_keeps_odsay_section_time(self):
        path = {
            "info": {"totalTime": 25, "payment": 1600},
            "subPath": [
                {"trafficType": 3, "sectionTime": 5, "distance": 700},
                {
                    "trafficType": 1,
                    "sectionTime": 8,
                    "distance": 4200,
                    "startName": "안락",
                    "endName": "벡스코",
                    "stationCount": 4,
                    "lane": [{"name": "동해선"}],
                },
                {"trafficType": 3, "sectionTime": 12, "distance": 800},
            ],
        }
        origin = TransitPoint("동래온천길", Decimal("129.07"), Decimal("35.21"))
        destination = TransitPoint("센텀시티", Decimal("129.13"), Decimal("35.17"))

        with patch("transit.routing.find_tmap_walking_route_if_enabled") as tmap:
            transit, _ = odsay_path_to_models(path, origin, destination, "FINAL", 1)

        self.assertEqual(transit.total_minutes, 25)
        self.assertEqual(transit.walk_minutes, 17)
        self.assertEqual(
            [segment.duration_minutes for segment in transit.segments],
            [5, 8, 12],
        )
        self.assertEqual(transit.segments[0].start_station_name, "동래온천길")
        self.assertEqual(transit.segments[0].end_station_name, "안락")
        self.assertEqual(transit.segments[2].start_station_name, "벡스코")
        self.assertEqual(transit.segments[2].end_station_name, "센텀시티")
        tmap.assert_not_called()
