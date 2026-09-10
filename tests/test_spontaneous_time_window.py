from datetime import datetime, timedelta, timezone, tzinfo
from unittest import TestCase
from unittest.mock import patch

import app as data_app
from spontaneous.models import SpontaneousCourseRequest, SpontaneousDestinationRequest, TransportMode
from spontaneous.time_profile import TimeProfile, calculate_time_fit_bonus, resolve_time_profile
from spontaneous.time_window import (
    KOREA_TIMEZONE,
    SpontaneousTimeWindowError,
    validate_spontaneous_time_window,
)
from tests.test_spontaneous_course_fallback import (
    COURSE_URL, DESTINATIONS_URL, payload, place, post_json, providers,
)
from tests.test_spontaneous_destination_routing_limit import START


NOW = datetime.fromisoformat("2026-09-10T20:00:00+09:00")


def at(clock, day=10):
    return datetime.fromisoformat(f"2026-09-{day:02d}T{clock}+09:00")


class SpontaneousTimeWindowTest(TestCase):
    def validate(self, start, end, now=NOW):
        self.assertIsNone(validate_spontaneous_time_window(start, end, now=now))

    def reject(self, start, end, reason, now=NOW):
        with self.assertRaises(SpontaneousTimeWindowError) as error:
            validate_spontaneous_time_window(start, end, now=now)
        self.assertEqual(error.exception.failure_reason, reason)

    def test_depart_now_and_return_same_day(self):
        self.validate(at("20:00"), at("23:00"))

    def test_four_minutes_in_past_is_allowed(self):
        self.validate(at("19:56"), at("23:00"))

    def test_exactly_five_minutes_in_past_is_allowed(self):
        self.validate(at("19:55"), at("23:00"))
        self.validate(at("20:00"), at("23:00"), now=at("20:05"))

    def test_six_minutes_in_past_is_rejected(self):
        self.reject(at("19:54"), at("23:00"), "SPONTANEOUS_START_TIME_IN_PAST")

    def test_past_tolerance_preserves_seconds_and_microseconds(self):
        self.reject(at("19:54:59.999999"), at("23:00"), "SPONTANEOUS_START_TIME_IN_PAST")
        precise_now = NOW.replace(microsecond=123456)
        boundary = precise_now - timedelta(minutes=5)
        self.validate(boundary, at("23:00"), now=precise_now)
        self.reject(boundary - timedelta(microseconds=1), at("23:00"),
                    "SPONTANEOUS_START_TIME_IN_PAST", now=precise_now)

    def test_evening_departure_and_next_day_one_am_return(self):
        self.validate(at("22:30"), at("01:00", 11))

    def test_next_day_three_am_is_inclusive(self):
        self.validate(at("23:30"), at("03:00", 11))

    def test_one_second_or_microsecond_after_three_am_is_rejected(self):
        for end in (at("03:00:01", 11), at("03:00:00.000001", 11), at("09:00", 11)):
            with self.subTest(end=end):
                self.reject(at("23:30"), end, "SPONTANEOUS_RETURN_TIME_TOO_LATE")

    def test_return_limit_does_not_inherit_departure_seconds(self):
        self.reject(at("23:30:45.123456"), at("03:00:01", 11), "SPONTANEOUS_RETURN_TIME_TOO_LATE")

    def test_next_day_half_past_midnight_departure_is_rejected(self):
        self.reject(at("00:30", 11), at("02:30", 11), "SPONTANEOUS_START_DATE_NOT_TODAY")

    def test_next_day_nine_am_departure_is_rejected(self):
        self.reject(at("09:00", 11), at("12:00", 11), "SPONTANEOUS_START_DATE_NOT_TODAY")

    def test_return_must_be_strictly_later_than_departure(self):
        for end in (NOW, NOW - timedelta(seconds=1), NOW.astimezone(timezone.utc)):
            with self.subTest(end=end):
                self.reject(NOW, end, "INVALID_TIME_RANGE")

    def test_future_evening_start_uses_night_not_current_afternoon(self):
        self.validate(at("19:00"), at("23:00"), now=at("15:00"))
        self.assertEqual(resolve_time_profile(at("19:00")), TimeProfile.NIGHT)

    def test_future_ten_pm_start_uses_late_night(self):
        self.validate(at("22:00"), at("01:00", 11))
        self.assertEqual(resolve_time_profile(at("22:00")), TimeProfile.LATE_NIGHT)

    def test_utc_input_on_previous_utc_date_is_valid_when_kst_today(self):
        start = datetime.fromisoformat("2026-09-09T16:10:00Z")
        self.validate(start, datetime.fromisoformat("2026-09-09T18:00:00Z"), now=at("01:00"))

    def test_utc_input_on_same_utc_date_is_rejected_when_kst_tomorrow(self):
        self.reject(datetime.fromisoformat("2026-09-10T15:30:00Z"),
                    datetime.fromisoformat("2026-09-10T17:30:00Z"), "SPONTANEOUS_START_DATE_NOT_TODAY")

    def test_equivalent_non_kst_offsets_for_start_return_and_now(self):
        for offset in (-10, 0, 5.5, 9, 14):
            zone = timezone(timedelta(hours=offset))
            with self.subTest(offset=offset):
                self.validate(at("23:30").astimezone(zone), at("03:00", 11).astimezone(zone),
                              now=NOW.astimezone(zone))

    def test_one_am_same_day_departure_is_allowed(self):
        self.validate(at("01:10"), at("03:00"), now=at("01:00"))

    def test_previous_day_within_tolerance_is_still_rejected(self):
        self.reject(at("23:59", 9), at("02:00"), "SPONTANEOUS_START_DATE_NOT_TODAY", now=at("00:02"))

    def test_timezone_is_required_for_start_return_and_injected_now(self):
        values = [NOW, at("23:00"), NOW]
        for index in range(3):
            changed = list(values)
            changed[index] = changed[index].replace(tzinfo=None)
            with self.subTest(naive_index=index):
                self.reject(changed[0], changed[1], "SPONTANEOUS_TIMEZONE_REQUIRED", now=changed[2])
        self.reject(NOW.replace(tzinfo=None), at("23:00").replace(tzinfo=None), "SPONTANEOUS_TIMEZONE_REQUIRED")

    def test_tzinfo_with_no_offset_is_also_rejected(self):
        class MissingOffset(tzinfo):
            def utcoffset(self, value):
                return None

        self.reject(NOW.replace(tzinfo=MissingOffset()), at("23:00"), "SPONTANEOUS_TIMEZONE_REQUIRED")

    def test_next_day_limit_handles_month_year_and_leap_day(self):
        for date in ("2026-09-30", "2026-12-31", "2028-02-29"):
            start = datetime.fromisoformat(date + "T23:30:00+09:00")
            end = (start + timedelta(days=1)).replace(hour=3, minute=0)
            with self.subTest(date=date):
                self.validate(start, end, now=start)
                self.reject(start, end + timedelta(seconds=1), "SPONTANEOUS_RETURN_TIME_TOO_LATE", now=start)

    def test_explicit_now_does_not_read_the_system_clock(self):
        with patch("spontaneous.time_window.current_kst_time", side_effect=AssertionError("unexpected clock read")):
            self.validate(NOW, at("23:00"))

    def test_default_clock_is_read_once_per_validation(self):
        with patch("spontaneous.time_window.current_kst_time", return_value=NOW) as clock:
            validate_spontaneous_time_window(NOW, at("23:00"))
        clock.assert_called_once_with()

    def test_next_day_visit_profile_does_not_reapply_departure_validation(self):
        arrival = at("00:30", 11)
        self.assertEqual(resolve_time_profile(arrival), TimeProfile.LATE_NIGHT)
        self.assertGreater(calculate_time_fit_bonus({"NIGHT_VIEW"}, [], resolve_time_profile(arrival), TransportMode.CAR), 0)


