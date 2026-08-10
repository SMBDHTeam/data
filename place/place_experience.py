from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExperienceProfile:
    experience_type: str
    semantic_group: str
    identities: frozenset[str]
    environments: frozenset[str]
    contents: frozenset[str]
    atmospheres: frozenset[str]
    contributions: dict[str, int]

    def contribution(self, key: str) -> int:
        return self.contributions.get(key, 0)


def classify_place(name: str | None, category_label: str | None, content_type_id: str | None) -> ExperienceProfile:
    text = f"{name or ''} {category_label or ''}".lower()
    content_type_id = (content_type_id or "").strip()

    cafe = content_type_id == "39" and contains_any(text, "카페", "커피", "베이커리", "디저트", "찻집")
    food = content_type_id == "39"
    market = contains_any(text, "시장", "상가", "전통시장")
    shopping = content_type_id == "38" or contains_any(text, "쇼핑", "백화점", "아울렛", "쇼핑몰")
    exhibition = content_type_id == "14" or contains_any(text, "박물관", "미술관", "전시", "기념관")
    history = contains_any(text, "역사", "유적", "문화재", "사찰", "성당", "교회", "문화마을")
    event = content_type_id == "15" or contains_any(text, "축제", "공연", "행사", "페스티벌")
    performance = contains_any(text, "공연", "콘서트", "뮤지컬", "불꽃")
    night_view = contains_any(text, "야경", "나이트", "night view")
    activity = content_type_id == "28" or contains_any(text, "체험", "레저", "요트", "서핑", "케이블카")
    beach = contains_any(text, "해수욕장", "해변", "비치", "백사장")
    coastal = beach or contains_any(text, "바다", "해안", "광안리", "송도", "송정", "영도", "기장")
    coastal_view = contains_any(text, "전망대", "스카이워크", "다릿돌", "등대") and coastal
    nature = contains_any(text, "숲", "둘레길", "산책로", "수목원", "생태", "등산")
    park = contains_any(text, "공원", "정원")
    village = contains_any(text, "마을", "골목", "거리", "로드")
    landmark = contains_any(text, "전망", "타워", "랜드마크", "광장")

    identities: set[str] = set()
    environments: set[str] = set()
    contents: set[str] = set()
    atmospheres: set[str] = set()
    contributions: dict[str, int] = {}

    def experience(key: str, score: int) -> None:
        contributions[key] = max(score, contributions.get(key, 0))

    if food:
        identities.add("CAFE" if cafe else "RESTAURANT")
        contents.add("FOOD")
        experience("CAFE_REST" if cafe else "FOOD", 100)
    if market:
        identities.add("MARKET")
        environments.add("URBAN")
        contents.add("SHOPPING")
        atmospheres.update({"LOCAL", "LIVELY"})
        experience("MARKET_BROWSING", 100)
    if shopping:
        identities.add("SHOPPING")
        environments.add("URBAN")
        contents.add("SHOPPING")
        experience("SHOPPING", 100)
    if exhibition:
        identities.add("MUSEUM")
        environments.add("INDOOR")
        contents.add("EXHIBITION")
        experience("EXHIBITION_VIEW", 100)
        experience("CULTURE_VIEW", 80)
    if history:
        identities.add("HERITAGE")
        contents.add("HISTORY")
        experience("CULTURE_VIEW", 100)
    if event:
        identities.add("EVENT_VENUE")
        contents.add("EVENT")
        atmospheres.add("LIVELY")
        experience("EVENT_ATTENDANCE", 100)
    if performance:
        contents.add("PERFORMANCE")
        experience("PERFORMANCE_VIEW", 100)
    if night_view or contains_any(text, "불꽃", "야간"):
        atmospheres.add("NIGHT_VIEW")
        experience("NIGHT_VIEW", 80)
    if activity:
        identities.add("ACTIVITY")
        contents.add("ACTIVITY")
        experience("ACTIVITY", 100)
    if beach:
        identities.add("BEACH")
    if coastal:
        environments.add("COASTAL")
        atmospheres.add("SCENIC")
        experience("SEA_VIEW", 100)
        if beach:
            experience("BEACH_WALK", 100)
        experience("PHOTO", 80)
    if coastal_view or landmark:
        identities.add("LANDMARK")
        atmospheres.add("SCENIC")
        experience("SCENIC_VIEW", 100)
    if nature:
        identities.add("NATURE_TRAIL")
        environments.add("GREEN")
        atmospheres.add("QUIET")
        experience("NATURE_WALK", 100)
        experience("PHOTO", 70)
    if park:
        identities.add("PARK")
        environments.add("GREEN")
        atmospheres.add("QUIET")
        experience("PARK_REST", 100)
        experience("REST", 80)
    if village:
        identities.add("VILLAGE_STREET")
        environments.add("URBAN")
        atmospheres.add("LOCAL")
        experience("VILLAGE_WALK", 100)
        experience("PHOTO", 70)

    experience_type = primary_type(
        food, cafe, market, shopping, exhibition, history, event, activity,
        beach, coastal_view, nature, park, village, landmark,
    )
    semantic_group = primary_semantic_group(experience_type, environments)
    if not identities:
        identities.add("OTHER")
    if not environments:
        environments.add("OTHER")
    if not contents:
        contents.add("NONE")
    if not atmospheres:
        atmospheres.add("OTHER")
    return ExperienceProfile(
        experience_type=experience_type,
        semantic_group=semantic_group,
        identities=frozenset(identities),
        environments=frozenset(environments),
        contents=frozenset(contents),
        atmospheres=frozenset(atmospheres),
        contributions=contributions,
    )


