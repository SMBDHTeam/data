from dataclasses import replace
from datetime import timedelta, timezone
from random import Random
from unittest import TestCase
from unittest.mock import patch

from spontaneous.destinations import DESTINATION_ZONES
from spontaneous.models import TransportMode
from spontaneous.places import infer_place_themes
from spontaneous.service import (
    calculate_zone_theme_score,
    calculate_zone_time_bonus,
    select_weighted_destination_candidates,
)
from spontaneous.time_profile import TimeProfile
from tests.test_spontaneous_course_fallback import DESTINATIONS_URL, payload, post_json, providers
from tests.test_spontaneous_destination_selection import COURSE_PLACES, TOURAPI_PLACES_BY_ZONE
from tests.test_spontaneous_destination_routing_limit import START, START_AT


def real_places(themes, count=5):
    return [p for p in COURSE_PLACES if infer_place_themes(p) == set(themes)][:count]


class TimeAwareDestinationTest(TestCase):
    def recommend(self, hour, themes=(), mode="PUBLIC_TRANSIT", seed=42, zone_places=None, utc=False):
        request = payload(themes)
        del request["destinationId"]
        start = START_AT.replace(hour=hour)
        end = start + timedelta(hours=3)
        if utc:
            start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        request.update(startAt=start.isoformat(), returnBy=end.isoformat(), transportMode=mode)
        zones = DESTINATION_ZONES
        if zone_places is not None:
            # Equal locations isolate contextual ranking. Place data is replayed
            # unmodified from the existing TourAPI fixture at the provider boundary.
            zones = [replace(zone, center_latitude=START.latitude, center_longitude=START.longitude)
                     for zone in DESTINATION_ZONES[:len(zone_places)]]
            records = {zone.destination_id: places for zone, places in zip(zones, zone_places)}
        else:
            records = TOURAPI_PLACES_BY_ZONE
        with providers(COURSE_PLACES) as calls, patch("app.DESTINATION_ZONES", zones), patch(
            "app.search_places_by_zone", side_effect=lambda zone, places_cache=None: records[zone.destination_id],
        ), patch("spontaneous.service.random", Random(seed)):
            status, body = post_json(DESTINATIONS_URL, request)
        self.assertEqual(status, 200, body)
        return body, calls, records

    def test_same_location_explicit_cafe_survives_afternoon_and_night(self):
        for hour in (15, 20):
            with self.subTest(hour=hour):
                body, calls, records = self.recommend(hour, ("CAFE",))
                self.assertTrue(body["destinations"])
                for item in body["destinations"]:
                    self.assertEqual(item["themeScore"], round(calculate_zone_theme_score(records[item["destinationId"]], ["CAFE"]), 4))
                    self.assertGreater(item["themeScore"], 0)
                self.assertLessEqual(len(calls["routes"]), 4)

    def test_empty_themes_switch_leader_using_start_at(self):
        groups = [real_places({"CAFE"}), real_places({"NIGHT_VIEW", "WALK"}), real_places({"CULTURE"})]
        afternoon, _, _ = self.recommend(15, zone_places=groups)
        night, _, _ = self.recommend(20, zone_places=groups)
        self.assertEqual(afternoon["destinations"][0]["destinationId"], DESTINATION_ZONES[0].destination_id)
        self.assertEqual(night["destinations"][0]["destinationId"], DESTINATION_ZONES[1].destination_id)
        self.assertTrue(all(item["themeScore"] == 0 for item in afternoon["destinations"] + night["destinations"]))

    def test_real_place_time_evidence_favors_expected_groups(self):
        def score(themes, profile):
            records = real_places(themes)
            self.assertTrue(records)
            return calculate_zone_time_bonus(records, [], profile, TransportMode.WALK)

        for themes in ({"CAFE"}, {"SEA", "WALK"}, {"WALK"}):
            self.assertGreater(score(themes, TimeProfile.AFTERNOON), score({"FOOD"}, TimeProfile.AFTERNOON))
        for themes in ({"NIGHT_VIEW", "WALK"}, {"SEA", "WALK"}, {"FOOD"}):
            self.assertGreater(score(themes, TimeProfile.NIGHT), score({"CAFE"}, TimeProfile.NIGHT))

    def test_user_theme_strength_precedes_night_scenery(self):
        cafes = real_places({"CAFE"})
        groups = [cafes, cafes[:1] + real_places({"NIGHT_VIEW", "WALK"})]
        body, _, _ = self.recommend(20, ("CAFE",), zone_places=groups)
        self.assertEqual(body["destinations"][0]["destinationId"], DESTINATION_ZONES[0].destination_id)
        self.assertEqual(body["destinations"][0]["themeScore"], 1.0)

    def test_equivalent_utc_request_has_same_ranking_and_scores(self):
        local, _, _ = self.recommend(20, ("CAFE",))
        utc, _, _ = self.recommend(20, ("CAFE",), utc=True)
        self.assertEqual(local, utc)

    def test_same_seed_reproduces_time_aware_destination_selection(self):
        first, _, _ = self.recommend(20, seed=123)
        second, _, _ = self.recommend(20, seed=123)
        self.assertEqual(first, second)

    def test_weighted_selection_preserves_leader_and_squared_bonus_scores(self):
        candidates = [{"zone": zone, "score": score} for zone, score in
                      zip(DESTINATION_ZONES, (0.5, 0.3, -0.1))]
        rng = Random(4)
        with patch.object(rng, "choices", wraps=rng.choices) as choices:
            selected = select_weighted_destination_candidates(candidates, 2, rng)
        self.assertIs(selected[0], candidates[0])
        self.assertEqual(choices.call_args.kwargs["weights"], [0.3 ** 2, 0.01 ** 2])

    def test_night_public_transit_keeps_two_roundtrips_four_provider_calls(self):
        body, calls, _ = self.recommend(20)
        self.assertEqual(len(body["destinations"]), 2)
        self.assertEqual(len(calls["routes"]), 4)
        self.assertEqual(calls["timeline"].call_count, 0)
        self.assertEqual(calls["details"], [])

    def test_destination_response_has_no_new_fields(self):
        body, _, _ = self.recommend(20)
        self.assertEqual(set(body), {"destinations"})
        for item in body["destinations"]:
            self.assertEqual(set(item), {"destinationId", "name", "themeScore", "distanceMeters", "transport"})
            self.assertEqual(set(item["transport"]), {"mode", "outboundMinutes", "returnMinutes", "availableStayMinutes"})

    def test_destination_logs_context_without_payload_or_keys(self):
        with self.assertLogs("data.app", level="INFO") as logs:
            self.recommend(20, ("CAFE",))
        message = "\n".join(logs.output)
        for expected in ("startAt=", "resolvedTimeProfile=NIGHT", "transportMode=PUBLIC_TRANSIT", "desiredThemes=['CAFE']"):
            self.assertIn(expected, message)
        for forbidden in ("test-key", str(START.latitude), str(START.longitude), "startLocation"):
            self.assertNotIn(forbidden, message)

    def test_naive_input_is_rejected_without_assuming_timezone(self):
        request = payload(())
        del request["destinationId"]
        request.update(startAt="2026-09-10T20:00:00", returnBy="2026-09-10T23:00:00")
        with providers(COURSE_PLACES) as calls, self.assertLogs("data.app", level="INFO") as logs:
            status, body = post_json(DESTINATIONS_URL, request)
        self.assertEqual((status, body), (422, {"detail": "INVALID_TIME_RANGE"}))
        self.assertIn("failureReason=SPONTANEOUS_TIMEZONE_REQUIRED", "\n".join(logs.output))
        self.assertEqual(calls["routes"], [])
        self.assertEqual(calls["details"], [])
