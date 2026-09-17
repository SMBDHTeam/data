from datetime import datetime, timedelta, timezone
from random import Random
from unittest import TestCase
from unittest.mock import patch

from spontaneous.course import (
    build_course_role_plan,
    course_stop_range,
    generate_course,
    get_place_identity,
    order_course_candidates,
    place_ranking_key,
)
from spontaneous.course_policy import (
    calculate_onsite_minutes,
    minimum_acceptable_course_stops,
)
from spontaneous.models import TransportMode
from spontaneous.places import base_course_place
from spontaneous.planner import CourseCandidateSearch, MAX_CANDIDATES_PER_ROLE, MAX_COURSE_ATTEMPTS, rank_course_candidates
from spontaneous.routing import get_transport_option, route_result_from_minutes
from spontaneous.time_profile import TimeProfile, resolve_time_profile
from tests.test_spontaneous_course_fallback import COURSE_URL, payload, place, post_json, providers
from tests.test_spontaneous_destination_routing_limit import START, START_AT


def trip(hour, minutes, themes=()):
    request = payload(themes, minutes)
    start = START_AT.replace(hour=hour)
    request.update(startAt=start.isoformat(), returnBy=(start + timedelta(minutes=minutes)).isoformat())
    return request


def night_place(content_id, rank=4):
    record = place(content_id, "ACTIVITY", rank)
    record["title"] = "전망대 " + content_id
    return record


def varied_places():
    return [place("a", "ACTIVITY"), place("m", "MEAL", 2), place("c", rank=3), night_place("n")]


