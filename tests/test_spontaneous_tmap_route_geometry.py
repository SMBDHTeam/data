import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase, mock
from uuid import uuid4

from schedule.models import ScheduleTransit
from schedule.persistence import route_to_model, save_transit
from schedule.service import get_schedule_map
from spontaneous.course import (
    calculate_sequential_course_timeline,
    route_result_to_transit,
)
from spontaneous.models import Coordinate, SpontaneousCourseRequest, TransportMode
from spontaneous.preview import (
    build_course_snapshot,
    create_preview_token,
    public_preview_course,
    public_preview_route_lines,
    verify_preview_token,
)
from spontaneous.routing import RouteResult, TmapRouteStep, search_route
from spontaneous.schedule_service import schedule_from_snapshot
from tests.test_spontaneous_schedule_save import snapshot


KST = timezone(timedelta(hours=9))
ORIGIN = Coordinate(latitude=35.11, longitude=129.04, name="부산역")
DESTINATION = Coordinate(latitude=35.15, longitude=129.12, name="해변")


def tmap_response(features: list[dict]) -> BytesIO:
    return BytesIO(json.dumps({"features": features}).encode("utf-8"))


def tmap_guidance_features(
    *,
    start: tuple[float, float] = (129.04, 35.11),
    middle: tuple[float, float] = (129.08, 35.13),
    end: tuple[float, float] = (129.12, 35.15),
    total_time: int = 151,
    line_names: tuple[str, str] = ("중앙대로", "해안로"),
    instructions: tuple[str, str] = (
        "중앙대로를 따라 140m 이동",
        "해안로 방면으로 우회전 후 360m 이동",
    ),
) -> list[dict]:
    return [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(start)},
            "properties": {
                "totalTime": total_time,
                "totalDistance": 500,
                "name": "출발지",
                "description": instructions[0],
                "pointType": "S",
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    list(start),
                    list(start),
                    [181, start[1]],
                    [middle[0], 95],
                    ["invalid", middle[1]],
                    list(middle),
                ],
            },
            "properties": {
                "name": line_names[0],
                "description": instructions[0],
                "time": 35,
                "distance": 140,
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(middle)},
            "properties": {
                "name": "",
                "description": instructions[1],
                "pointType": "N",
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [list(middle), list(middle), list(end)],
            },
            "properties": {
                "name": line_names[1],
                "description": instructions[1],
                "time": 91,
                "distance": 360,
            },
        },
    ]


def persisted_route_row(transit) -> dict:
    return {
        "route_type": transit.route_type,
        "route_order": transit.route_order,
        "total_minutes": transit.total_minutes,
        "fare_amount": transit.fare_amount,
        "provider": transit.provider,
        "realtime_status": transit.realtime_status,
        "fallback_used": transit.fallback_used,
        "warnings_json": json.dumps(transit.warnings, ensure_ascii=False),
        "raw_json": json.dumps(
            transit.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        ),
        "depart_at_datetime": transit.depart_at_datetime,
        "arrive_at_datetime": transit.arrive_at_datetime,
        "segments": [
            {
                "segment_order": segment.order,
                "mode": segment.mode,
                "line_name": segment.line_name,
                "start_station_id": segment.start_station_id,
                "start_station_name": segment.start_station_name,
                "end_station_id": segment.end_station_id,
                "end_station_name": segment.end_station_name,
                "instruction": segment.instruction,
                "duration_minutes": segment.duration_minutes,
                "distance_meters": segment.distance_meters,
                "station_count": segment.station_count,
                "wait_minutes": segment.wait_minutes,
                "realtime_status": segment.realtime_status,
            }
            for segment in transit.segments
        ],
        "lines": [
            {
                "line_order": index,
                "mode": line["mode"],
                "line_name": line.get("lineName"),
                "coordinates_json": json.dumps(line["coordinates"]),
                "duration_minutes": line.get("durationMinutes"),
                "distance_meters": line.get("distanceMeters"),
                "instruction": line.get("instruction"),
                "fallback_used": line.get("fallbackUsed", False),
            }
            for index, line in enumerate(transit.route_lines, start=1)
        ],
    }


