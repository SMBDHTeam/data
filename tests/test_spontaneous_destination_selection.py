import json
from pathlib import Path
from random import Random
from unittest import TestCase, main
from unittest.mock import patch

import app as data_app
from spontaneous.destinations import DESTINATION_ZONES
from spontaneous.models import TransportMode, TravelTheme
from spontaneous.places import filter_course_candidates, infer_place_themes
from spontaneous.service import (
    DESTINATION_SELECTION_POOL_LIMIT,
    calculate_zone_theme_score,
    has_coarse_course_viability,
    select_weighted_destination_candidates,
)
from tests.test_spontaneous_destination_routing_limit import request, successful_transport


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tourapi_destination_places.json"
TOURAPI_PLACES_BY_ZONE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["zones"]
# Use distinct, unmodified provider records even when testing controlled ratios.
TOURAPI_PLACES = list({
    place["contentid"]: place
    for places in TOURAPI_PLACES_BY_ZONE.values()
    for place in places
}.values())
COURSE_PLACES = filter_course_candidates(TOURAPI_PLACES)
CAFE_PLACES = [place for place in COURSE_PLACES if "CAFE" in infer_place_themes(place)]
CULTURE_PLACES = [place for place in COURSE_PLACES if "CULTURE" in infer_place_themes(place)]
OTHER_PLACES = [
    place for place in COURSE_PLACES
    if not {"CAFE", "CULTURE"}.intersection(infer_place_themes(place))
]


class DestinationThemeStrengthTest(TestCase):
    def test_more_cafe_coverage_and_volume_produce_stronger_score(self):
        strong = CAFE_PLACES[:8] + OTHER_PLACES[:2]
        weak = CAFE_PLACES[:1] + OTHER_PLACES[:9]
        strong_score = calculate_zone_theme_score(strong, ["CAFE"])
        weak_score = calculate_zone_theme_score(weak, ["CAFE"])

        self.assertAlmostEqual(strong_score, 0.86)
        self.assertAlmostEqual(weak_score, 0.13)
        self.assertGreater(strong_score, weak_score)

    def test_one_cafe_among_twenty_is_not_full_strength(self):
        places = CAFE_PLACES[:1] + OTHER_PLACES[:19]
        score = calculate_zone_theme_score(places, ["CAFE"])

        self.assertAlmostEqual(score, 0.095)
        self.assertLess(score, 1.0)

    def test_multiple_themes_average_each_strength(self):
        places = CAFE_PLACES[:4] + CULTURE_PLACES[:2] + OTHER_PLACES[:4]
        # CAFE: 0.4 * 0.7 + 0.8 * 0.3 = 0.52
        # CULTURE: 0.2 * 0.7 + 0.4 * 0.3 = 0.26
        self.assertAlmostEqual(calculate_zone_theme_score(places, ["CAFE", "CULTURE"]), 0.39)

    def test_missing_requested_theme_contributes_zero_to_average(self):
        places = CAFE_PLACES[:4] + OTHER_PLACES[:6]
        self.assertAlmostEqual(calculate_zone_theme_score(places, ["CAFE", "CULTURE"]), 0.26)
        self.assertEqual(calculate_zone_theme_score(places, ["CULTURE"]), 0.0)

    def test_no_desired_themes_or_no_candidates_score_zero(self):
        self.assertEqual(calculate_zone_theme_score(COURSE_PLACES, []), 0.0)
        self.assertEqual(calculate_zone_theme_score([], ["CAFE"]), 0.0)

    def test_non_course_places_do_not_dilute_strength(self):
        candidate_ids = {place["contentid"] for place in COURSE_PLACES}
        excluded = [place for place in TOURAPI_PLACES if place["contentid"] not in candidate_ids]
        self.assertTrue(excluded)
        self.assertEqual(calculate_zone_theme_score(excluded, ["CAFE"]), 0.0)
        self.assertAlmostEqual(calculate_zone_theme_score(CAFE_PLACES[:5] + excluded, ["CAFE"]), 1.0)

    def test_theme_case_order_and_duplicates_preserve_score(self):
        places = CAFE_PLACES[:4] + CULTURE_PLACES[:2] + OTHER_PLACES[:4]
        self.assertAlmostEqual(calculate_zone_theme_score(places, ["culture", "CAFE", "cafe"]), 0.39)

    def test_volume_saturates_at_five_and_scores_stay_bounded(self):
        self.assertAlmostEqual(calculate_zone_theme_score(CAFE_PLACES[:1], ["CAFE"]), 0.76)
        self.assertEqual(calculate_zone_theme_score(CAFE_PLACES[:5], ["CAFE"]), 1.0)
        self.assertEqual(calculate_zone_theme_score(CAFE_PLACES[:8], ["CAFE"]), 1.0)
        for places in TOURAPI_PLACES_BY_ZONE.values():
            for theme in TravelTheme:
                score = calculate_zone_theme_score(places, [theme])
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)

    def test_actual_zone_theme_strengths_differ(self):
        cafe_scores = {
            zone_id: calculate_zone_theme_score(places, ["CAFE"])
            for zone_id, places in TOURAPI_PLACES_BY_ZONE.items()
        }
        self.assertGreater(cafe_scores["BUSAN_SONGJEONG"], cafe_scores["BUSAN_NAMPO"])
        self.assertGreater(len(set(cafe_scores.values())), 2)