def similarity_percent(left: ExperienceProfile, right: ExperienceProfile) -> int:
    identity = overlap(left.identities, right.identities)
    environment = overlap(left.environments, right.environments)
    experience = experience_overlap(left.contributions, right.contributions)
    content = overlap(left.contents, right.contents)
    return round((identity * 0.25 + environment * 0.30 + experience * 0.30 + content * 0.15) * 100)


def primary_type(
    food: bool,
    cafe: bool,
    market: bool,
    shopping: bool,
    exhibition: bool,
    history: bool,
    event: bool,
    activity: bool,
    beach: bool,
    coastal_view: bool,
    nature: bool,
    park: bool,
    village: bool,
    landmark: bool,
) -> str:
    if food:
        return "CAFE_REST" if cafe else "FOOD"
    if market:
        return "MARKET_COMMERCE"
    if shopping:
        return "SHOPPING"
    if exhibition:
        return "EXHIBITION_MUSEUM"
    if history:
        return "HISTORY_CULTURE"
    if event:
        return "EVENT"
    if activity:
        return "ACTIVITY"
    if beach:
        return "BEACH_WALK"
    if coastal_view:
        return "COASTAL_VIEW"
    if nature:
        return "NATURE_TRAIL"
    if park:
        return "PARK_GREEN"
    if village:
        return "VILLAGE_STREET_WALK"
    if landmark:
        return "LANDMARK"
    return "OTHER"


def primary_semantic_group(experience_type: str, environments: set[str]) -> str:
    if "COASTAL" in environments:
        return "COASTAL_NATURE"
    if "GREEN" in environments:
        return "GREEN_NATURE"
    if experience_type == "VILLAGE_STREET_WALK":
        return "URBAN_WALK"
    if experience_type in {"MARKET_COMMERCE", "SHOPPING"}:
        return "COMMERCE"
    if experience_type in {"HISTORY_CULTURE", "EXHIBITION_MUSEUM", "EVENT"}:
        return "CULTURE"
    if experience_type == "ACTIVITY":
        return "ACTIVITY"
    if experience_type in {"FOOD", "CAFE_REST"}:
        return "FOOD_REST"
    if experience_type == "LANDMARK":
        return "LANDMARK"
    return "OTHER"


def overlap(left: frozenset[str], right: frozenset[str]) -> float:
    smaller = min(len(left), len(right))
    if smaller == 0:
        return 0.0
    return len(left & right) / smaller


def experience_overlap(left: dict[str, int], right: dict[str, int]) -> float:
    shared = 0
    for key, value in left.items():
        if key in right:
            shared += min(value, right[key])
    smaller = min(sum(left.values()), sum(right.values()))
    if smaller == 0:
        return 0.0
    return shared / smaller


def contains_any(value: str, *tokens: str) -> bool:
    return any(token in value for token in tokens)