class TimeAwareCourseTest(TestCase):
    def assert_success(self, response, minimum=1, maximum=5):
        status, body = response
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(body["course"]), minimum)
        self.assertLessEqual(len(body["course"]), maximum)
        self.assertLessEqual(datetime.fromisoformat(body["estimatedReturnAt"]), datetime.fromisoformat(body["returnBy"]))
        self.assertEqual(len({stop["contentId"] for stop in body["course"]}), len(body["course"]))
        return body

    def check_duration(self, minutes, minimum, maximum):
        with providers(varied_places()) as calls:
            body = self.assert_success(post_json(COURSE_URL, trip(15, minutes + 20)), minimum, maximum)
        self.assertGreaterEqual(calls["timeline"].call_count, 1)
        self.assertLessEqual(calls["timeline"].call_count, MAX_COURSE_ATTEMPTS)
        self.assertGreaterEqual(len(calls["routes"]), len(body["course"]) + 1)
        self.assertEqual(course_stop_range(minutes), (minimum, maximum))

    def test_ninety_minutes_has_at_most_two_stops(self):
        self.check_duration(90, 1, 2)

    def test_three_hours_targets_two_to_three_stops(self):
        self.check_duration(180, 2, 3)

    def test_five_hours_targets_three_to_four_stops(self):
        self.check_duration(300, 3, 4)

    def test_seven_hours_still_has_at_most_four_stops(self):
        self.check_duration(420, 3, 4)

    def test_stop_policy_exact_boundaries(self):
        for minutes, expected in ((119, (1, 2)), (120, (2, 3)), (239, (2, 3)),
                                  (240, (3, 4)), (359, (3, 4)), (360, (3, 4))):
            onsite = calculate_onsite_minutes(
                START_AT, START_AT + timedelta(minutes=minutes + 20), 10, 10,
            )
            self.assertEqual(onsite, minutes)
            self.assertEqual(course_stop_range(minutes), expected)

    def test_hard_minimum_allows_a_smaller_valid_fallback(self):
        for minutes, expected in ((119, 1), (120, 1), (239, 1), (240, 2), (420, 2)):
            self.assertEqual(minimum_acceptable_course_stops(minutes), expected)

    def test_four_hour_onsite_window_builds_three_or_four_stops(self):
        with providers(varied_places()) as calls:
            body = self.assert_success(
                post_json(COURSE_URL, trip(15, 260)), 3, 4,
            )
        self.assertEqual(len(body["course"]), 3)
        self.assertLessEqual(calls["timeline"].call_count, MAX_COURSE_ATTEMPTS)

    def test_four_hour_window_does_not_accept_initial_two_stop_course(self):
        def only_two(*args, **kwargs):
            return generate_course(*args, **kwargs)[:2]

        with providers(varied_places()), patch("app.generate_course", side_effect=only_two):
            body = self.assert_success(
                post_json(COURSE_URL, trip(15, 260)), 3, 4,
            )
        self.assertEqual(len(body["course"]), 3)

    def test_four_hour_window_returns_two_valid_stops_when_third_is_unavailable(self):
        records = [place("a", "ACTIVITY"), place("c", "CAFE", 2)]
        request = trip(15, 260, ("SEA", "CAFE"))
        request["transportMode"] = "WALK"

        def walk_route(mode, origin, destination, departure_at, cache=None):
            self.assertEqual(mode, TransportMode.WALK)
            return route_result_from_minutes(
                mode=mode,
                provider="TEST_WALK",
                travel_minutes=10,
                departure_at=departure_at,
            )

        with providers(records), patch(
            "spontaneous.course.search_route", side_effect=walk_route,
        ), self.assertLogs("data.app", level="INFO") as logs:
            body = self.assert_success(
                post_json(COURSE_URL, request), 2, 2,
            )
        self.assertEqual(body["transportMode"], "WALK")
        self.assertEqual(len(body["course"]), 2)
        summary = next(message for message in logs.output if "course search summary" in message)
        for field in (
            "onsiteMinutes=240", "targetMinStops=3", "targetMaxStops=4",
            "selectedStopCount=2", "attemptCount=", "selectedStops=",
            "failureReason=None", "rejectionReasons=", "preferredTargetMet=False",
        ):
            self.assertIn(field, summary)

    def test_four_hour_window_still_rejects_a_single_stop_course(self):
        with providers([place("a", "ACTIVITY")]):
            self.assertEqual(
                post_json(COURSE_URL, trip(15, 260, ("SEA",))),
                (422, {"detail": "COURSE_NOT_FEASIBLE"}),
            )

    def test_first_unroutable_combination_uses_alternative_and_keeps_density(self):
        records = [
            place("a1", "ACTIVITY"),
            place("a2", "ACTIVITY", 2),
            place("m1", "MEAL", 3),
            place("c1", "CAFE", 4),
        ]
        with providers(
            records,
            routing=lambda origin, destination, time: None if destination == "a1" else 10,
        ) as calls:
            body = self.assert_success(
                post_json(COURSE_URL, trip(15, 260, ("SEA",))), 3, 4,
            )
        self.assertIn("a2", {stop["contentId"] for stop in body["course"]})
        self.assertGreaterEqual(calls["timeline"].call_count, 2)

    def test_seeded_course_selection_can_choose_different_valid_combinations(self):
        records = (
            [place(f"a{index}", "ACTIVITY", index) for index in range(1, 4)]
            + [place(f"m{index}", "MEAL", index + 3) for index in range(1, 4)]
            + [place(f"c{index}", "CAFE", index + 6) for index in range(1, 4)]
        )
        combinations = []
        for seed in (1, 7):
            with providers(records), patch(
                "app.COURSE_RNG_FACTORY", side_effect=lambda seed=seed: Random(seed),
            ):
                body = self.assert_success(
                    post_json(COURSE_URL, trip(15, 260, ("SEA",))), 3, 4,
                )
            combinations.append(tuple(stop["contentId"] for stop in body["course"]))
        self.assertNotEqual(combinations[0], combinations[1])

    def test_diversity_pool_never_promotes_low_quality_candidate(self):
        leader = base_course_place(place("leader", "CAFE", 1))
        peer = base_course_place(place("peer", "CAFE", 2))
        low = base_course_place(place("low", "CAFE", 3))
        low["latitude"] = START.latitude + 10
        low["longitude"] = START.longitude + 10
        for seed in range(20):
            ordered = order_course_candidates(
                [low, peer, leader], {"CAFE"}, "CAFE", START,
                rng=Random(seed),
            )
            self.assertNotEqual(ordered[0]["contentId"], "low")

    def test_missing_content_id_identity_normalizes_name_and_coordinates(self):
        first = {"name": "  Same   Place ", "latitude": 35.1, "longitude": 129.1}
        duplicate = {"title": "same place", "mapy": "35.1000001", "mapx": "129.1000001"}
        self.assertEqual(get_place_identity(first), get_place_identity(duplicate))

    def test_stop_policy_is_applied_for_every_transport_mode(self):
        def route(mode, origin, destination, departure_at, cache=None):
            return route_result_from_minutes(
                mode=mode,
                provider="TEST",
                travel_minutes=10,
                departure_at=departure_at,
            )

        for mode in TransportMode:
            with self.subTest(mode=mode), providers(varied_places()), patch(
                "spontaneous.course.search_route", side_effect=route,
            ):
                request = trip(15, 260)
                request["transportMode"] = mode.value
                body = self.assert_success(post_json(COURSE_URL, request), 3, 4)
            self.assertLessEqual(len(body["course"]), 4)

    def test_destination_transport_and_course_policy_share_onsite_boundary(self):
        def route(mode, origin, destination, departure_at, cache=None):
            return route_result_from_minutes(
                mode=mode,
                provider="TEST",
                travel_minutes=10,
                departure_at=departure_at,
            )

        destination = START.model_copy(update={
            "latitude": START.latitude + 0.01,
            "longitude": START.longitude + 0.01,
        })
        for onsite, expected in ((119, (1, 2)), (120, (2, 3)),
                                 (239, (2, 3)), (240, (3, 4))):
            with self.subTest(onsite=onsite), patch(
                "spontaneous.routing.search_route", side_effect=route,
            ):
                transport = get_transport_option(
                    START,
                    destination,
                    TransportMode.CAR,
                    START_AT,
                    START_AT + timedelta(minutes=onsite + 20),
                )
            self.assertTrue(transport.available)
            self.assertEqual(transport.availableStayMinutes, onsite)
            self.assertEqual(course_stop_range(transport.availableStayMinutes), expected)

    def test_success_summary_log_contains_density_and_selection_context(self):
        with providers(varied_places()), self.assertLogs("data.app", level="INFO") as logs:
            self.assert_success(post_json(COURSE_URL, trip(15, 260)), 3, 4)
        summary = next(message for message in logs.output if "course search summary" in message)
        for field in (
            "destinationId=", "transportMode=", "onsiteMinutes=240",
            "targetMinStops=3", "targetMaxStops=4", "selectedStopCount=3",
            "attemptCount=", "selectedStops=", "failureReason=None",
            "rejectionReasons=",
        ):
            self.assertIn(field, summary)

    def test_seven_hours_rejects_required_five_stop_course(self):
        records = varied_places() + [place("museum", "CULTURE", 5)]
        with providers(records):
            self.assertEqual(
                post_json(COURSE_URL, trip(15, 420, ("SEA", "CULTURE", "FOOD", "CAFE", "NIGHT_VIEW"))),
                (422, {"detail": "COURSE_THEME_NOT_FEASIBLE"}),
            )

    def test_short_trip_does_not_silently_drop_required_themes_to_meet_cap(self):
        with providers(varied_places()) as calls:
            self.assertEqual(post_json(COURSE_URL, trip(20, 90, ("SEA", "CAFE", "FOOD", "NIGHT_VIEW"))),
                             (422, {"detail": "COURSE_THEME_NOT_FEASIBLE"}))
        # The onsite band is known only after exact outbound/return routing.
        self.assertTrue(calls["routes"])

    def test_short_trip_can_trim_optional_stops_without_losing_requested_coverage(self):
        with providers(varied_places()):
            body = self.assert_success(post_json(COURSE_URL, trip(20, 90, ("CAFE",))), 1, 2)
        self.assertIn("CAFE", {theme for stop in body["course"] for theme in stop["themes"]})

    def test_cafe_course_returns_200_in_afternoon_and_night_when_open(self):
        for hour in (15, 20):
            with self.subTest(hour=hour), providers([place("c")], hours={"c": "14:00~23:59"}) as calls:
                body = self.assert_success(post_json(COURSE_URL, trip(hour, 120, ("CAFE",))))
                self.assertEqual([stop["contentId"] for stop in body["course"]], ["c"])
                self.assertEqual(calls["details"], ["c"])
                self.assertEqual(len(calls["routes"]), 2)

    def test_late_night_cafe_supports_overnight_opening_hours(self):
        with providers([place("c")], hours={"c": "18:00~02:00"}):
            body = self.assert_success(post_json(COURSE_URL, trip(23, 120, ("CAFE",))))
        self.assertIn("CAFE", body["course"][0]["themes"])

    def test_night_cafe_fallback_rejects_closed_arrival_or_departure(self):
        cafe = place("closed")
        for hours in ("10:00~19:00", "20:00~20:30"):
            with self.subTest(hours=hours), providers([cafe, place("open", rank=2)], hours={"closed": hours}) as calls:
                body = self.assert_success(post_json(COURSE_URL, trip(20, 120, ("CAFE",))))
                self.assertEqual([stop["contentId"] for stop in body["course"]], ["open"])
                self.assertEqual(calls["timeline"].call_count, 2)

    def test_return_by_is_enforced_after_real_routing_for_night_cafe(self):
        with providers([place("c")], routing=lambda origin, destination, time: 20):
            self.assertEqual(post_json(COURSE_URL, trip(20, 90, ("CAFE",))),
                             (422, {"detail": "COURSE_RETURN_TIME_EXCEEDED"}))

    def test_required_cafe_outranks_optional_night_view(self):
        with providers([place("c"), night_place("n")]):
            body = self.assert_success(post_json(COURSE_URL, trip(20, 90, ("CAFE",))))
        self.assertEqual([stop["contentId"] for stop in body["course"]], ["c"])

    def test_empty_themes_change_optional_role_by_start_at(self):
        afternoon = build_course_role_plan(set(), 90, START_AT.replace(hour=15), TransportMode.WALK)
        night = build_course_role_plan(set(), 90, START_AT.replace(hour=20), TransportMode.WALK)
        self.assertEqual(afternoon[0][0], "CAFE")
        self.assertEqual(night[0][0], "NIGHT_VIEW")
        for hour, expected in ((15, "CAFE"), (20, "NIGHT_VIEW")):
            with providers(varied_places()):
                body = self.assert_success(post_json(COURSE_URL, trip(hour, 90)))
            self.assertEqual(body["course"][0]["role"], expected)

    def test_empty_short_trip_does_not_budget_for_absent_preferred_roles(self):
        for hour in (15, 20):
            with self.subTest(hour=hour), providers([place("meal", "MEAL")],
                                                  routing=lambda origin, destination, time: 10):
                # FOOD needs 90 minutes plus routing, so use a 2-hour window.
                body = self.assert_success(post_json(COURSE_URL, trip(hour, 120)))
            self.assertEqual([stop["contentId"] for stop in body["course"]], ["meal"])

    def test_high_time_fit_closed_meal_is_replaced(self):
        # FOOD receives a positive NIGHT signal even without a user theme, but
        # the actual visit window still rejects both arrival and departure gaps.
        for hours in ("10:00~19:00", "20:00~20:30"):
            with self.subTest(hours=hours), providers(
                [place("closed", "MEAL"), place("open", "MEAL", 2)], hours={"closed": hours},
            ) as calls:
                body = self.assert_success(post_json(COURSE_URL, trip(20, 120)))
            self.assertEqual([stop["contentId"] for stop in body["course"]], ["open"])
            self.assertEqual(calls["timeline"].call_count, 2)

    def test_candidate_selection_uses_estimated_later_visit_profile(self):
        cafe = base_course_place(place("c"))
        seaside = base_course_place(night_place("sea", 2))
        seaside["themes"] = {"SEA", "NIGHT_VIEW"}
        city = base_course_place(night_place("city", 3))
        city["themes"] = {"NIGHT_VIEW"}
        start = START_AT.replace(hour=17)
        choices = [seaside, city]
        at_start = sorted(choices, key=lambda p: place_ranking_key(
            p, {"NIGHT_VIEW"}, "NIGHT_VIEW", START, visit_at=start, transport_mode=TransportMode.WALK,
        ))
        self.assertEqual(at_start[0]["contentId"], "sea")
        course = generate_course(
            {"CAFE": [cafe], "NIGHT_VIEW": choices}, {"CAFE", "NIGHT_VIEW"}, START,
            role_plan=[("CAFE", 130), ("NIGHT_VIEW", 40)],
            start_at=start, transport_mode=TransportMode.WALK,
        )
        self.assertEqual([stop["contentId"] for stop in course], ["c", "city"])

    def test_fallback_ranks_replacements_at_actual_night_arrival(self):
        # Exercise planner ordering on enriched candidate records. A successful
        # timeline that later fails visit validation is supplied by the endpoint.
        first = {**base_course_place(night_place("first", 1)), "themes": {"SEA", "NIGHT_VIEW"}}
        sunset_choice = {**base_course_place(night_place("sea", 2)), "themes": {"SEA", "NIGHT_VIEW"}}
        night_choice = {**base_course_place(night_place("night", 3)), "themes": {"NIGHT_VIEW"}}
        candidates = {"NIGHT_VIEW": [first, sunset_choice, night_choice]}
        plan = [("NIGHT_VIEW", 40)]
        start = START_AT.replace(hour=17)
        course = generate_course(candidates, {"NIGHT_VIEW"}, START, role_plan=plan,
                                 start_at=start, transport_mode=TransportMode.PUBLIC_TRANSIT)
        self.assertEqual(course[0]["contentId"], "first")
        for hour, expected in ((18, "sea"), (19, "night")):
            with self.subTest(actual_arrival_hour=hour):
                search = CourseCandidateSearch(course, candidates, {"NIGHT_VIEW"}, START, plan,
                                               start_at=start, transport_mode=TransportMode.PUBLIC_TRANSIT)
                search.next_course()
                actual_arrival = start.replace(hour=hour, minute=10)
                timeline = [{**course[0], "arrivalAt": actual_arrival.isoformat(),
                             "departureAt": (actual_arrival + timedelta(minutes=40)).isoformat()}]
                search.retry(course, "PLACE_CLOSED_AT_VISIT_TIME", 0, timeline=timeline)
                self.assertEqual(search.next_course()[0]["contentId"], expected)

    def test_departure_sunset_and_actual_night_cafe_are_validated_at_arrival(self):
        with providers([place("c")], hours={"c": "19:00~23:00"},
                       routing=lambda origin, destination, time: 130 if origin == "home" else 10):
            body = self.assert_success(post_json(COURSE_URL, trip(17, 250, ("CAFE",))))
        self.assertEqual(resolve_time_profile(datetime.fromisoformat(body["course"][0]["arrivalAt"])), TimeProfile.NIGHT)

    def test_night_pool_limit_preserves_rare_required_theme(self):
        nature = base_course_place(place("nature", "ACTIVITY", 8))
        nature["themes"] = {"NATURE"}
        cultures = [base_course_place(place(f"museum{i}", "CULTURE", i)) for i in range(1, 7)]
        ranked = rank_course_candidates(
            {"ACTIVITY": cultures + [nature]}, {"CULTURE", "NATURE"}, START,
            start_at=START_AT.replace(hour=20), transport_mode=TransportMode.CAR,
            role_plan=[("ACTIVITY", 60)],
        )
        self.assertEqual(len(ranked["ACTIVITY"]), 5)
        self.assertIn("nature", [p["contentId"] for p in ranked["ACTIVITY"]])

    def test_night_search_limits_and_caches_remain_bounded(self):
        records = [place(f"a{i}", "ACTIVITY", i) for i in range(1, 7)] + [place(f"c{i}", rank=i) for i in range(1, 7)]
        with providers(records, routing=lambda origin, destination, time: None) as calls:
            self.assertEqual(post_json(COURSE_URL, trip(20, 300, ("SEA", "CAFE"))),
                             (422, {"detail": "NO_ROUTE"}))
        self.assertEqual(MAX_CANDIDATES_PER_ROLE, 5)
        self.assertEqual(MAX_COURSE_ATTEMPTS, 20)
        self.assertEqual(calls["timeline"].call_count, 20)
        self.assertEqual(len(calls["routes"]), 5)
        attempts = [tuple(stop["contentId"] for stop in call.args[0]) for call in calls["timeline"].call_args_list]
        self.assertEqual(len(attempts), len(set(attempts)))

    def test_night_cafe_utc_and_korea_inputs_have_equivalent_visits(self):
        local = trip(20, 120, ("CAFE",))
        utc = {**local, **{key: datetime.fromisoformat(local[key]).astimezone(timezone.utc).isoformat()
                          for key in ("startAt", "returnBy")}}
        with providers([place("c")], hours={"c": "20:00~23:00"}):
            local_body = self.assert_success(post_json(COURSE_URL, local))
            utc_body = self.assert_success(post_json(COURSE_URL, utc))
        self.assertEqual(local_body["course"][0]["contentId"], utc_body["course"][0]["contentId"])
        self.assertEqual(datetime.fromisoformat(local_body["course"][0]["arrivalAt"]),
                         datetime.fromisoformat(utc_body["course"][0]["arrivalAt"]))

    def test_course_logs_profile_and_returns_preview_fields(self):
        with providers([place("c")]), self.assertLogs("data.app", level="INFO") as logs:
            body = self.assert_success(post_json(COURSE_URL, trip(20, 120, ("CAFE",))))
        message = "\n".join(logs.output)
        self.assertIn("timeProfile=NIGHT", message)
        self.assertIn("stops=1", message)
        self.assertNotIn("test-key", message)
        self.assertEqual(set(body), {"destinationId", "name", "transportMode", "returnTravelMinutes",
                                     "estimatedReturnAt", "returnBy", "course", "previewId",
                                     "previewToken", "previewExpiresAt", "startLocation", "startAt",
                                     "finalTransit", "routeLines"})
        self.assertEqual(set(body["course"][0]), {"order", "role", "name", "contentId", "contentTypeId",
                                                 "latitude", "longitude", "travelMinutesFromPrevious",
                                                 "arrivalAt", "departureAt", "stayMinutes", "themes",
                                                 "place", "inboundTransit"})