class SpontaneousTmapRouteGeometryTest(TestCase):
    def test_car_guidance_keeps_provider_steps_without_inflating_total_time(self):
        departure = datetime(2026, 9, 11, 23, 58, tzinfo=KST)
        features = tmap_guidance_features()

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
                departure,
            )

        self.assertIsNotNone(route)
        self.assertEqual(route.travelMinutes, 3)
        self.assertEqual(route.totalDistanceMeters, 500)
        self.assertEqual(route.arrivalAt, datetime(2026, 9, 12, 0, 1, tzinfo=KST))
        self.assertEqual(
            route.routeCoordinates,
            ((129.04, 35.11), (129.08, 35.13), (129.12, 35.15)),
        )
        self.assertEqual([step.mode for step in route.routeSteps], [TransportMode.CAR] * 2)
        self.assertEqual([step.lineName for step in route.routeSteps], ["중앙대로", "해안로"])
        self.assertEqual(
            [step.instruction for step in route.routeSteps],
            ["중앙대로를 따라 140m 이동", "해안로 방면으로 우회전 후 360m 이동"],
        )
        self.assertEqual([step.durationMinutes for step in route.routeSteps], [0, 1])
        self.assertEqual([step.distanceMeters for step in route.routeSteps], [140, 360])
        self.assertEqual(route.routeSteps[0].coordinates, ((129.04, 35.11), (129.08, 35.13)))

        transit = route_result_to_transit(
            route,
            "부산역",
            "해변",
            ORIGIN,
            DESTINATION,
            route_order=1,
            route_type="INBOUND",
        )
        self.assertEqual(transit["totalMinutes"], 3)
        self.assertLessEqual(
            sum(segment["durationMinutes"] for segment in transit["segments"]),
            transit["totalMinutes"],
        )
        self.assertEqual(
            [segment["instruction"] for segment in transit["segments"]],
            ["중앙대로를 따라 140m 이동", "해안로 방면으로 우회전 후 360m 이동"],
        )
        self.assertEqual(
            [segment["distanceMeters"] for segment in transit["segments"]],
            [140, 360],
        )
        self.assertEqual(
            [segment["startStationName"] for segment in transit["segments"]],
            [None, None],
        )
        self.assertEqual(
            [line["durationMinutes"] for line in transit["route_lines"]],
            [0, 1],
        )
        self.assertEqual(
            [line["coordinates"] for line in transit["route_lines"]],
            [
                [[129.04, 35.11], [129.08, 35.13]],
                [[129.08, 35.13], [129.12, 35.15]],
            ],
        )
        self.assertFalse(transit["fallbackUsed"])
        self.assertEqual(transit["warnings"], [])

    def test_walking_guidance_keeps_provider_steps_and_geometry(self):
        features = tmap_guidance_features(
            line_names=("보행자도로", "해변 산책로"),
            instructions=("보행자도로를 따라 직진", "횡단보도를 건너 산책로로 이동"),
        )

        with (
            mock.patch.dict(
                "os.environ",
                {"SKT_API_KEY": "test-key", "TMAP_WALKING_ENABLED": "true"},
            ),
            mock.patch(
                "spontaneous.routing.urlopen",
                return_value=tmap_response(features),
            ) as request,
        ):
            route = search_route(
                TransportMode.WALK,
                ORIGIN,
                DESTINATION,
                datetime(2026, 9, 11, 12, 0, tzinfo=KST),
            )

        self.assertIsNotNone(route)
        self.assertEqual([step.mode for step in route.routeSteps], [TransportMode.WALK] * 2)
        self.assertEqual([step.durationMinutes for step in route.routeSteps], [0, 1])
        self.assertEqual(
            route.routeSteps[1].coordinates,
            ((129.08, 35.13), (129.12, 35.15)),
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
        self.assertEqual(transit["walkMinutes"], transit["totalMinutes"])
        self.assertEqual(
            [segment["mode"] for segment in transit["segments"]],
            ["WALK", "WALK"],
        )
        self.assertEqual(
            [line["instruction"] for line in transit["route_lines"]],
            ["보행자도로를 따라 직진", "횡단보도를 건너 산책로로 이동"],
        )
        self.assertTrue(all(not line["fallbackUsed"] for line in transit["route_lines"]))

    def test_missing_provider_guidance_and_distance_remain_empty(self):
        features = tmap_guidance_features()
        features[3]["properties"] = {"name": "이름만 제공된 도로"}

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
                datetime(2026, 9, 11, 12, 0, tzinfo=KST),
            )

        self.assertIsNotNone(route)
        self.assertIsNone(route.routeSteps[1].instruction)
        self.assertIsNone(route.routeSteps[1].durationMinutes)
        self.assertIsNone(route.routeSteps[1].distanceMeters)
        transit = route_result_to_transit(
            route,
            "부산역",
            "해변",
            ORIGIN,
            DESTINATION,
            route_order=1,
            route_type="INBOUND",
        )
        self.assertIsNone(transit["segments"][1]["instruction"])
        self.assertIsNone(transit["segments"][1]["durationMinutes"])
        self.assertIsNone(transit["segments"][1]["distanceMeters"])
        self.assertIsNone(transit["route_lines"][1]["durationMinutes"])
        self.assertIsNone(transit["route_lines"][1]["distanceMeters"])
        validated = ScheduleTransit.model_validate(transit)
        self.assertIsNone(validated.segments[1].duration_minutes)

    def test_inbound_and_final_guidance_survive_preview_snapshot_across_midnight(self):
        start_at = datetime(2026, 9, 11, 23, 58, tzinfo=KST)
        inbound_features = tmap_guidance_features(total_time=151)
        final_features = tmap_guidance_features(
            start=(129.12, 35.15),
            middle=(129.08, 35.13),
            end=(129.04, 35.11),
            total_time=121,
            line_names=("귀환 해안로", "귀환 중앙대로"),
            instructions=("해안로를 따라 복귀", "중앙대로로 진입"),
        )
        request = SpontaneousCourseRequest(
            destinationId="BUSAN_GWANGALLI",
            startLocation=ORIGIN,
            startAt=start_at,
            returnBy=datetime(2026, 9, 12, 2, 0, tzinfo=KST),
            desiredThemes=["SEA"],
            transportMode=TransportMode.CAR,
        )

        with (
            mock.patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
            mock.patch(
                "spontaneous.routing.urlopen",
                side_effect=[
                    tmap_response(inbound_features),
                    tmap_response(final_features),
                ],
            ),
        ):
            timeline = calculate_sequential_course_timeline(
                [
                    {
                        "order": 1,
                        "role": "ACTIVITY",
                        "name": "해변",
                        "contentId": "123",
                        "contentTypeId": "12",
                        "latitude": DESTINATION.latitude,
                        "longitude": DESTINATION.longitude,
                        "stayMinutes": 60,
                        "themes": ["SEA"],
                        "raw": {"addr1": "부산광역시"},
                    }
                ],
                ORIGIN,
                TransportMode.CAR,
                start_at,
            )

        value = build_course_snapshot(
            request,
            SimpleNamespace(destination_id="BUSAN_GWANGALLI", name="광안리·민락"),
            timeline,
        )
        preview_id, token, _ = create_preview_token(value, owner_id=None)
        verified = verify_preview_token(token, preview_id, owner_id=None)
        public_course = public_preview_course(verified)
        public_lines = public_preview_route_lines(verified)

        self.assertEqual(verified, value)
        self.assertEqual(
            public_course[0]["inboundTransit"]["segments"][0]["instruction"],
            "중앙대로를 따라 140m 이동",
        )
        self.assertEqual(
            verified["finalTransit"]["segments"][0]["instruction"],
            "해안로를 따라 복귀",
        )
        self.assertEqual([line["routeOrder"] for line in public_lines], [1, 1, 2, 2])
        self.assertEqual(public_lines[0]["distanceMeters"], 140)
        self.assertEqual(public_lines[2]["lineName"], "귀환 해안로")
        self.assertEqual(
            verified["estimatedReturnAt"],
            datetime(2026, 9, 12, 1, 4, tzinfo=KST).isoformat(),
        )

    def test_car_linestring_coordinates_keep_provider_order_and_lon_lat(self):
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [129.04, 35.11]},
                "properties": {"totalTime": 120, "totalDistance": 1_000},
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
        self.assertEqual(route.totalDistanceMeters, 1_000)
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
        self.assertEqual(transit["segments"][0]["distanceMeters"], 1_000)
        self.assertEqual(transit["route_lines"][0]["distanceMeters"], 1_000)
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

    def test_guidance_survives_sql_conversion_schedule_detail_and_map(self):
        value = deepcopy(snapshot())
        start = datetime(2026, 9, 11, 23, 30, tzinfo=KST)
        arrival = datetime(2026, 9, 12, 0, 5, tzinfo=KST)
        departure = datetime(2026, 9, 12, 1, 5, tzinfo=KST)
        returned = datetime(2026, 9, 12, 1, 40, tzinfo=KST)

        def transit_payload(
            route_type: str,
            route_order: int,
            depart_at: datetime,
            arrive_at: datetime,
            coordinates: tuple[tuple[float, float], ...],
            names: tuple[str, str],
            instructions: tuple[str, str],
        ) -> dict:
            middle = coordinates[1]
            route = RouteResult(
                travelMinutes=35,
                requestedDepartureAt=depart_at,
                departureAt=depart_at,
                arrivalAt=arrive_at,
                mode=TransportMode.CAR,
                provider="TMAP",
                routeCoordinates=coordinates,
                routeSteps=(
                    TmapRouteStep(
                        mode=TransportMode.CAR,
                        lineName=names[0],
                        instruction=instructions[0],
                        durationMinutes=15,
                        distanceMeters=140,
                        coordinates=(coordinates[0], middle),
                    ),
                    TmapRouteStep(
                        mode=TransportMode.CAR,
                        lineName=names[1],
                        instruction=instructions[1],
                        durationMinutes=20,
                        distanceMeters=360,
                        coordinates=(middle, coordinates[2]),
                    ),
                ),
                totalDistanceMeters=500,
            )
            return route_result_to_transit(
                route,
                "부산역" if route_type == "INBOUND" else "해변",
                "해변" if route_type == "INBOUND" else "부산역",
                ORIGIN if route_type == "INBOUND" else DESTINATION,
                DESTINATION if route_type == "INBOUND" else ORIGIN,
                route_order=route_order,
                route_type=route_type,
            )

        value["course"][0]["inboundTransit"] = transit_payload(
            "INBOUND",
            1,
            start,
            arrival,
            ((129.04, 35.11), (129.08, 35.13), (129.12, 35.15)),
            ("중앙대로", "해안로"),
            ("중앙대로를 따라 이동", "해안로 방면으로 우회전"),
        )
        value["finalTransit"] = transit_payload(
            "FINAL",
            2,
            departure,
            returned,
            ((129.12, 35.15), (129.08, 35.13), (129.04, 35.11)),
            ("귀환 해안로", "귀환 중앙대로"),
            ("해안로를 따라 복귀", "중앙대로로 진입"),
        )

        schedule, _ = schedule_from_snapshot(value, uuid4())
        day = schedule.days[0]
        loaded_transits = []
        for transit, stop in (
            (day.stops[0].inbound_transit, day.stops[0]),
            (day.final_transit, None),
        ):
            cursor = mock.Mock()
            save_transit(cursor, schedule.id, day, stop, transit)
            segment_calls = [
                call
                for call in cursor.execute.call_args_list
                if "INSERT INTO transit_segments" in call.args[0]
            ]
            line_calls = [
                call
                for call in cursor.execute.call_args_list
                if "INSERT INTO transit_route_lines" in call.args[0]
            ]
            self.assertEqual(len(segment_calls), 2)
            self.assertEqual(len(line_calls), 2)
            self.assertEqual(segment_calls[0].args[1]["instruction"], transit.segments[0].instruction)
            self.assertEqual(segment_calls[0].args[1]["distance_meters"], 140)
            self.assertEqual(
                json.loads(line_calls[0].args[1]["coordinates_json"]),
                transit.route_lines[0]["coordinates"],
            )
            self.assertEqual(line_calls[1].args[1]["instruction"], transit.segments[1].instruction)
            loaded_transits.append(route_to_model(persisted_route_row(transit)))

        day.stops[0].inbound_transit = loaded_transits[0]
        day.final_transit = loaded_transits[1]
        detail = schedule.model_dump(mode="json", by_alias=True)
        self.assertEqual(
            detail["days"][0]["stops"][0]["inboundTransit"]["segments"][0]["instruction"],
            "중앙대로를 따라 이동",
        )
        self.assertEqual(
            detail["days"][0]["finalTransit"]["segments"][1]["distanceMeters"],
            360,
        )

        with mock.patch("schedule.service.get_schedule", return_value=schedule):
            map_response = get_schedule_map(schedule.id, 1)
        self.assertEqual(
            [line.instruction for line in map_response.route_lines],
            [
                "중앙대로를 따라 이동",
                "해안로 방면으로 우회전",
                "해안로를 따라 복귀",
                "중앙대로로 진입",
            ],
        )
        self.assertEqual(
            [line.distance_meters for line in map_response.route_lines],
            [140, 360, 140, 360],
        )
        self.assertEqual(
            [float(value) for value in map_response.route_lines[2].coordinates[0]],
            [129.12, 35.15],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
