from datetime import datetime, timedelta, timezone
from unittest import TestCase, main
from unittest.mock import patch

from spontaneous.course import (
    calculate_sequential_course_timeline,
    normalize_course_orders,
    remove_last_optional_stop,
)
from spontaneous.models import Coordinate, TransportMode
from spontaneous.routing import (
    BusRealtimeResult,
    RouteResult,
    TransitLeg,
    combine_provider_time,
    parse_odsay_schedule_payload,
    refine_public_transit_route,
    search_route,
    select_available_tmap_itinerary,
    select_busan_bims_realtime,
)


KST = timezone(timedelta(hours=9))
HOME = Coordinate(latitude=35.100000, longitude=129.000000)
A_COORD = Coordinate(latitude=35.110000, longitude=129.010000)
B_COORD = Coordinate(latitude=35.120000, longitude=129.020000)
C_COORD = Coordinate(latitude=35.130000, longitude=129.030000)


def stop(
    name: str,
    coordinate: Coordinate,
    order: int,
    stay_minutes: int,
    required: bool = True,
    role: str = "ACTIVITY",
    content_type_id: str = "12",
) -> dict:
    return {
        "order": order,
        "role": role,
        "name": name,
        "contentId": name,
        "contentTypeId": content_type_id,
        "latitude": coordinate.latitude,
        "longitude": coordinate.longitude,
        "stayMinutes": stay_minutes,
        "themes": ["SEA"],
        "_required": required,
    }


def coordinate_key(coordinate: Coordinate) -> tuple[float, float]:
    return (
        round(coordinate.latitude, 6),
        round(coordinate.longitude, 6),
    )


def fake_route_provider(minutes_by_leg, calls):
    def provider(mode, origin, destination, departure_at, cache=None):
        calls.append(
            (
                mode,
                coordinate_key(origin),
                coordinate_key(destination),
                departure_at,
            )
        )
        minutes = minutes_by_leg.get(
            (
                coordinate_key(origin),
                coordinate_key(destination),
            )
        )
        if minutes is None:
            return None
        return RouteResult(
            travelMinutes=minutes,
            requestedDepartureAt=departure_at,
            departureAt=departure_at,
            arrivalAt=departure_at + timedelta(minutes=minutes),
            mode=mode,
            provider="test",
        )

    return provider


def route_until_feasible(
    course,
    *,
    minutes_by_leg,
    return_by,
    mode=TransportMode.CAR,
    closed_names=None,
):
    closed_names = closed_names or set()
    calls = []

    with patch(
        "spontaneous.course.search_route",
        side_effect=fake_route_provider(minutes_by_leg, calls),
    ):
        while course:
            timeline = calculate_sequential_course_timeline(
                course,
                HOME,
                mode,
                datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                cache={},
            )

            invalid_index = None
            for index, item in enumerate(timeline["course"]):
                if item["name"] in closed_names:
                    invalid_index = index
                    break

            if invalid_index is not None:
                if course[invalid_index].get("_required", False):
                    raise ValueError("COURSE_NOT_FEASIBLE")
                course = normalize_course_orders(
                    course[:invalid_index] + course[invalid_index + 1:]
                )
                continue

            if timeline["estimatedReturnAt"] <= return_by:
                return timeline, calls

            trimmed = remove_last_optional_stop(course)
            if trimmed is None:
                raise ValueError("COURSE_NOT_FEASIBLE")
            course = trimmed

    raise ValueError("COURSE_NOT_FEASIBLE")


