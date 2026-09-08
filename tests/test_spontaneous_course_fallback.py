import asyncio
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import app as data_app
from spontaneous.course import calculate_sequential_course_timeline, group_places_by_role
from spontaneous.models import TransportMode
from spontaneous.places import (
    base_course_place,
    convert_to_course_place,
    is_course_place_open_for_visit,
    is_open_now,
    search_food_detail,
)
from spontaneous.planner import MAX_CANDIDATES_PER_ROLE, MAX_COURSE_ATTEMPTS
from spontaneous.routing import route_cache_key, route_result_from_minutes
from spontaneous.service import coarse_course_place, has_coarse_course_viability
from tests.test_spontaneous_destination_routing_limit import START, START_AT, coordinate_key


COURSE_URL = "/api/v1/spontaneous-trips/course"
DESTINATIONS_URL = "/api/v1/spontaneous-trips/destinations"


def place(content_id, role="CAFE", rank=1):
    types = {"CAFE": "39", "MEAL": "39", "ACTIVITY": "12", "CULTURE": "14"}
    return {
        "contentid": content_id,
        "title": ("해변 " if role == "ACTIVITY" else "Place ") + content_id,
        "contenttypeid": types[role],
        "cat3": "A05020900" if role == "CAFE" else "",
        "mapy": str(START.latitude + rank * 0.001),
        "mapx": str(START.longitude + (0.002 if role == "CAFE" else 0.001)),
    }


def payload(themes=("CAFE",), minutes=480):
    start = START_AT.replace(hour=10)
    return {
        "destinationId": "BUSAN_GWANGALLI",
        "startLocation": START.model_dump(),
        "startAt": start.isoformat(),
        "returnBy": (start + timedelta(minutes=minutes)).isoformat(),
        "desiredThemes": list(themes),
        "transportMode": "PUBLIC_TRANSIT",
    }


def post_json(path, body):
    """Exercise real FastAPI routing, request validation and serialization via ASGI."""
    async def post():
        messages = []
        request_body = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": request_body, "more_body": False}

        async def send(message):
            messages.append(message)

        await data_app.app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
        }, receive, send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        response = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(response)

    return asyncio.run(post())


@contextmanager
def providers(places, hours=None, routing=None):
    hours = hours or {}
    by_coordinate = {
        (round(float(item["mapy"]), 6), round(float(item["mapx"]), 6)): item["contentid"]
        for item in places
    }
    calls = {"routes": [], "details": []}

    def route(origin, destination, departure_at, cache=None):
        calls["routes"].append(route_cache_key(
            TransportMode.PUBLIC_TRANSIT, origin, destination, departure_at,
        ))
        origin_id = by_coordinate.get(coordinate_key(origin), "home")
        destination_id = by_coordinate.get(coordinate_key(destination), "home")
        minutes = routing(origin_id, destination_id, departure_at) if routing else 10
        if minutes is None:
            return None
        return route_result_from_minutes(
            mode=TransportMode.PUBLIC_TRANSIT, provider="TMAP_TRANSIT",
            travel_minutes=minutes, departure_at=departure_at,
        )

    def detail(url, **kwargs):
        content_id = parse_qs(urlparse(url).query)["contentId"][0]
        calls["details"].append(content_id)
        item = {"opentimefood": hours.get(content_id, "00:00~23:59")}
        return BytesIO(json.dumps({"response": {"body": {"items": {"item": [item]}}}}).encode())

    with (
        patch.dict("os.environ", {"TOUR_API_KEY": "test-key", "SKT_API_KEY": "test-key"}),
        patch("app.search_places_by_zone", side_effect=lambda zone, places_cache=None: places),
        patch("spontaneous.places.urlopen", side_effect=detail),
        patch("spontaneous.routing.search_tmap_transit_route", side_effect=route),
        patch("app.calculate_sequential_course_timeline", wraps=calculate_sequential_course_timeline) as timeline,
    ):
        calls["timeline"] = timeline
        yield calls