class SpontaneousTimeWindowEndpointTest(TestCase):
    def post(self, path, start, end, now=NOW, routing=None):
        request = payload(("CAFE",))
        request.update(startAt=start.isoformat(), returnBy=end.isoformat())
        if path == DESTINATIONS_URL:
            del request["destinationId"]
        with providers([place("c")], hours={"c": "00:00~03:00" if start.hour < 3 else "14:00~03:00"},
                       routing=routing, now=now) as calls, patch(
            "app.search_places_by_zone", wraps=data_app.search_places_by_zone,
        ) as places, self.assertLogs("data.app", level="INFO") as logs:
            status, body = post_json(path, request)
        return status, body, calls, places.call_count, "\n".join(logs.output)

    def test_both_endpoints_have_identical_time_validation_results(self):
        cases = (
            (NOW, at("23:00"), True),
            (at("19:56"), at("23:00"), True),
            (at("19:55"), at("23:00"), True),
            (at("19:54"), at("23:00"), False),
            (at("22:30"), at("01:00", 11), True),
            (at("23:30"), at("03:00", 11), True),
            (at("23:30"), at("03:00:01", 11), False),
            (at("00:30", 11), at("02:30", 11), False),
            (at("09:00", 11), at("12:00", 11), False),
            (NOW, NOW, False),
            (NOW, NOW - timedelta(seconds=1), False),
            (NOW.replace(tzinfo=None), at("23:00"), False),
            (NOW, at("23:00").replace(tzinfo=None), False),
            (NOW.replace(tzinfo=None), at("23:00").replace(tzinfo=None), False),
        )
        for start, end, valid in cases:
            responses = []
            for path in (DESTINATIONS_URL, COURSE_URL):
                with self.subTest(path=path, start=start, end=end):
                    status, body, calls, place_calls, _ = self.post(path, start, end)
                    responses.append(status)
                    self.assertEqual(status, 200 if valid else 422, body)
                    if not valid:
                        self.assertEqual(body, {"detail": "INVALID_TIME_RANGE"})
                        self.assertEqual(place_calls, 0)
                        self.assertEqual(calls["routes"], [])
                        self.assertEqual(calls["details"], [])
                        self.assertEqual(calls["timeline"].call_count, 0)
            self.assertEqual(responses[0], responses[1])

    def test_future_start_determines_destination_and_course_profile(self):
        for now, start, expected in ((at("15:00"), at("19:00"), "NIGHT"),
                                     (NOW, at("22:00"), "LATE_NIGHT")):
            for path in (DESTINATIONS_URL, COURSE_URL):
                with self.subTest(path=path, expected=expected):
                    status, body, calls, _, message = self.post(path, start, at("03:00", 11), now=now)
                    self.assertEqual(status, 200, body)
                    self.assertIn(("resolvedTimeProfile=" if path == DESTINATIONS_URL else "timeProfile=") + expected, message)
                    self.assertEqual(calls["routes"][0][-1], start.isoformat())

    def test_tolerance_does_not_clamp_start_to_now_for_routing(self):
        start = at("19:56")
        for path in (DESTINATIONS_URL, COURSE_URL):
            status, body, calls, _, _ = self.post(path, start, at("23:00"))
            self.assertEqual(status, 200, body)
            self.assertEqual(calls["routes"][0][-1], start.isoformat())

    def test_midnight_arrival_succeeds_and_resolves_late_night(self):
        status, body, calls, _, _ = self.post(
            COURSE_URL, at("23:30"), at("03:00", 11),
            routing=lambda origin, destination, time: 60 if origin == "home" else 10,
        )
        self.assertEqual(status, 200, body)
        arrival = datetime.fromisoformat(body["course"][0]["arrivalAt"])
        self.assertEqual(arrival, at("00:30", 11))
        self.assertEqual(resolve_time_profile(arrival), TimeProfile.LATE_NIGHT)
        self.assertLessEqual(datetime.fromisoformat(body["estimatedReturnAt"]), at("03:00", 11))
        self.assertEqual(calls["timeline"].call_count, 1)
        self.assertEqual(calls["details"], ["c"])

    def test_same_day_early_morning_is_allowed_on_both_endpoints(self):
        for path in (DESTINATIONS_URL, COURSE_URL):
            status, body, _, _, _ = self.post(path, at("01:10"), at("03:00"), now=at("01:00"))
            self.assertEqual(status, 200, body)

    def test_utc_today_is_allowed_and_utc_tomorrow_is_rejected(self):
        for path in (DESTINATIONS_URL, COURSE_URL):
            start, end = at("20:00").astimezone(timezone.utc), at("23:00").astimezone(timezone.utc)
            status, body, _, _, _ = self.post(path, start, end)
            self.assertEqual(status, 200, body)
            tomorrow = at("00:30", 11).astimezone(timezone.utc)
            status, body, calls, _, _ = self.post(path, tomorrow, tomorrow + timedelta(hours=2))
            self.assertEqual((status, body), (422, {"detail": "INVALID_TIME_RANGE"}))
            self.assertEqual(calls["routes"], [])

    def test_naive_and_aware_json_still_have_unchanged_pydantic_field_schema(self):
        # Parsing/schema stay unchanged; the shared endpoint helper enforces
        # the new policy even on already-constructed model instances.
        for model in (SpontaneousCourseRequest, SpontaneousDestinationRequest):
            schema = model.model_json_schema()
            expected = {"startLocation", "startAt", "returnBy", "transportMode", "desiredThemes"}
            if model is SpontaneousCourseRequest:
                expected.add("destinationId")
            self.assertEqual(set(schema["properties"]), expected)
            self.assertEqual(schema["properties"]["startAt"]["format"], "date-time")
            request = payload(("CAFE",))
            request.update(startAt="2026-09-10T20:00:00", returnBy="2026-09-10T23:00:00")
            parsed = model.model_validate(request)
            self.assertIsNone(parsed.startAt.tzinfo)

    def test_internal_reasons_are_logged_but_public_detail_stays_compatible(self):
        cases = (
            (at("19:54"), at("23:00"), "SPONTANEOUS_START_TIME_IN_PAST"),
            (at("00:30", 11), at("02:30", 11), "SPONTANEOUS_START_DATE_NOT_TODAY"),
            (at("23:30"), at("03:00:01", 11), "SPONTANEOUS_RETURN_TIME_TOO_LATE"),
            (NOW.replace(tzinfo=None), at("23:00"), "SPONTANEOUS_TIMEZONE_REQUIRED"),
            (NOW, NOW, "INVALID_TIME_RANGE"),
        )
        for start, end, reason in cases:
            for path in (DESTINATIONS_URL, COURSE_URL):
                with self.subTest(path=path, reason=reason):
                    status, body, _, _, message = self.post(path, start, end)
                    self.assertEqual((status, body), (422, {"detail": "INVALID_TIME_RANGE"}))
                    self.assertIn("failureReason=" + reason, message)
                    for forbidden in ("test-key", str(START.latitude), str(START.longitude), "startLocation", "desiredThemes"):
                        self.assertNotIn(forbidden, message)

    def test_clock_is_sampled_once_even_when_provider_work_crosses_tolerance(self):
        for path in (DESTINATIONS_URL, COURSE_URL):
            request = payload(("CAFE",))
            request.update(startAt=NOW.isoformat(), returnBy=at("23:00").isoformat())
            with providers([place("c")], now=NOW), patch(
                "spontaneous.time_window.current_kst_time", side_effect=[NOW, NOW + timedelta(minutes=6)],
            ) as clock:
                status, body = post_json(path, request)
            self.assertEqual(status, 200, body)
            clock.assert_called_once_with()

    def test_supplied_utc_start_and_return_offsets_remain_in_course_response(self):
        start = NOW.astimezone(timezone.utc)
        end = at("23:00").astimezone(timezone.utc)
        status, body, _, _, _ = self.post(COURSE_URL, start, end)
        self.assertEqual(status, 200, body)
        self.assertEqual(datetime.fromisoformat(body["returnBy"]).utcoffset(), timedelta(0))
        self.assertEqual(datetime.fromisoformat(body["course"][0]["arrivalAt"]).utcoffset(), timedelta(0))
