from datetime import datetime, timedelta, timezone
from unittest import TestCase

from spontaneous.course import (
    build_course_role_plan,
    course_stop_range,
    generate_course,
    place_ranking_key,
)
from spontaneous.models import TransportMode
from spontaneous.places import base_course_place
from spontaneous.planner import CourseCandidateSearch, MAX_CANDIDATES_PER_ROLE, MAX_COURSE_ATTEMPTS, rank_course_candidates
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
            body = self.assert_success(post_json(COURSE_URL, trip(15, minutes)), minimum, maximum)
        self.assertEqual(calls["timeline"].call_count, 1)
        self.assertEqual(len(calls["routes"]), len(body["course"]) + 1)
        self.assertEqual(course_stop_range(minutes), (minimum, maximum))

    def test_ninety_minutes_has_at_most_two_stops(self):
        self.check_duration(90, 1, 2)

    def test_three_hours_targets_two_to_three_stops(self):
        self.check_duration(180, 2, 3)

    def test_five_hours_targets_three_to_four_stops(self):
        self.check_duration(300, 3, 4)

    def test_seven_hours_targets_four_to_five_stops(self):
        self.check_duration(420, 4, 5)

    def test_stop_policy_exact_boundaries(self):
        for minutes, expected in ((119, (1, 2)), (120, (2, 3)), (239, (2, 3)),
                                  (240, (3, 4)), (359, (3, 4)), (360, (4, 5))):
            self.assertEqual(course_stop_range(minutes), expected)

    def test_seven_hours_can_cover_five_stops_when_activity_themes_need_two(self):
        records = varied_places() + [place("museum", "CULTURE", 5)]
        with providers(records):
            body = self.assert_success(post_json(COURSE_URL, trip(15, 420, ("SEA", "CULTURE", "FOOD", "CAFE", "NIGHT_VIEW"))), 5, 5)
        self.assertTrue({"SEA", "CULTURE", "FOOD", "CAFE", "NIGHT_VIEW"}.issubset(
            {theme for stop in body["course"] for theme in stop["themes"]},
        ))

    def test_short_trip_does_not_silently_drop_required_themes_to_meet_cap(self):
        with providers(varied_places()) as calls:
            self.assertEqual(post_json(COURSE_URL, trip(20, 90, ("SEA", "CAFE", "FOOD", "NIGHT_VIEW"))),
                             (422, {"detail": "COURSE_NOT_FEASIBLE"}))
        self.assertEqual(calls["routes"], [])

    def test_short_trip_can_trim_optional_stops_without_losing_requested_coverage(self):
        with providers(varied_places()):
            body = self.assert_success(post_json(COURSE_URL, trip(20, 90, ("CAFE",))), 1, 2)
        self.assertIn("CAFE", {theme for stop in body["course"] for theme in stop["themes"]})

    def test_cafe_course_returns_200_in_afternoon_and_night_when_open(self):
        for hour in (15, 20):
            with self.subTest(hour=hour), providers([place("c")], hours={"c": "14:00~23:59"}) as calls:
                body = self.assert_success(post_json(COURSE_URL, trip(hour, 180, ("CAFE",))))
                self.assertEqual([stop["contentId"] for stop in body["course"]], ["c"])
                self.assertEqual(calls["details"], ["c"])
                self.assertEqual(len(calls["routes"]), 2)

    def test_late_night_cafe_supports_overnight_opening_hours(self):
        with providers([place("c")], hours={"c": "18:00~02:00"}):
            body = self.assert_success(post_json(COURSE_URL, trip(23, 180, ("CAFE",))))
        self.assertIn("CAFE", body["course"][0]["themes"])

    def test_night_cafe_fallback_rejects_closed_arrival_or_departure(self):
        cafe = place("closed")
        for hours in ("10:00~19:00", "20:00~20:30"):
            with self.subTest(hours=hours), providers([cafe, place("open", rank=2)], hours={"closed": hours}) as calls:
                body = self.assert_success(post_json(COURSE_URL, trip(20, 180, ("CAFE",))))
                self.assertEqual([stop["contentId"] for stop in body["course"]], ["open"])
                self.assertEqual(calls["timeline"].call_count, 2)

    def test_return_by_is_enforced_after_real_routing_for_night_cafe(self):
        with providers([place("c")], routing=lambda origin, destination, time: 20):
            self.assertEqual(post_json(COURSE_URL, trip(20, 90, ("CAFE",))),
                             (422, {"detail": "COURSE_NOT_FEASIBLE"}))

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
                body = self.assert_success(post_json(COURSE_URL, trip(20, 180)))
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
            body = self.assert_success(post_json(COURSE_URL, trip(17, 420, ("CAFE",))))
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
                             (422, {"detail": "COURSE_NOT_FEASIBLE"}))
        self.assertEqual(MAX_CANDIDATES_PER_ROLE, 5)
        self.assertEqual(MAX_COURSE_ATTEMPTS, 20)
        self.assertEqual(calls["timeline"].call_count, 20)
        self.assertEqual(len(calls["routes"]), 5)
        attempts = [tuple(stop["contentId"] for stop in call.args[0]) for call in calls["timeline"].call_args_list]
        self.assertEqual(len(attempts), len(set(attempts)))

    def test_night_cafe_utc_and_korea_inputs_have_equivalent_visits(self):
        local = trip(20, 180, ("CAFE",))
        utc = {**local, **{key: datetime.fromisoformat(local[key]).astimezone(timezone.utc).isoformat()
                          for key in ("startAt", "returnBy")}}
        with providers([place("c")], hours={"c": "20:00~23:00"}):
            local_body = self.assert_success(post_json(COURSE_URL, local))
            utc_body = self.assert_success(post_json(COURSE_URL, utc))
        self.assertEqual(local_body["course"][0]["contentId"], utc_body["course"][0]["contentId"])
        self.assertEqual(datetime.fromisoformat(local_body["course"][0]["arrivalAt"]),
                         datetime.fromisoformat(utc_body["course"][0]["arrivalAt"]))

    def test_course_logs_profile_without_new_response_fields(self):
        with providers([place("c")]), self.assertLogs("data.app", level="INFO") as logs:
            body = self.assert_success(post_json(COURSE_URL, trip(20, 180, ("CAFE",))))
        message = "\n".join(logs.output)
        self.assertIn("timeProfile=NIGHT", message)
        self.assertIn("stops=1", message)
        self.assertNotIn("test-key", message)
        self.assertEqual(set(body), {"destinationId", "name", "transportMode", "returnTravelMinutes",
                                     "estimatedReturnAt", "returnBy", "course"})
        self.assertEqual(set(body["course"][0]), {"order", "role", "name", "contentId", "contentTypeId",
                                                 "latitude", "longitude", "travelMinutesFromPrevious",
                                                 "arrivalAt", "departureAt", "stayMinutes", "themes"})