class SequentialCourseRoutingTest(TestCase):
    def test_car_sequential_timeline_uses_previous_departure(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        course = [
            stop("A", A_COORD, 1, 60),
            stop("B", B_COORD, 2, 90),
        ]
        calls = []

        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(
                {
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                    (coordinate_key(A_COORD), coordinate_key(B_COORD)): 20,
                    (coordinate_key(B_COORD), coordinate_key(HOME)): 30,
                },
                calls,
            ),
        ):
            timeline = calculate_sequential_course_timeline(
                course,
                HOME,
                TransportMode.CAR,
                start_at,
                cache={},
            )

        self.assertEqual(timeline["course"][0]["arrivalAt"], "2026-09-03T18:10:00+09:00")
        self.assertEqual(timeline["course"][0]["departureAt"], "2026-09-03T19:10:00+09:00")
        self.assertEqual(timeline["course"][1]["arrivalAt"], "2026-09-03T19:30:00+09:00")
        self.assertEqual(timeline["course"][1]["departureAt"], "2026-09-03T21:00:00+09:00")
        self.assertEqual(timeline["estimatedReturnAt"], datetime(2026, 9, 3, 21, 30, tzinfo=KST))
        self.assertEqual(calls[1][3], datetime(2026, 9, 3, 19, 10, tzinfo=KST))
        self.assertEqual(calls[2][3], datetime(2026, 9, 3, 21, 0, tzinfo=KST))

    def test_walk_uses_same_sequential_structure(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        calls = []

        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(
                {
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 5,
                    (coordinate_key(A_COORD), coordinate_key(B_COORD)): 6,
                    (coordinate_key(B_COORD), coordinate_key(HOME)): 7,
                },
                calls,
            ),
        ):
            calculate_sequential_course_timeline(
                [
                    stop("A", A_COORD, 1, 10),
                    stop("B", B_COORD, 2, 10),
                ],
                HOME,
                TransportMode.WALK,
                start_at,
                cache={},
            )

        self.assertEqual(calls[0][0], TransportMode.WALK)
        self.assertEqual(calls[1][3], datetime(2026, 9, 3, 18, 15, tzinfo=KST))
        self.assertEqual(calls[2][3], datetime(2026, 9, 3, 18, 31, tzinfo=KST))

    def test_public_transit_uses_time_aware_route_provider(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        provider_route = RouteResult(
            travelMinutes=42,
            requestedDepartureAt=start_at,
            departureAt=start_at,
            arrivalAt=datetime(2026, 9, 3, 18, 42, tzinfo=KST),
            mode=TransportMode.PUBLIC_TRANSIT,
            provider="TMAP_TRANSIT",
        )

        with patch(
            "spontaneous.routing.search_tmap_transit_route",
            return_value=provider_route,
        ) as provider:
            route = search_route(
                TransportMode.PUBLIC_TRANSIT,
                HOME,
                A_COORD,
                start_at,
                cache={},
            )

        self.assertIsNotNone(route)
        self.assertEqual(route.travelMinutes, 42)
        self.assertEqual(route.requestedDepartureAt, start_at)
        self.assertEqual(route.departureAt, start_at)
        self.assertEqual(route.arrivalAt, datetime(2026, 9, 3, 18, 42, tzinfo=KST))
        provider.assert_called_once()

    def test_timezone_offset_is_preserved(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        calls = []

        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(
                {
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                    (coordinate_key(A_COORD), coordinate_key(HOME)): 10,
                },
                calls,
            ),
        ):
            timeline = calculate_sequential_course_timeline(
                [stop("A", A_COORD, 1, 10)],
                HOME,
                TransportMode.CAR,
                start_at,
                cache={},
            )

        self.assertTrue(timeline["course"][0]["arrivalAt"].endswith("+09:00"))
        self.assertTrue(timeline["course"][0]["departureAt"].endswith("+09:00"))
        self.assertEqual(timeline["estimatedReturnAt"].utcoffset(), timedelta(hours=9))

    def test_optional_trimming_recalculates_from_home(self):
        course = [
            stop("A", A_COORD, 1, 10, required=True),
            stop("B", B_COORD, 2, 10, required=True),
            stop("C", C_COORD, 3, 200, required=False),
        ]

        timeline, calls = route_until_feasible(
            course,
            minutes_by_leg={
                (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                (coordinate_key(A_COORD), coordinate_key(B_COORD)): 10,
                (coordinate_key(B_COORD), coordinate_key(C_COORD)): 10,
                (coordinate_key(C_COORD), coordinate_key(HOME)): 10,
                (coordinate_key(B_COORD), coordinate_key(HOME)): 10,
            },
            return_by=datetime(2026, 9, 3, 19, 0, tzinfo=KST),
        )

        self.assertEqual([item["name"] for item in timeline["course"]], ["A", "B"])
        home_to_a_calls = [
            call
            for call in calls
            if call[1] == coordinate_key(HOME)
            and call[2] == coordinate_key(A_COORD)
        ]
        self.assertEqual(len(home_to_a_calls), 2)

    def test_required_only_over_time_fails(self):
        with self.assertRaisesRegex(ValueError, "COURSE_NOT_FEASIBLE"):
            route_until_feasible(
                [stop("A", A_COORD, 1, 120, required=True)],
                minutes_by_leg={
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                    (coordinate_key(A_COORD), coordinate_key(HOME)): 10,
                },
                return_by=datetime(2026, 9, 3, 18, 30, tzinfo=KST),
            )

    def test_outbound_no_route_fails(self):
        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider({}, []),
        ):
            with self.assertRaisesRegex(ValueError, "NO_ROUTE"):
                calculate_sequential_course_timeline(
                    [stop("A", A_COORD, 1, 10)],
                    HOME,
                    TransportMode.CAR,
                    datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    cache={},
                )

    def test_middle_no_route_fails(self):
        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(
                {
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                },
                [],
            ),
        ):
            with self.assertRaisesRegex(ValueError, "NO_ROUTE"):
                calculate_sequential_course_timeline(
                    [
                        stop("A", A_COORD, 1, 10),
                        stop("B", B_COORD, 2, 10),
                    ],
                    HOME,
                    TransportMode.CAR,
                    datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    cache={},
                )

    def test_return_no_route_fails(self):
        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(
                {
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                },
                [],
            ),
        ):
            with self.assertRaisesRegex(ValueError, "NO_ROUTE"):
                calculate_sequential_course_timeline(
                    [stop("A", A_COORD, 1, 10)],
                    HOME,
                    TransportMode.CAR,
                    datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    cache={},
                )

    def test_optional_closed_is_trimmed_and_recalculated(self):
        timeline, calls = route_until_feasible(
            [
                stop("A", A_COORD, 1, 10, required=True),
                stop("C", C_COORD, 2, 10, required=False, content_type_id="39"),
            ],
            minutes_by_leg={
                (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                (coordinate_key(A_COORD), coordinate_key(C_COORD)): 10,
                (coordinate_key(C_COORD), coordinate_key(HOME)): 10,
                (coordinate_key(A_COORD), coordinate_key(HOME)): 10,
            },
            return_by=datetime(2026, 9, 3, 21, 0, tzinfo=KST),
            closed_names={"C"},
        )

        self.assertEqual([item["name"] for item in timeline["course"]], ["A"])
        self.assertEqual(calls[0][1], coordinate_key(HOME))
        self.assertEqual(calls[3][1], coordinate_key(HOME))

    def test_required_closed_fails(self):
        with self.assertRaisesRegex(ValueError, "COURSE_NOT_FEASIBLE"):
            route_until_feasible(
                [stop("A", A_COORD, 1, 10, required=True, content_type_id="39")],
                minutes_by_leg={
                    (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
                    (coordinate_key(A_COORD), coordinate_key(HOME)): 10,
                },
                return_by=datetime(2026, 9, 3, 21, 0, tzinfo=KST),
                closed_names={"A"},
            )

    def test_public_transit_cache_includes_departure_at(self):
        with patch(
            "spontaneous.routing.search_tmap_transit_route",
            side_effect=[
                RouteResult(
                    travelMinutes=10,
                    requestedDepartureAt=datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    departureAt=datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    arrivalAt=datetime(2026, 9, 3, 18, 10, tzinfo=KST),
                    mode=TransportMode.PUBLIC_TRANSIT,
                    provider="TMAP_TRANSIT",
                ),
                RouteResult(
                    travelMinutes=20,
                    requestedDepartureAt=datetime(2026, 9, 3, 23, 0, tzinfo=KST),
                    departureAt=datetime(2026, 9, 3, 23, 0, tzinfo=KST),
                    arrivalAt=datetime(2026, 9, 3, 23, 20, tzinfo=KST),
                    mode=TransportMode.PUBLIC_TRANSIT,
                    provider="TMAP_TRANSIT",
                ),
            ],
        ) as provider:
            cache = {}
            first = search_route(
                TransportMode.PUBLIC_TRANSIT,
                HOME,
                A_COORD,
                datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                cache=cache,
            )
            second = search_route(
                TransportMode.PUBLIC_TRANSIT,
                HOME,
                A_COORD,
                datetime(2026, 9, 3, 23, 0, tzinfo=KST),
                cache=cache,
            )

        self.assertEqual(first.travelMinutes, 10)
        self.assertEqual(second.travelMinutes, 20)
        self.assertEqual(provider.call_count, 2)

    def test_deterministic_with_same_provider_responses(self):
        course = [stop("A", A_COORD, 1, 10)]
        minutes_by_leg = {
            (coordinate_key(HOME), coordinate_key(A_COORD)): 10,
            (coordinate_key(A_COORD), coordinate_key(HOME)): 10,
        }
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        calls = []

        with patch(
            "spontaneous.course.search_route",
            side_effect=fake_route_provider(minutes_by_leg, calls),
        ):
            first = calculate_sequential_course_timeline(
                course,
                HOME,
                TransportMode.CAR,
                start_at,
                cache={},
            )
            second = calculate_sequential_course_timeline(
                course,
                HOME,
                TransportMode.CAR,
                start_at,
                cache={},
            )

        self.assertEqual(first, second)

    def test_tmap_bus_service_one_is_available(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {
                                "totalTime": 600,
                                "legs": [
                                    {"mode": "WALK", "sectionTime": 2},
                                    {
                                        "mode": "BUS",
                                        "route": "100",
                                        "routeId": "bus-100",
                                        "service": 1,
                                        "start": {"name": "A", "lat": 35.1, "lon": 129.0},
                                        "end": {"name": "B", "lat": 35.2, "lon": 129.1},
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.totalSeconds, 600)

    def test_tmap_service_zero_itinerary_is_excluded(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {
                                "totalTime": 600,
                                "legs": [
                                    {"mode": "BUS", "route": "100", "service": 0},
                                ],
                            }
                        ]
                    }
                }
            }
        )

        self.assertIsNone(candidate)

    def test_tmap_uses_second_available_itinerary(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {
                                "totalTime": 600,
                                "legs": [{"mode": "BUS", "route": "100", "service": 0}],
                            },
                            {
                                "totalTime": 900,
                                "legs": [{"mode": "BUS", "route": "101", "service": 1}],
                            },
                        ]
                    }
                }
            }
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.totalSeconds, 900)
        self.assertEqual(candidate.legs[0].route, "101")

    def test_all_tmap_service_zero_is_no_route(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {"totalTime": 600, "legs": [{"mode": "BUS", "service": 0}]},
                            {"totalTime": 700, "legs": [{"mode": "SUBWAY", "service": 0}]},
                        ]
                    }
                }
            }
        )

        self.assertIsNone(candidate)

    def test_odsay_subway_schedule_after_requested_is_matched(self):
        requested = datetime(2026, 9, 3, 20, 0, tzinfo=KST)
        schedule = parse_odsay_schedule_payload(
            {
                "result": {
                    "notificationCode": 0,
                    "departureTime": "20:07:00",
                    "arrivalTime": "20:35:00",
                }
            },
            requested,
        )

        self.assertIsNotNone(schedule)
        self.assertEqual(schedule.departureAt, datetime(2026, 9, 3, 20, 7, tzinfo=KST))
        self.assertEqual(schedule.arrivalAt, datetime(2026, 9, 3, 20, 35, tzinfo=KST))

    def test_odsay_schedule_before_requested_is_not_usable(self):
        requested = datetime(2026, 9, 3, 0, 30, tzinfo=KST)
        schedule = parse_odsay_schedule_payload(
            {
                "result": {
                    "notificationCode": 3,
                    "departureTime": "00:10:00",
                    "arrivalTime": "00:25:00",
                }
            },
            requested,
        )

        self.assertIsNone(schedule)

    def test_first_train_after_requested_is_allowed(self):
        requested = datetime(2026, 9, 3, 4, 30, tzinfo=KST)
        schedule = parse_odsay_schedule_payload(
            {
                "result": {
                    "notificationCode": 2,
                    "departureTime": "05:08:00",
                    "arrivalTime": "05:40:00",
                }
            },
            requested,
        )

        self.assertEqual(schedule.departureAt, datetime(2026, 9, 3, 5, 8, tzinfo=KST))

    def test_provider_time_rolls_arrival_to_next_day(self):
        departure = datetime(2026, 9, 3, 23, 55, tzinfo=KST)
        arrival = combine_provider_time(
            departure,
            "00:15:00",
            not_before=departure,
        )

        self.assertEqual(arrival, datetime(2026, 9, 4, 0, 15, tzinfo=KST))

    def test_bus_realtime_uses_first_vehicle_after_requested(self):
        leg = TransitLeg(
            mode="BUS",
            route="100",
            startName="A",
            startLatitude=35.1,
            startLongitude=129.0,
        )

        with patch(
            "spontaneous.routing.busan_bims_get",
            side_effect=[
                [{"bstopid": "s1", "bstopnm": "A", "gpsy": "35.1", "gpsx": "129.0"}],
                [{"lineno": "100", "min1": "5", "min2": "17"}],
            ],
        ):
            realtime = select_busan_bims_realtime(
                leg,
                datetime(2026, 9, 3, 20, 10, tzinfo=KST),
                now=datetime(2026, 9, 3, 20, 0, tzinfo=KST),
                cache={},
            )

        self.assertEqual(
            realtime,
            BusRealtimeResult(
                boardingAt=datetime(2026, 9, 3, 20, 17, tzinfo=KST),
                sourceMinutes=17,
            ),
        )

    def test_bus_realtime_does_not_create_future_from_headway(self):
        leg = TransitLeg(
            mode="BUS",
            route="100",
            startName="A",
            startLatitude=35.1,
            startLongitude=129.0,
        )

        with patch(
            "spontaneous.routing.busan_bims_get",
            side_effect=[
                [{"bstopid": "s1", "bstopnm": "A", "gpsy": "35.1", "gpsx": "129.0"}],
                [{"lineno": "100", "min1": "5", "min2": "17", "headway": "12"}],
            ],
        ):
            realtime = select_busan_bims_realtime(
                leg,
                datetime(2026, 9, 3, 22, 0, tzinfo=KST),
                now=datetime(2026, 9, 3, 20, 0, tzinfo=KST),
                cache={},
            )

        self.assertIsNone(realtime)

    def test_mixed_bus_subway_all_service_one_succeeds(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {
                                "totalTime": 1200,
                                "legs": [
                                    {"mode": "BUS", "service": 1, "route": "100"},
                                    {"mode": "WALK"},
                                    {"mode": "SUBWAY", "service": 1, "route": "2"},
                                ],
                            }
                        ]
                    }
                }
            }
        )

        self.assertIsNotNone(candidate)

    def test_mixed_service_zero_excludes_itinerary(self):
        candidate = select_available_tmap_itinerary(
            {
                "metaData": {
                    "plan": {
                        "itineraries": [
                            {
                                "totalTime": 1200,
                                "legs": [
                                    {"mode": "BUS", "service": 1, "route": "100"},
                                    {"mode": "SUBWAY", "service": 0, "route": "2"},
                                ],
                            }
                        ]
                    }
                }
            }
        )

        self.assertIsNone(candidate)

    def test_return_route_no_service_prevents_course_success(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        course = [stop("A", A_COORD, 1, 60)]
        calls = []

        def provider(mode, origin, destination, departure_at, cache=None):
            calls.append((origin, destination, departure_at))
            if coordinate_key(origin) == coordinate_key(A_COORD):
                return None
            return RouteResult(
                travelMinutes=10,
                requestedDepartureAt=departure_at,
                departureAt=departure_at,
                arrivalAt=departure_at + timedelta(minutes=10),
                mode=mode,
                provider="TMAP_TRANSIT",
            )

        with patch("spontaneous.course.search_route", side_effect=provider):
            with self.assertRaisesRegex(ValueError, "NO_ROUTE"):
                calculate_sequential_course_timeline(
                    course,
                    HOME,
                    TransportMode.PUBLIC_TRANSIT,
                    start_at,
                    cache={},
                )

        self.assertEqual(calls[-1][2], datetime(2026, 9, 3, 19, 10, tzinfo=KST))

    def test_tmap_timeout_raises_provider_error(self):
        from urllib.error import URLError
        from spontaneous.routing import RoutingApiError, search_tmap_transit_route

        with patch.dict("os.environ", {"SKT_API_KEY": "test-key"}):
            with patch("spontaneous.routing.urlopen", side_effect=URLError("timeout")):
                with self.assertRaises(RoutingApiError):
                    search_tmap_transit_route(
                        HOME,
                        A_COORD,
                        datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                    )

    def test_odsay_quota_skips_exact_schedule_without_fake_timetable(self):
        from urllib.error import HTTPError
        from io import BytesIO
        from spontaneous.routing import search_odsay_subway_schedule

        class QuotaError(HTTPError):
            def read(self):
                return b'{"error":{"code":"429","message":"quota exceeded"}}'

        with patch.dict("os.environ", {"ODSAY_ENABLED": "true", "ODSAY_API_KEY": "test-key"}):
            with patch("spontaneous.routing.korea_subway_day", return_value=1):
                with patch("spontaneous.routing.urlopen", side_effect=QuotaError("", 429, "", {}, BytesIO())):
                    schedule = search_odsay_subway_schedule(
                        "1",
                        "2",
                        datetime(2026, 9, 3, 18, 0, tzinfo=KST),
                        cache={},
                    )

        self.assertIsNone(schedule)

    def test_same_departure_cache_hits(self):
        start_at = datetime(2026, 9, 3, 18, 0, tzinfo=KST)
        route = RouteResult(
            travelMinutes=10,
            requestedDepartureAt=start_at,
            departureAt=start_at,
            arrivalAt=start_at + timedelta(minutes=10),
            mode=TransportMode.PUBLIC_TRANSIT,
            provider="TMAP_TRANSIT",
        )

        with patch("spontaneous.routing.search_tmap_transit_route", return_value=route) as provider:
            cache = {}
            first = search_route(TransportMode.PUBLIC_TRANSIT, HOME, A_COORD, start_at, cache=cache)
            second = search_route(TransportMode.PUBLIC_TRANSIT, HOME, A_COORD, start_at, cache=cache)

        self.assertEqual(first, second)
        provider.assert_called_once()


if __name__ == "__main__":
    main()
