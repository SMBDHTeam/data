from __future__ import annotations

from dataclasses import dataclass


SECONDARY_THEME_RULES: dict[str, list[str]] = {
    "FOOD": ["HEALING"],
    "NATURE": ["HEALING"],
    "CULTURE": [],
    "ACTIVITY": ["NATURE"],
    "SHOPPING": ["FOOD"],
    "HEALING": ["NATURE", "FOOD"],
}

SEMANTIC_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("beach", ("해수욕장", "해변", "비치")),
    ("coastal_walk", ("해안산책로", "해안길", "산책로", "동백섬")),
    ("park", ("공원", "수목원", "숲")),
    ("historic_site", ("역사", "유적", "기념관", "사찰", "절", "문화마을")),
    ("museum", ("박물관", "미술관", "전시", "역사관")),
    ("market", ("시장", "상가", "몰", "백화점", "아울렛")),
    ("food_street", ("맛집", "식당", "카페", "브런치", "디저트", "국밥", "밀면")),
    ("marine_activity", ("요트", "서핑", "카약", "패들보드", "sup")),
    ("ride_activity", ("케이블카", "루지", "레일바이크", "짚라인")),
    ("spa_relax", ("온천", "스파", "사우나")),
    ("resort_stay", ("리조트", "풀빌라", "호텔", "숙소")),
    ("scenic_view", ("전망대", "절경", "야경")),
)

MOOD_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("relaxed", ("휴식", "힐링", "여유", "조용", "산책", "온천")),
    ("scenic", ("바다", "해변", "해수욕장", "야경", "전망", "절경")),
    ("local", ("시장", "골목", "로컬", "전통")),
    ("family_friendly", ("공원", "박물관", "수목원", "해수욕장")),
    ("active", ("체험", "액티비티", "케이블카", "루지", "서핑", "요트")),
)

LOW_MOBILITY_BURDEN_KEYWORDS = (
    "감천",
    "흰여울",
    "이바구",
    "산복",
    "계단",
    "전망대",
    "가파른",
    "언덕",
)


@dataclass(frozen=True)
class PlaceAnalysis:
    primary_theme: str
    secondary_themes: list[str]
    semantic_tags: list[str]
    mood_tags: list[str]
    is_meal_place: bool
    is_low_mobility_friendly: bool
    cluster_key: str
    reason: str


def analyze_place(
    primary_theme: str,
    text: str,
    content_type_id: str | None,
    source_category: str | None,
) -> PlaceAnalysis:
    normalized_text = normalize_text(text)
    normalized_content_type = (content_type_id or "").strip()
    normalized_source_category = (source_category or "").strip().upper()

    semantic_tags = match_tags(normalized_text, SEMANTIC_TAG_RULES)
    mood_tags = match_tags(normalized_text, MOOD_TAG_RULES)

    is_meal_place = primary_theme == "FOOD" or normalized_content_type == "39"
    is_low_mobility_friendly = not any(
        keyword in normalized_text for keyword in LOW_MOBILITY_BURDEN_KEYWORDS
    )
    secondary_themes = infer_secondary_themes(
        primary_theme, semantic_tags, mood_tags, is_meal_place
    )
    cluster_key = derive_cluster_key(primary_theme, semantic_tags, normalized_source_category)
    reason = build_reason(primary_theme, semantic_tags, mood_tags, is_meal_place)

    return PlaceAnalysis(
        primary_theme=primary_theme,
        secondary_themes=secondary_themes,
        semantic_tags=semantic_tags,
        mood_tags=mood_tags,
        is_meal_place=is_meal_place,
        is_low_mobility_friendly=is_low_mobility_friendly,
        cluster_key=cluster_key,
        reason=reason,
    )


def normalize_text(text: str | None) -> str:
    return (text or "").strip().lower()


def match_tags(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    tags: list[str] = []
    for tag, keywords in rules:
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    return tags


def infer_secondary_themes(
    primary_theme: str,
    semantic_tags: list[str],
    mood_tags: list[str],
    is_meal_place: bool,
) -> list[str]:
    ordered: list[str] = []
    for theme in SECONDARY_THEME_RULES.get(primary_theme, []):
        if theme not in ordered:
            ordered.append(theme)

    if "scenic_view" in semantic_tags and "NATURE" not in ordered and primary_theme != "NATURE":
        ordered.append("NATURE")
    if "museum" in semantic_tags and "CULTURE" not in ordered and primary_theme != "CULTURE":
        ordered.append("CULTURE")
    if is_meal_place and "FOOD" != primary_theme and "FOOD" not in ordered:
        ordered.append("FOOD")
    if "relaxed" in mood_tags and "HEALING" not in ordered and primary_theme != "HEALING":
        ordered.append("HEALING")
    return ordered[:3]


def derive_cluster_key(
    primary_theme: str,
    semantic_tags: list[str],
    source_category: str,
) -> str:
    if semantic_tags:
        return f"{primary_theme.lower()}_{semantic_tags[0]}"
    if source_category:
        return f"{primary_theme.lower()}_{source_category[:5].lower()}"
    return primary_theme.lower()


def build_reason(
    primary_theme: str,
    semantic_tags: list[str],
    mood_tags: list[str],
    is_meal_place: bool,
) -> str:
    label = theme_label(primary_theme)
    details: list[str] = []
    if semantic_tags:
        details.append(", ".join(semantic_tags[:2]))
    if mood_tags:
        details.append(", ".join(mood_tags[:2]))
    if is_meal_place:
        details.append("meal_place")
    if not details:
        return f"{label} 중심 장소"
    return f"{label} 중심 장소 ({'; '.join(details)})"


def theme_label(theme: str) -> str:
    return {
        "FOOD": "맛집",
        "NATURE": "자연",
        "CULTURE": "문화·역사",
        "ACTIVITY": "체험·액티비티",
        "SHOPPING": "쇼핑",
        "HEALING": "휴식",
    }.get(theme, theme)
