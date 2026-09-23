from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from spontaneous.course import route_result_to_transit
from spontaneous.models import Coordinate, TransportMode
from spontaneous.preview import public_preview_route_lines
from spontaneous.routing import (
    NO_SUBWAY_SCHEDULE,
    search_route,
)


KST = timezone(timedelta(hours=9))
START = datetime(2026, 9, 22, 18, 0, tzinfo=KST)
ORIGIN = Coordinate(name="출발지", latitude=35.10, longitude=129.00)
DESTINATION = Coordinate(name="도착지", latitude=35.16, longitude=129.06)


def subway_path():
    return {
        "info": {"totalTime": 30, "payment": 1600},
        "subPath": [
            {"trafficType": 3, "sectionTime": 5, "distance": 300},
            {
                "trafficType": 1,
                "sectionTime": 20,
                "startName": "A역",
                "endName": "B역",
                "startID": "101",
                "endID": "102",
                "lane": [{"name": "부산 2호선"}],
                "passStopList": {"stations": [
                    {"x": "129.01", "y": "35.11"},
                    {"x": "129.05", "y": "35.15"},
                ]},
            },
            {"trafficType": 3, "sectionTime": 5, "distance": 300},
        ],
    }


class SpontaneousOdsayTransitTest(TestCase):
    def test_short_connection_walks_without_external_provider(self):
        nearby = Coordinate(name="근처", latitude=35.1005, longitude=129.0005)
        with patch("spontaneous.routing.search_public_transit_paths") as odsay, patch(
            "transit.routing.find_tmap_walking_route_if_enabled",
        ) as tmap:
            route = search_route(
                TransportMode.PUBLIC_TRANSIT, ORIGIN, nearby, START, cache={},
            )
        self.assertEqual(route.provider, "INTERNAL_WALK")
        self.assertEqual(route.legs[0].mode, "WALK")
        odsay.assert_not_called()
        tmap.assert_not_called()

    def test_uses_planned_transit_segments_without_tmap(self):
        with patch("spontaneous.routing.search_public_transit_paths", return_value=[subway_path()]), patch(
            "spontaneous.routing.search_odsay_subway_schedule", return_value=None,
        ), patch("transit.routing.find_tmap_walking_route_if_enabled") as tmap:
            route = search_route(
                TransportMode.PUBLIC_TRANSIT, ORIGIN, DESTINATION, START, cache={},
            )

        self.assertIsNotNone(route)
        self.assertEqual(route.provider, "ODSAY")
        self.assertEqual([leg.mode for leg in route.legs], ["WALK", "SUBWAY", "WALK"])
        self.assertEqual(route.fareAmount, 1600)
        self.assertEqual(route.routeLines[1]["coordinates"], [["129.01", "35.11"], ["129.05", "35.15"]])
        tmap.assert_not_called()

        transit = route_result_to_transit(
            route, "출발지", "도착지", ORIGIN, DESTINATION,
            route_order=1, route_type="INBOUND",
        )
        self.assertEqual([segment["mode"] for segment in transit["segments"]], ["WALK", "SUBWAY", "WALK"])
        self.assertEqual(transit["provider"], "ODSAY")
        self.assertEqual(transit["fareAmount"], 1600)
        self.assertEqual(len(transit["route_lines"]), 3)
        self.assertIn("실제 운행 시각", transit["warnings"][-1])
        transit["routeOrder"] = 3
        lines = public_preview_route_lines({"course": [{"inboundTransit": transit}], "finalTransit": None})
        self.assertTrue(all(line["dayNo"] == 1 and line["routeOrder"] == 3 for line in lines))

    def test_missing_subway_service_rejects_static_path(self):
        with patch("spontaneous.routing.search_public_transit_paths", return_value=[subway_path()]), patch(
            "spontaneous.routing.search_odsay_subway_schedule", return_value=NO_SUBWAY_SCHEDULE,
        ):
            route = search_route(
                TransportMode.PUBLIC_TRANSIT, ORIGIN, DESTINATION, START, cache={},
            )
        self.assertIsNone(route)

    def test_subway_timetable_includes_initial_walk_wait_and_final_walk(self):
        from spontaneous.routing import SubwayScheduleResult

        schedule = SubwayScheduleResult(
            departureAt=START + timedelta(minutes=10),
            arrivalAt=START + timedelta(minutes=30),
        )
        with patch("spontaneous.routing.search_public_transit_paths", return_value=[subway_path()]), patch(
            "spontaneous.routing.search_odsay_subway_schedule", return_value=schedule,
        ) as timetable:
            route = search_route(
                TransportMode.PUBLIC_TRANSIT, ORIGIN, DESTINATION, START, cache={},
            )

        timetable.assert_called_once()
        self.assertEqual(timetable.call_args.args[2], START + timedelta(minutes=5))
        self.assertEqual(route.arrivalAt, START + timedelta(minutes=35))
        self.assertEqual(route.travelMinutes, 35)
        self.assertEqual(route.waitMinutes, 5)
        self.assertEqual(route.provider, "ODSAY+ODSAY_SUBWAY")


if __name__ == "__main__":
    from unittest import main

    main()
