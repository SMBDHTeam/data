from datetime import datetime, timedelta, timezone, tzinfo
from unittest import TestCase

from spontaneous.models import TransportMode, TravelTheme
from spontaneous.time_profile import (
    CAR_SCENIC_BONUS,
    EMPTY_THEME_TIME_SCALE,
    EXPLICIT_THEME_TIME_SCALE,
    TimeProfile,
    calculate_time_fit_bonus,
    resolve_time_profile,
)


KST = timezone(timedelta(hours=9))


class TimeProfileTest(TestCase):
    def test_all_local_time_boundaries(self):
        for clock, expected in (
            ("00:00:00", "LATE_NIGHT"), ("08:59:59", "LATE_NIGHT"),
            ("09:00:00", "DAYTIME"), ("13:59:59", "DAYTIME"),
            ("14:00:00", "AFTERNOON"), ("16:59:59", "AFTERNOON"),
            ("17:00:00", "SUNSET"), ("18:59:59", "SUNSET"),
            ("19:00:00", "NIGHT"), ("21:59:59", "NIGHT"),
            ("22:00:00", "LATE_NIGHT"), ("23:59:59", "LATE_NIGHT"),
        ):
            with self.subTest(clock=clock):
                self.assertEqual(resolve_time_profile(datetime.fromisoformat(
                    f"2026-09-10T{clock}+09:00",
                )), TimeProfile(expected))

    def test_equivalent_offsets_use_korea_local_hour(self):
        for hour in range(24):
            local = datetime(2026, 9, 10, hour, tzinfo=KST)
            for offset in (-7, 0, 5, 9, 12):
                with self.subTest(hour=hour, offset=offset):
                    self.assertEqual(resolve_time_profile(local), resolve_time_profile(
                        local.astimezone(timezone(timedelta(hours=offset))),
                    ))

    def test_start_at_date_and_system_clock_do_not_define_profile(self):
        for year in (2020, 2026, 2030):
            self.assertEqual(resolve_time_profile(datetime(year, 1, 1, 19, tzinfo=KST)), TimeProfile.NIGHT)

    def test_naive_and_missing_offset_are_neutral_without_kst_guess(self):
        class MissingOffset(tzinfo):
            def utcoffset(self, value):
                return None

        for value in (None, datetime(2026, 9, 10, 19), datetime(2026, 9, 10, 19, tzinfo=MissingOffset())):
            with self.subTest(value=value):
                self.assertIsNone(resolve_time_profile(value))
                self.assertEqual(calculate_time_fit_bonus({"NIGHT_VIEW"}, [], resolve_time_profile(value), TransportMode.CAR), 0)

    def test_explicit_cafe_is_never_penalized_in_any_profile(self):
        for profile in TimeProfile:
            self.assertGreaterEqual(calculate_time_fit_bonus({"CAFE"}, {"CAFE"}, profile, TransportMode.WALK), 0)
            self.assertGreaterEqual(calculate_time_fit_bonus({"CAFE", "CULTURE"}, {"CAFE"}, profile, TransportMode.WALK), 0)

    def test_every_explicit_theme_is_protected_from_negative_weight(self):
        for profile in TimeProfile:
            for theme in TravelTheme:
                with self.subTest(profile=profile, theme=theme):
                    self.assertGreaterEqual(calculate_time_fit_bonus({theme}, {theme}, profile, TransportMode.WALK), 0)

    def test_empty_themes_prefer_afternoon_and_night_groups(self):
        def score(theme, profile):
            return calculate_time_fit_bonus({theme}, [], profile, TransportMode.WALK)

        for theme in ("CAFE", "SEA", "WALK"):
            self.assertGreater(score(theme, TimeProfile.AFTERNOON), score("NIGHT_VIEW", TimeProfile.AFTERNOON))
        for theme in ("NIGHT_VIEW", "SEA", "FOOD"):
            self.assertGreater(score(theme, TimeProfile.NIGHT), score("CAFE", TimeProfile.NIGHT))

    def test_car_evening_scenic_bonus_is_small_and_not_stacked(self):
        for profile in (TimeProfile.SUNSET, TimeProfile.NIGHT, TimeProfile.LATE_NIGHT):
            for themes in ({"SEA"}, {"NIGHT_VIEW"}, {"NATURE"}, {"WALK"}, {"SEA", "NATURE", "NIGHT_VIEW"}):
                with self.subTest(profile=profile, themes=themes):
                    car = calculate_time_fit_bonus(themes, {"CAFE"}, profile, TransportMode.CAR)
                    walk = calculate_time_fit_bonus(themes, {"CAFE"}, profile, TransportMode.WALK)
                    self.assertAlmostEqual(car - walk, CAR_SCENIC_BONUS * EXPLICIT_THEME_TIME_SCALE)

    def test_public_transit_never_receives_car_bonus(self):
        for profile in TimeProfile:
            for themes in ({"SEA"}, {"NATURE"}, {"NIGHT_VIEW"}, {"WALK"}):
                self.assertEqual(calculate_time_fit_bonus(themes, [], profile, TransportMode.PUBLIC_TRANSIT),
                                 calculate_time_fit_bonus(themes, [], profile, TransportMode.WALK))

    def test_daytime_car_and_non_scenic_places_receive_no_car_bonus(self):
        for profile, themes in ((TimeProfile.DAYTIME, {"SEA"}), (TimeProfile.AFTERNOON, {"NATURE"}),
                                (TimeProfile.NIGHT, {"CAFE"}), (TimeProfile.NIGHT, {"FOOD"})):
            self.assertEqual(calculate_time_fit_bonus(themes, [], profile, TransportMode.CAR),
                             calculate_time_fit_bonus(themes, [], profile, TransportMode.WALK))

    def test_bonus_normalization_and_empty_theme_strength(self):
        themes = {"NIGHT_VIEW"}
        explicit = calculate_time_fit_bonus(themes, {"FOOD"}, TimeProfile.NIGHT, TransportMode.CAR)
        empty = calculate_time_fit_bonus(themes, [], TimeProfile.NIGHT, TransportMode.CAR)
        self.assertAlmostEqual(explicit, 0.056)
        self.assertAlmostEqual(empty, 0.42)
        self.assertAlmostEqual(empty / explicit, EMPTY_THEME_TIME_SCALE / EXPLICIT_THEME_TIME_SCALE)
        for profile in TimeProfile:
            for mode in TransportMode:
                for theme in TravelTheme:
                    self.assertLessEqual(round(abs(calculate_time_fit_bonus({theme}, {"FOOD"}, profile, mode)), 6), 0.056)

    def test_duplicate_case_order_and_empty_evidence_are_stable(self):
        arguments = ({"CAFE"}, TimeProfile.NIGHT, TransportMode.CAR)
        self.assertEqual(calculate_time_fit_bonus(["sea", "NATURE", "SEA"], *arguments),
                         calculate_time_fit_bonus({"NATURE", "SEA"}, *arguments))
        self.assertEqual(calculate_time_fit_bonus([], *arguments), 0)
