import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import TestCase, mock
from uuid import uuid4

from schedule.persistence import save_transit
from schedule.service import get_schedule_map
from spontaneous.course import (
    calculate_sequential_course_timeline,
    route_result_to_transit,
)
from spontaneous.models import Coordinate, TransportMode
from spontaneous.preview import public_preview_route_lines
from spontaneous.routing import RouteResult, search_route
from spontaneous.schedule_service import schedule_from_snapshot
from tests.test_spontaneous_schedule_save import snapshot


KST = timezone(timedelta(hours=9))
ORIGIN = Coordinate(latitude=35.11, longitude=129.04, name="부산역")
DESTINATION = Coordinate(latitude=35.15, longitude=129.12, name="해변")


def tmap_response(features: list[dict]) -> BytesIO:
    return BytesIO(json.dumps({"features": features}).encode("utf-8"))


class SpontaneousTmapRouteGeometryTest(TestCase):
    def test_car_linestring_coordinates_keep_provider_order_and_lon_lat(self):
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [129.04, 35.11]},
                "properties": {"totalTime": 120},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[129.04, 35.11], [129.08, 35.13]],
                },
                "properties": {},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[129.08, 35.13], [129.12, 35.15]],
                },
                "properties": {},
            },
        ]
        departure = datetime(2026, 9, 11, 23, 58, tzinfo=KST)

        with (
            mock.patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
            mock.patch(
                "spontaneous.routing.urlopen",
                return_value=tmap_response(features),
            ) as request,
        ):
            route = search_route(
                TransportMode.CAR,
                ORIGIN,
                DESTINATION,
                departure,
            )

        self.assertIsNotNone(route)
        self.assertEqual(route.travelMinutes, 2)
        self.assertEqual(route.arrivalAt, datetime(2026, 9, 12, 0, 0, tzinfo=KST))
        self.assertEqual(
            route.routeCoordinates,
            (
                (129.04, 35.11),
                (129.08, 35.13),
                (129.12, 35.15),
            ),
        )
        request_body = json.loads(request.call_args.args[0].data)
        self.assertEqual(request_body["reqCoordType"], "WGS84GEO")
        self.assertEqual(request_body["resCoordType"], "WGS84GEO")

        transit = route_result_to_transit(
            route,
            "부산역",
            "해변",
            ORIGIN,
            DESTINATION,
            route_order=1,
            route_type="INBOUND",
        )
        self.assertFalse(transit["fallbackUsed"])
        self.assertEqual(transit["warnings"], [])
        self.assertFalse(transit["route_lines"][0]["fallbackUsed"])
        self.assertEqual(
            transit["route_lines"][0]["coordinates"],
            [[129.04, 35.11], [129.08, 35.13], [129.12, 35.15]],
        )

    def test_car_route_without_linestring_keeps_geometry_fallback(self):
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [129.04, 35.11]},
                "properties": {"totalTime": 120},
            }
        ]

        with (
            mock.patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
            mock.patch(
                "spontaneous.routing.urlopen",
                return_value=tmap_response(features),
            ),
        ):
            route = search_route(
                TransportMode.CAR,
                ORIGIN,
                DESTINATION,
                datetime(2026, 9, 11, 18, 0, tzinfo=KST),
            )

        self.assertIsNotNone(route)
        self.assertEqual(route.routeCoordinates, ())
        transit = route_result_to_transit(
            route,
            "부산역",
            "해변",
            ORIGIN,
            DESTINATION,
            route_order=1,
            route_type="INBOUND",
        )
        self.assertTrue(transit["fallbackUsed"])
        self.assertTrue(transit["route_lines"][0]["fallbackUsed"])
        self.assertEqual(transit["route_lines"][0]["coordinates"], [])
        self.assertEqual(
            transit["warnings"],
            ["제공사가 상세 경로 선형을 제공하지 않았습니다."],
        )

    def test_inbound_and_final_geometry_flow_to_preview_across_midnight(self):
        start_at = datetime(2026, 9, 11, 23, 50, tzinfo=KST)
        arrival = datetime(2026, 9, 12, 0, 5, tzinfo=KST)
        return_departure = datetime(2026, 9, 12, 1, 5, tzinfo=KST)
        returned = datetime(2026, 9, 12, 1, 25, tzinfo=KST)
        inbound_coordinates = (
            (129.04, 35.11),
            (129.08, 35.13),
            (129.12, 35.15),
        )
        final_coordinates = (
            (129.12, 35.15),
            (129.07, 35.12),
            (129.04, 35.11),
        )
        routes = [
            RouteResult(
                travelMinutes=15,
                requestedDepartureAt=start_at,
                departureAt=start_at,
                arrivalAt=arrival,
                mode=TransportMode.CAR,
                provider="TMAP",
                routeCoordinates=inbound_coordinates,
            ),
            RouteResult(
                travelMinutes=20,
                requestedDepartureAt=return_departure,
                departureAt=return_departure,
                arrivalAt=returned,
                mode=TransportMode.CAR,
                provider="TMAP",
                routeCoordinates=final_coordinates,
            ),
        ]

        with mock.patch("spontaneous.course.search_route", side_effect=routes):
            timeline = calculate_sequential_course_timeline(
                [
                    {
                        "order": 1,
                        "name": "해변",
                        "latitude": DESTINATION.latitude,
                        "longitude": DESTINATION.longitude,
                        "stayMinutes": 60,
                    }
                ],
                ORIGIN,
                TransportMode.CAR,
                start_at,
            )

        preview_lines = public_preview_route_lines(
            {
                "course": timeline["course"],
                "finalTransit": timeline["finalTransit"],
            }
        )
        self.assertEqual(
            [line["routeOrder"] for line in preview_lines],
            [1, 2],
        )
        self.assertEqual(
            preview_lines[0]["coordinates"],
            [list(coordinate) for coordinate in inbound_coordinates],
        )
        self.assertEqual(
            preview_lines[1]["coordinates"],
            [list(coordinate) for coordinate in final_coordinates],
        )
        self.assertEqual(timeline["course"][0]["arrivalAt"], arrival.isoformat())
        self.assertEqual(timeline["course"][0]["departureAt"], return_departure.isoformat())
        self.assertEqual(timeline["estimatedReturnAt"], returned)

    def test_saved_schedule_map_keeps_inbound_and_final_geometry(self):
        value = deepcopy(snapshot())
        inbound_coordinates = [
            [129.04, 35.11],
            [129.08, 35.13],
            [129.12, 35.15],
        ]
        final_coordinates = [
            [129.12, 35.15],
            [129.07, 35.12],
            [129.04, 35.11],
        ]
        inbound = value["course"][0]["inboundTransit"]
        final = value["finalTransit"]
        for transit, coordinates in (
            (inbound, inbound_coordinates),
            (final, final_coordinates),
        ):
            transit["fallbackUsed"] = False
            transit["warnings"] = []
            transit["route_lines"][0]["fallbackUsed"] = False
            transit["route_lines"][0]["coordinates"] = coordinates

        schedule, _ = schedule_from_snapshot(value, uuid4())
        day = schedule.days[0]
        for transit, stop, coordinates in (
            (day.stops[0].inbound_transit, day.stops[0], inbound_coordinates),
            (day.final_transit, None, final_coordinates),
        ):
            cursor = mock.Mock()
            save_transit(cursor, schedule.id, day, stop, transit)
            line_calls = [
                call
                for call in cursor.execute.call_args_list
                if "INSERT INTO transit_route_lines" in call.args[0]
            ]
            self.assertEqual(len(line_calls), 1)
            parameters = line_calls[0].args[1]
            self.assertEqual(json.loads(parameters["coordinates_json"]), coordinates)
            self.assertFalse(parameters["fallback_used"])

        with mock.patch("schedule.service.get_schedule", return_value=schedule):
            map_response = get_schedule_map(schedule.id, 1)

        lines_by_order = {
            line.route_order: [
                [float(longitude), float(latitude)]
                for longitude, latitude in line.coordinates
            ]
            for line in map_response.route_lines
        }
        self.assertEqual(lines_by_order[1], inbound_coordinates)
        self.assertEqual(lines_by_order[2], final_coordinates)
        self.assertFalse(schedule.days[0].stops[0].inbound_transit.fallback_used)
        self.assertFalse(schedule.days[0].final_transit.fallback_used)


if __name__ == "__main__":
    import unittest

    unittest.main()
