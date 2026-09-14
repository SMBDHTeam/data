import re
from enum import Enum
from unittest import TestCase

import spontaneous.models as spontaneous_models
from schedule.persistence import (
    SPONTANEOUS_THEME_LABELS,
    normalize_style_summary,
    spontaneous_theme_text,
)

CODE_PATTERN = re.compile(r"[A-Z]{2,}_?[A-Z_]*")


class NormalizeStyleSummaryTest(TestCase):
    def assertNoCode(self, text):
        self.assertIsNone(CODE_PATTERN.search(text), f"내부 코드가 남았다: {text}")

    def test_slash_format_keeps_current_output(self):
        summary = "COMPANION_FRIENDS / MOBILITY_LOW_WALK / PACE_RELAXED / TRANSIT_SIMPLE / THEME_FOOD"

        self.assertEqual(normalize_style_summary(summary), "친구와 맛집 여유 일정")

    def test_question_answer_comma_format(self):
        # dev 에 48건 남아 있던 형식이다. " / " 로만 나누면 통째로 원문이 나간다.
        summary = (
            "COMPANION:COMPANION_PARENTS, MOBILITY:MOBILITY_NORMAL, PACE:PACE_RELAXED, "
            "TRANSIT:TRANSIT_SIMPLE, THEME:THEME_FOOD, THEME:THEME_NATURE"
        )

        self.assertEqual(normalize_style_summary(summary), "부모님과 맛집·자연 여유 일정")

    def test_retired_answers_are_labeled(self):
        summary = "COMPANION:COMPANION_PARENTS, THEME:THEME_LOCAL, PACE:PACE_BALANCED"

        self.assertEqual(normalize_style_summary(summary), "부모님과 로컬 적당한 일정")

    def test_unknown_answer_does_not_expose_the_rest(self):
        # 모르는 코드가 하나 섞였다고 원문 전체를 돌려주면 안 된다.
        result = normalize_style_summary("COMPANION_SOLO / THEME_SOMETHING_NEW / PACE_PACKED")

        self.assertEqual(result, "혼자 알찬 일정")

    def test_only_unknown_answers_fall_back(self):
        self.assertEqual(normalize_style_summary("THEME_A_NEW / PACE_B_NEW"), "추천 일정")

    def test_same_theme_label_is_not_repeated(self):
        result = normalize_style_summary("THEME_CULTURE / THEME_HISTORY_CULTURE / PACE_RELAXED")

        self.assertEqual(result, "문화·역사 여유 일정")

    def test_sentence_is_left_as_is(self):
        self.assertEqual(normalize_style_summary("부모님과 쇼핑 알찬 일정"), "부모님과 쇼핑 알찬 일정")

    def test_empty_summary(self):
        self.assertEqual(normalize_style_summary(None), "추천 일정")
        self.assertEqual(normalize_style_summary("   "), "추천 일정")

    def test_spontaneous_theme_codes_are_labeled(self):
        summary = "즉흥여행 · 광안리·민락 · SHOPPING, CAFE, CULTURE"

        self.assertEqual(normalize_style_summary(summary), "즉흥여행 · 광안리·민락 · 쇼핑, 카페, 문화")

    def test_spontaneous_without_themes_is_left_as_is(self):
        # 목적지 이름의 가운뎃점을 테마 구분으로 오해하면 안 된다.
        self.assertEqual(normalize_style_summary("즉흥여행 · 광안리·민락"), "즉흥여행 · 광안리·민락")

    def test_spontaneous_already_labeled_is_left_as_is(self):
        summary = "즉흥여행 · 광안리·민락 · 쇼핑, 카페"

        self.assertEqual(normalize_style_summary(summary), summary)


class SpontaneousThemeTextTest(TestCase):
    def test_codes_become_labels(self):
        self.assertEqual(spontaneous_theme_text(["SEA", "NIGHT_VIEW"]), "바다, 야경")

    def test_unknown_codes_are_dropped(self):
        self.assertEqual(spontaneous_theme_text(["CAFE", "NOT_A_THEME"]), "카페")

    def test_every_theme_has_a_label(self):
        # 테마를 추가하고 이름을 빠뜨리면 그 테마만 요약에서 조용히 사라진다.
        theme_enum = next(
            value
            for value in vars(spontaneous_models).values()
            if isinstance(value, type) and issubclass(value, Enum) and "SEAFOOD" in value.__members__
        )

        missing = [member.value for member in theme_enum if member.value not in SPONTANEOUS_THEME_LABELS]
        self.assertEqual(missing, [])