class WeightedDestinationSelectionTest(TestCase):
    def setUp(self):
        self.candidates = [
            {"zone": zone, "score": 1.0 - index * 0.1}
            for index, zone in enumerate(DESTINATION_ZONES)
        ]

    def selected_ids(self, seed, limit=5, candidates=None):
        return [
            item["zone"].destination_id
            for item in select_weighted_destination_candidates(
                self.candidates if candidates is None else candidates, limit, rng=Random(seed)
            )
        ]

    def test_sampling_has_no_duplicate_destination_ids(self):
        candidates = [self.candidates[0], dict(self.candidates[0]), *self.candidates[1:]]
        for seed in range(30):
            ids = self.selected_ids(seed, candidates=candidates)
            self.assertEqual(len(ids), 5)
            self.assertEqual(len(ids), len(set(ids)))

    def test_pre_ranked_leader_is_always_selected_first(self):
        for seed in range(30):
            for limit in (1, 2, 5):
                ids = self.selected_ids(seed, limit=limit)
                self.assertEqual(ids[0], self.candidates[0]["zone"].destination_id)

    def test_identical_seeds_produce_identical_selection(self):
        for seed in (0, 1, 42, 2026):
            self.assertEqual(self.selected_ids(seed), self.selected_ids(seed))

    def test_different_seeds_vary_membership(self):
        selections = {frozenset(self.selected_ids(seed)) for seed in range(30)}
        self.assertGreater(len(selections), 1)

    def test_sampling_never_reaches_beyond_top_six(self):
        allowed = {item["zone"].destination_id for item in self.candidates[:6]}
        for seed in range(30):
            self.assertTrue(set(self.selected_ids(seed)).issubset(allowed))
        self.assertEqual(len(self.selected_ids(0, limit=100)), DESTINATION_SELECTION_POOL_LIMIT)

    def test_cubed_weights_favor_higher_scores(self):
        candidates = [
            {"zone": DESTINATION_ZONES[0], "score": 1.0},
            {"zone": DESTINATION_ZONES[1], "score": 0.8},
            {"zone": DESTINATION_ZONES[2], "score": 0.4},
        ]
        high_id = DESTINATION_ZONES[1].destination_id
        high_count = sum(
            self.selected_ids(seed, limit=2, candidates=candidates)[1] == high_id
            for seed in range(1000)
        )
        # Cubing yields an 8:1 ratio, rather than the 2:1 ratio of linear weights.
        self.assertGreater(high_count, 850)
        self.assertLess(high_count, 930)

    def test_zero_scores_still_allow_sampling(self):
        candidates = [{**candidate, "score": 0.0} for candidate in self.candidates]
        ids = self.selected_ids(0, candidates=candidates)
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

    def test_empty_small_pools_and_nonpositive_limits(self):
        self.assertEqual(self.selected_ids(0, candidates=[]), [])
        self.assertEqual(self.selected_ids(0, limit=0), [])
        self.assertEqual(self.selected_ids(0, limit=-1), [])
        self.assertEqual(len(self.selected_ids(0, candidates=self.candidates[:1])), 1)
        self.assertEqual(len(self.selected_ids(0, candidates=self.candidates[:3])), 3)

    def test_sampling_does_not_modify_input(self):
        original = [dict(candidate) for candidate in self.candidates]
        self.selected_ids(42)
        self.assertEqual(self.candidates, original)


class TourApiDestinationRecommendationTest(TestCase):
    def recommend(self, themes, seed=42):
        payload = request(TransportMode.PUBLIC_TRANSIT)
        payload.desiredThemes = [TravelTheme(theme) for theme in themes]
        # Replay real TourAPI records at the network boundary; filtering, inference,
        # viability, scoring and weighted selection all run unchanged.
        def replay_places(zone, places_cache=None):
            return TOURAPI_PLACES_BY_ZONE[zone.destination_id]

        with (
            patch("app.search_places_by_zone", side_effect=replay_places),
            patch("spontaneous.service.random", Random(seed)),
            patch(
                "app.get_transport_option",
                return_value=successful_transport(payload.transportMode),
            ) as transport,
        ):
            response = data_app.recommend_spontaneous_destinations(payload)
        return response, transport.call_args_list

    def test_changing_only_theme_changes_leading_zone(self):
        cafe, cafe_calls = self.recommend(["CAFE"])
        culture, culture_calls = self.recommend(["CULTURE"])

        self.assertEqual(cafe.destinations[0].destinationId, "BUSAN_SONGJEONG")
        self.assertEqual(culture.destinations[0].destinationId, "BUSAN_DONGNAE")
        self.assertNotEqual(cafe_calls[0].args[1], culture_calls[0].args[1])
        self.assertEqual(len(cafe_calls), 2)
        self.assertEqual(len(culture_calls), 2)

    def test_positive_theme_score_does_not_bypass_course_viability(self):
        themes = ["CAFE", "SEA"]
        positive_but_nonviable = {
            zone_id for zone_id, places in TOURAPI_PLACES_BY_ZONE.items()
            if calculate_zone_theme_score(places, themes) > 0
            and not has_coarse_course_viability(places, themes)
        }
        self.assertTrue(positive_but_nonviable)
        response, calls = self.recommend(themes)
        for item in response.destinations:
            self.assertNotIn(item.destinationId, positive_but_nonviable)
            self.assertTrue(has_coarse_course_viability(TOURAPI_PLACES_BY_ZONE[item.destinationId], themes))
        self.assertEqual(len(calls), 2)

    def test_unmatched_zones_never_reach_routing(self):
        response, calls = self.recommend(["CAFE"])
        unmatched = {
            (zone.center_latitude, zone.center_longitude)
            for zone in DESTINATION_ZONES
            if calculate_zone_theme_score(TOURAPI_PLACES_BY_ZONE[zone.destination_id], ["CAFE"]) == 0
        }
        self.assertTrue(unmatched)
        for call in calls:
            coordinate = call.args[1]
            self.assertNotIn((coordinate.latitude, coordinate.longitude), unmatched)
        self.assertTrue(all(item.themeScore > 0 for item in response.destinations))


if __name__ == "__main__":
    main()