class SpontaneousCourseFallbackTest(TestCase):
    def assert_success(self, response, ids):
        status, body = response
        self.assertEqual(status, 200, body)
        self.assertEqual([stop["contentId"] for stop in body["course"]], ids)
        self.assertEqual([stop["order"] for stop in body["course"]], list(range(1, len(ids) + 1)))
        self.assertLessEqual(datetime.fromisoformat(body["estimatedReturnAt"]), datetime.fromisoformat(body["returnBy"]))

    def test_required_closed_cafe_replaced_at_arrival_and_departure(self):
        for closed_hours in ("12:00~18:00", "10:00~10:30"):
            with self.subTest(hours=closed_hours), providers(
                [place("c1"), place("c2", rank=2)], hours={"c1": closed_hours},
            ) as calls, self.assertLogs("data.app", level="INFO") as logs:
                self.assert_success(post_json(COURSE_URL, payload()), ["c2"])
            self.assertEqual(calls["timeline"].call_count, 2)
            self.assertEqual(Counter(calls["details"]), {"c1": 1, "c2": 1})
            message = "\n".join(logs.output)
            self.assertIn("failureReason=PLACE_CLOSED_AT_VISIT_TIME failedRole=CAFE failedContentId=c1", message)
            self.assertIn("attempt=2 replacementRole=CAFE replacementContentId=c2", message)
            self.assertNotIn("test-key", message)

    def test_required_no_route_retries_cafe_and_first_activity(self):
        for role, themes in (("CAFE", ("CAFE",)), ("ACTIVITY", ("SEA",))):
            with self.subTest(role=role), providers(
                [place("first", role), place("second", role, 2)],
                routing=lambda origin, destination, time: None if destination == "first" else 10,
            ) as calls:
                self.assert_success(post_json(COURSE_URL, payload(themes)), ["second"])
            self.assertEqual(calls["timeline"].call_count, 2)

    def test_closed_then_no_route_then_third_candidate_succeeds(self):
        with providers(
            [place("c1"), place("c2", rank=2), place("c3", rank=3)],
            hours={"c1": "00:00~01:00"},
            routing=lambda origin, destination, time: None if destination == "c2" else 10,
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload()), ["c3"])
        self.assertEqual(calls["timeline"].call_count, 3)

    def test_required_return_time_exceeded_retries_shorter_route(self):
        with providers(
            [place("c1"), place("c2", rank=2)],
            routing=lambda origin, destination, time: 70 if "c1" in (origin, destination) else 10,
        ) as calls, self.assertLogs("data.app", level="INFO") as logs:
            self.assert_success(post_json(COURSE_URL, payload(minutes=150)), ["c2"])
        self.assertEqual(calls["timeline"].call_count, 2)
        self.assertIn("failureReason=RETURN_TIME_EXCEEDED", "\n".join(logs.output))

    def test_return_time_replaces_optional_cafe_before_dropping_it(self):
        with providers(
            [place("a1", "ACTIVITY"), place("c1"), place("c2", rank=2)],
            routing=lambda origin, destination, time: 200 if "c1" in (origin, destination) else 10,
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload(("SEA",), minutes=300)), ["a1", "c2"])
        self.assertEqual(calls["timeline"].call_count, 2)

    def test_return_time_drops_optional_then_replaces_required_without_readding_optional(self):
        with providers(
            [place("a1", "ACTIVITY"), place("a2", "ACTIVITY", 2), place("c1"), place("c2", rank=2)],
            routing=lambda origin, destination, time: 150 if "a1" in (origin, destination) else 10,
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload(("SEA",), minutes=300)), ["a2"])
        attempts = [[stop["contentId"] for stop in call.args[0]] for call in calls["timeline"].call_args_list]
        self.assertEqual(attempts, [["a1", "c1"], ["a1", "c2"], ["a1"], ["a2"]])

    def test_return_time_can_require_replacing_two_required_roles(self):
        with providers(
            [place("a1", "ACTIVITY"), place("a2", "ACTIVITY", 2), place("c1"), place("c2", rank=2)],
            routing=lambda origin, destination, time: 150 if {origin, destination} & {"a1", "c1"} else 10,
        ):
            self.assert_success(post_json(COURSE_URL, payload(("SEA", "CAFE"), minutes=300)), ["a2", "c2"])

    def test_optional_closed_cafe_is_replaced_before_omission(self):
        with providers(
            [place("a1", "ACTIVITY"), place("c1"), place("c2", rank=2)],
            hours={"c1": "00:00~01:00"},
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload(("SEA",))), ["a1", "c2"])
        self.assertEqual(calls["timeline"].call_count, 2)

    def test_all_optional_candidates_fail_then_required_course_succeeds(self):
        with providers(
            [place("a1", "ACTIVITY"), place("c1"), place("c2", rank=2)],
            hours={"c1": "00:00~01:00", "c2": "00:00~01:00"},
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload(("SEA",))), ["a1"])
        self.assertEqual(calls["timeline"].call_count, 3)
        self.assertEqual(len(calls["routes"]), len(set(calls["routes"])))

    def test_all_five_required_cafes_closed_exhausts_candidates(self):
        places = [place(f"c{index}", rank=index) for index in range(1, 6)]
        with providers(places, hours={item["contentid"]: "00:00~01:00" for item in places}) as calls:
            self.assertEqual(post_json(COURSE_URL, payload()), (422, {"detail": "COURSE_NOT_FEASIBLE"}))
        self.assertEqual(calls["timeline"].call_count, MAX_CANDIDATES_PER_ROLE)
        self.assertEqual(len(calls["details"]), 5)

    def test_tmap_429_and_5xx_abort_without_place_retry(self):
        from spontaneous.routing import search_tmap_transit_route

        for status in (429, 500, 503):
            with self.subTest(status=status), providers(
                [place(f"c{index}", rank=index) for index in range(1, 6)],
            ) as calls, patch(
                "spontaneous.routing.search_tmap_transit_route", wraps=search_tmap_transit_route,
            ), patch("spontaneous.routing.urlopen", side_effect=HTTPError(
                "https://apis.openapi.sk.com/transit/routes", status, "provider error", None, None,
            )) as http:
                self.assertEqual(post_json(COURSE_URL, payload()), (
                    503 if status == 429 else 502,
                    {"detail": "TMAP_QUOTA_EXCEEDED" if status == 429 else "EXTERNAL_ROUTING_API_ERROR"},
                ))
            self.assertEqual(calls["timeline"].call_count, 1)
            self.assertEqual(http.call_count, 1)

    def test_unique_combinations_attempt_limit_and_negative_route_cache(self):
        places = ([place(f"a{i}", "ACTIVITY", i) for i in range(1, 6)]
                  + [place(f"c{i}", rank=i) for i in range(1, 6)])
        with providers(places, routing=lambda origin, destination, time: None) as calls, self.assertLogs(
            "data.app", level="INFO",
        ) as logs:
            self.assertEqual(post_json(COURSE_URL, payload(("SEA", "CAFE"))),
                             (422, {"detail": "COURSE_NOT_FEASIBLE"}))
        attempts = [tuple(stop["contentId"] for stop in call.args[0])
                    for call in calls["timeline"].call_args_list]
        self.assertEqual(len(attempts), len(set(attempts)))
        self.assertEqual(len(attempts), MAX_COURSE_ATTEMPTS)
        self.assertEqual(len(calls["routes"]), 5)  # Each identical failed first leg is cached.
        self.assertEqual(len(calls["details"]), 5)
        self.assertIn("attempts=20", "\n".join(logs.output))
        self.assertIn("searchLimitReached=True", "\n".join(logs.output))

    def test_duplicate_records_do_not_hide_fallback_or_duplicate_stops(self):
        first = place("c1")
        with providers([first] * 6 + [place("c2", rank=2)], hours={"c1": "00:00~01:00"}) as calls:
            self.assert_success(post_json(COURSE_URL, payload()), ["c2"])
        self.assertEqual(calls["details"], ["c1", "c2"])

    def test_return_leg_no_route_replaces_last_required_stop(self):
        with providers(
            [place("c1"), place("c2", rank=2)],
            routing=lambda origin, destination, time: None if origin == "c1" and destination == "home" else 10,
        ):
            self.assert_success(post_json(COURSE_URL, payload()), ["c2"])

    def test_unroutable_edge_can_be_fixed_by_replacing_previous_role(self):
        with providers(
            [place("a1", "ACTIVITY"), place("a2", "ACTIVITY", 2), place("c1")],
            routing=lambda origin, destination, time: None if (origin, destination) == ("a1", "c1") else 10,
        ):
            self.assert_success(post_json(COURSE_URL, payload(("SEA", "CAFE"))), ["a2", "c1"])

    def test_same_cafe_can_succeed_after_upstream_arrival_time_changes(self):
        with providers(
            [place("a1", "ACTIVITY"), place("a2", "ACTIVITY", 2), place("c1")],
            hours={"c1": "12:00~18:00"},
            routing=lambda origin, destination, time: 80 if destination == "a2" else 10,
        ):
            self.assert_success(post_json(COURSE_URL, payload(("SEA", "CAFE"))), ["a2", "c1"])

    def test_no_route_cache_does_not_poison_same_place_at_later_departure(self):
        def routing(origin, destination, time):
            if destination == "c1" and time.hour < 12:
                return None
            return 80 if destination == "a2" else 10

        with providers(
            [place("a1", "ACTIVITY"), place("a2", "ACTIVITY", 2), place("c1")], routing=routing,
        ) as calls:
            self.assert_success(post_json(COURSE_URL, payload(("SEA", "CAFE"))), ["a2", "c1"])
        self.assertEqual(calls["timeline"].call_count, 2)
        self.assertEqual(len(calls["routes"]), len(set(calls["routes"])))

    def test_activity_fallback_can_replace_one_stop_with_two_theme_covering_stops(self):
        combined = place("combined", "CULTURE")
        combined["title"] = "공원 museum"  # CULTURE + NATURE, ranked ahead of single-theme places.
        nature = place("nature", "ACTIVITY", 2)
        nature["title"] = "공원"
        culture = place("culture", "CULTURE", 3)
        with providers(
            [combined, nature, culture],
            routing=lambda origin, destination, time: None if destination == "combined" else 10,
        ):
            status, body = post_json(COURSE_URL, payload(("NATURE", "CULTURE")))
        self.assertEqual(status, 200, body)
        self.assertEqual({stop["contentId"] for stop in body["course"]}, {"nature", "culture"})
        self.assertTrue({"NATURE", "CULTURE"}.issubset({theme for stop in body["course"] for theme in stop["themes"]}))

    def test_missing_required_theme_in_initial_selection_is_repaired(self):
        from spontaneous.course import generate_course

        def partial(*args, **kwargs):
            return generate_course(*args, **kwargs)[:1]

        nature = place("nature", "ACTIVITY")
        nature["title"] = "공원"
        with providers([nature, place("culture", "CULTURE", 2)]), patch(
            "app.generate_course", side_effect=partial,
        ), self.assertLogs("data.app", level="INFO") as logs:
            status, body = post_json(COURSE_URL, payload(("NATURE", "CULTURE")))
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["course"]), 2)
        self.assertIn("failureReason=MISSING_REQUIRED_THEME", "\n".join(logs.output))

    def test_missing_required_role_in_initial_selection_is_repaired(self):
        from spontaneous.course import generate_course

        def partial(*args, **kwargs):
            return generate_course(*args, **kwargs)[:1]

        with providers([place("a1", "ACTIVITY"), place("c1")]), patch(
            "app.generate_course", side_effect=partial,
        ), self.assertLogs("data.app", level="INFO") as logs:
            self.assert_success(post_json(COURSE_URL, payload(("SEA", "CAFE"))), ["a1", "c1"])
        self.assertIn("failureReason=MISSING_REQUIRED_ROLE failedRole=CAFE", "\n".join(logs.output))

    def test_candidate_limit_preserves_rare_required_theme(self):
        nature = place("nature", "ACTIVITY", 8)
        nature["title"] = "공원"
        places = [place(f"culture{i}", "CULTURE", i) for i in range(1, 6)] + [nature]
        with providers(places):
            self.assert_success(post_json(COURSE_URL, payload(("NATURE", "CULTURE"))), ["culture1", "nature"])

    def test_empty_or_failed_detail_lookups_are_cached_for_the_request(self):
        for outcome in (BytesIO(b'{"response":{"body":{"items":{"item":[]}}}}'), OSError("timeout")):
            with self.subTest(outcome=type(outcome).__name__), patch.dict(
                "os.environ", {"TOUR_API_KEY": "test-key"},
            ), patch("spontaneous.places.urlopen", side_effect=[outcome]) as http:
                cache = {}
                self.assertEqual(search_food_detail("c1", detail_cache=cache), {})
                self.assertEqual(search_food_detail("c1", detail_cache=cache), {})
                self.assertEqual(http.call_count, 1)

    def test_destinations_to_course_acceptance_uses_same_request_and_real_fallback(self):
        places = [place("a1", "ACTIVITY"), place("c1"), place("c2", rank=2)]
        request = payload(("SEA", "CAFE"))
        destination_request = {key: value for key, value in request.items() if key != "destinationId"}
        with providers(places, hours={"c1": "00:00~01:00"}) as calls:
            status, recommendation = post_json(DESTINATIONS_URL, destination_request)
            self.assertEqual(status, 200, recommendation)
            self.assertTrue(recommendation["destinations"])
            self.assertEqual(calls["timeline"].call_count, 0)
            self.assertEqual(calls["details"], [])
            self.assertEqual(len(calls["routes"]), 2 * len(recommendation["destinations"]))
            request["destinationId"] = recommendation["destinations"][0]["destinationId"]
            self.assert_success(post_json(COURSE_URL, request), ["a1", "c2"])
        self.assertEqual(calls["timeline"].call_count, 2)

    def test_equivalent_korea_and_utc_course_requests_select_same_cafe(self):
        cafe = place("c1")

        def car_route(mode, origin, destination, departure_at, cache=None):
            travel_minutes = 48 if coordinate_key(origin) == coordinate_key(START) else 10
            return route_result_from_minutes(
                mode=mode,
                provider="TMAP",
                travel_minutes=travel_minutes,
                departure_at=departure_at,
            )

        request = payload(("CAFE",))
        request.update({
            "destinationId": "BUSAN_SONGJEONG",
            "transportMode": "CAR",
            "startAt": "2026-09-08T13:00:00+09:00",
            "returnBy": "2026-09-08T21:00:00+09:00",
        })
        utc_request = {
            **request,
            "startAt": "2026-09-08T04:00:00Z",
            "returnBy": "2026-09-08T12:00:00Z",
            "validTimeRange": True,
        }

        with providers([cafe], hours={"c1": "11:00~18:00"}), patch(
            "spontaneous.course.search_route",
            side_effect=car_route,
        ):
            korea_status, korea_response = post_json(COURSE_URL, request)
            utc_status, utc_response = post_json(COURSE_URL, utc_request)

        self.assertEqual(korea_status, 200, korea_response)
        self.assertEqual(utc_status, 200, utc_response)
        self.assertEqual(
            [stop["contentId"] for stop in korea_response["course"]],
            ["c1"],
        )
        self.assertEqual(
            [stop["contentId"] for stop in utc_response["course"]],
            ["c1"],
        )
        self.assertEqual(
            datetime.fromisoformat(korea_response["course"][0]["arrivalAt"]),
            datetime.fromisoformat(utc_response["course"][0]["arrivalAt"]),
        )

    def test_utc_visit_times_are_checked_in_korea_local_time(self):
        stop = {
            "contentId": "c1",
            "contentTypeId": "39",
        }
        with patch(
            "spontaneous.places.search_food_detail",
            return_value={"opentimefood": "11:00~18:00"},
        ):
            self.assertTrue(is_course_place_open_for_visit(
                stop,
                datetime(2026, 9, 8, 4, 48, tzinfo=timezone.utc),
                datetime(2026, 9, 8, 5, 48, tzinfo=timezone.utc),
            ))
            self.assertFalse(is_course_place_open_for_visit(
                stop,
                datetime(2026, 9, 8, 1, 48, tzinfo=timezone.utc),
                datetime(2026, 9, 8, 4, 48, tzinfo=timezone.utc),
            ))
            self.assertFalse(is_course_place_open_for_visit(
                stop,
                datetime(2026, 9, 8, 4, 48, tzinfo=timezone.utc),
                datetime(2026, 9, 8, 9, 1, tzinfo=timezone.utc),
            ))

        self.assertTrue(is_open_now(
            "11:00~18:00",
            datetime(2026, 9, 8, 4, 48, tzinfo=timezone.utc),
        ))
        self.assertFalse(is_open_now(
            "11:00~18:00",
            datetime(2026, 9, 8, 4, 48),
        ))

    def test_coarse_viability_uses_shared_shape_without_details_or_routing(self):
        cafe = place("c1")
        with patch("spontaneous.places.search_food_detail", return_value={"firstmenu": "해산물"}):
            enriched = convert_to_course_place(cafe)
        self.assertEqual(coarse_course_place(cafe), base_course_place(cafe))
        self.assertEqual(set(group_places_by_role([coarse_course_place(cafe)])), {"CAFE"})
        self.assertEqual(set(group_places_by_role([enriched])), {"CAFE"})
        self.assertTrue(has_coarse_course_viability([cafe], ["CAFE"]))
        self.assertFalse(has_coarse_course_viability([cafe], ["SEA", "CAFE"]))
        self.assertFalse(has_coarse_course_viability([{"contenttypeid": "32"}], []))
