from __future__ import annotations

import json
import logging
import re
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException

from core.runtime_env import load_runtime_env
from place.place_experience import classify_place
from schedule.models import (
    DayLocation,
    MapMarker,
    PlanningAssumptions,
    RouteLine,
    ScheduleCreateRequest,
    ScheduleDay,
    ScheduleListResponse,
    ScheduleLocation,
    ScheduleMapResponse,
    SchedulePlace,
    ScheduleResponse,
    ScheduleStop,
    ScheduleTransit,
    ScheduleUpdateRequest,
    StopMarker,
)
from schedule.persistence import (
    db_enabled,
    list_schedules as list_schedules_from_db,
    load_schedule as load_schedule_from_db,
    save_schedule as save_schedule_to_db,
)
from transit.routing import TransitPoint, find_route

load_runtime_env()
log = logging.getLogger("data.schedule.service")

DEFAULT_PLANNING_WARNINGS = [
    "일부 장소 정보는 운영시간과 상세 안내를 방문 전에 다시 확인해 주세요.",
    "일부 도보 구간은 실시간 보행 장애 정보가 반영되지 않을 수 있습니다.",
]

BASE_DIR = Path(__file__).resolve().parent.parent
CANDIDATE_PLACES_PATH = BASE_DIR / "candidate_places.json"

DEFAULT_STAY_MINUTES = 90
MIN_STAY_MINUTES = 30
MAX_STOPS_PER_DAY = 5
EARLY_FINISH_BUFFER_MINUTES = 180
MIN_DAY_UTILIZATION_RATIO = 0.7
CANDIDATE_POOL_TTL_SECONDS = max(5, int(os.getenv("SCHEDULE_CANDIDATE_POOL_TTL_SECONDS", "60")))

THEME_PLACEHOLDER_NAMES = {
    "THEME_FOOD": ["로컬 맛집", "시장 먹거리", "바다뷰 카페", "디저트 스팟", "해산물 식당"],
    "THEME_NATURE": ["해변 산책", "전망 포인트", "도심 공원", "해안 산책로", "자연 경관지"],
    "THEME_CULTURE": ["전시 공간", "역사 장소", "문화 거리", "박물관", "전통 명소"],
    "THEME_ACTIVITY": ["체험 스팟", "액티비티 존", "레포츠 장소", "야외 체험", "탐방 코스"],
    "THEME_SHOPPING": ["로컬 마켓", "기념품 거리", "쇼핑 스팟", "상점가", "복합 쇼핑존"],
    "THEME_HEALING": ["휴식 공간", "조용한 산책지", "힐링 스팟", "뷰 포인트", "리프레시 코스"],
}

THEME_CATEGORY_LABELS = {
    "THEME_FOOD": ("A0502", "음식점"),
    "THEME_NATURE": ("A0101", "자연 관광지"),
    "THEME_CULTURE": ("A0201", "역사 관광지"),
    "THEME_ACTIVITY": ("A0302", "레포츠"),
    "THEME_SHOPPING": ("A0401", "쇼핑"),
    "THEME_HEALING": ("A0202", "휴양 관광지"),
}

CONTENT_TYPE_LABELS = {
    "12": "관광지",
    "14": "문화시설",
    "15": "축제·공연",
    "28": "레포츠",
    "32": "숙박",
    "38": "쇼핑",
    "39": "음식점",
}

THEME_CONTENT_TYPE_RULES = {
    "12": ["THEME_NATURE"],
    "14": ["THEME_CULTURE"],
    "28": ["THEME_ACTIVITY"],
    "32": ["THEME_HEALING", "THEME_NATURE"],
    "38": ["THEME_SHOPPING"],
    "39": ["THEME_FOOD"],
}

THEME_KEYWORD_RULES = {
    "THEME_FOOD": ["카페", "브런치", "디저트", "커피", "맛집", "국밥", "횟집", "밀면", "식당", "해산물"],
    "THEME_NATURE": ["해변", "해수욕장", "공원", "숲", "수목원", "바다", "산책로", "해안산책로", "해안길", "동백섬"],
    "THEME_HEALING": ["산책", "휴식", "힐링", "조용", "여유", "온천", "사우나"],
    "THEME_CULTURE": ["박물관", "미술관", "전시", "역사", "전통", "공연", "문화", "기념관", "유적", "마을"],
    "THEME_ACTIVITY": ["체험", "레저", "테마파크", "액티비티", "케이블카", "요트", "서핑"],
    "THEME_SHOPPING": ["시장", "쇼핑", "기념품", "몰", "상가", "아울렛"],
}


@dataclass(frozen=True)
class PlannedDay:
    date: date
    day_no: int
    start_time: time
    end_time: time
    start_location: ScheduleLocation
    end_location: ScheduleLocation


@dataclass(frozen=True)
class CandidatePlace:
    id: int
    external_id: str
    content_type_id: str
    name: str
    category_label: str
    address: str
    longitude: Decimal
    latitude: Decimal


@dataclass(frozen=True)
class MealSlot:
    code: str
    start: time
    end: time


@dataclass(frozen=True)
class FixedEventSpec:
    place_id: int
    name: str
    starts_at: datetime
    ends_at: datetime


DEFAULT_CANDIDATES = [
    CandidatePlace(1, "GAMCHEON", "14", "감천문화마을", "문화시설", "부산 사하구 감내2로 203", Decimal("129.0106"), Decimal("35.0974")),
    CandidatePlace(2, "SONGDO_BEACH", "12", "송도해수욕장", "관광지", "부산 서구 송도해변로 100", Decimal("129.0172"), Decimal("35.0770")),
    CandidatePlace(3, "JAGALCHI", "38", "자갈치시장", "쇼핑", "부산 중구 자갈치해안로 52", Decimal("129.0305"), Decimal("35.0967")),
    CandidatePlace(4, "GWANGALLI", "12", "광안리해수욕장", "관광지", "부산 수영구 광안해변로 219", Decimal("129.1186"), Decimal("35.1532")),
    CandidatePlace(5, "BUSAN_MUSEUM", "14", "부산박물관", "문화시설", "부산 남구 유엔평화로 63", Decimal("129.0840"), Decimal("35.1296")),
    CandidatePlace(6, "HAEUNDAE_BEACH", "12", "해운대해수욕장", "관광지", "부산 해운대구 해운대해변로 264", Decimal("129.1604"), Decimal("35.1587")),
    CandidatePlace(7, "DALMAJI", "12", "달맞이길 전망대", "관광지", "부산 해운대구 달맞이길 190", Decimal("129.1775"), Decimal("35.1578")),
    CandidatePlace(8, "GUKJE_MARKET", "38", "국제시장", "쇼핑", "부산 중구 신창동4가", Decimal("129.0286"), Decimal("35.1025")),
]

LUNCH_SLOT = MealSlot("LUNCH", time(11, 0), time(14, 0))
DINNER_SLOT = MealSlot("DINNER", time(17, 0), time(19, 0))
MEAL_SLOTS = [LUNCH_SLOT, DINNER_SLOT]

CONTENT_TYPE_FALLBACK_LABELS = {
    "12": "관광지",
    "14": "문화시설",
    "15": "축제·공연",
    "28": "레포츠",
    "32": "숙박",
    "38": "쇼핑",
    "39": "음식점",
}


def resolve_db_dsn() -> tuple[str | None, str | None, str | None]:
    jdbc_url = os.getenv("SPRING_DATASOURCE_URL")
    username = os.getenv("SPRING_DATASOURCE_USERNAME")
    password = os.getenv("SPRING_DATASOURCE_PASSWORD")
    if jdbc_url:
        return jdbc_url.removeprefix("jdbc:"), username, password

    host = os.getenv("LOCAL_POSTGRES_HOST")
    port = os.getenv("LOCAL_POSTGRES_PORT")
    db = os.getenv("LOCAL_POSTGRES_DB")
    user = os.getenv("LOCAL_POSTGRES_USER")
    local_password = os.getenv("LOCAL_POSTGRES_PASSWORD")
    if host and port and db and user:
        return f"postgresql://{user}:{local_password or ''}@{host}:{port}/{db}", user, local_password
    return None, None, None


CANDIDATE_SELECT = """
        SELECT
            id,
            external_content_id,
            COALESCE(content_type_id, '12') AS content_type_id,
            name,
            CASE
                WHEN category IS NOT NULL AND btrim(category) <> '' THEN category
                ELSE COALESCE(content_type_id, '12')
            END AS category_label,
            address,
            longitude::text,
            latitude::text
        FROM places
        WHERE name IS NOT NULL
          AND longitude IS NOT NULL
          AND latitude IS NOT NULL
          -- 관리자가 가린 장소는 일정 후보에서 뺀다. Spring 쪽 검색에서도 빠지므로
          -- 사용자가 새로 고를 수 없지만, 이미 만들어 둔 Preview 가 참조할 수는 있다.
          AND hidden_at IS NULL
"""


def rows_to_candidates(rows) -> list[CandidatePlace]:
    candidates: list[CandidatePlace] = []
    for row in rows:
        raw_category_label = str(row[4]) if row[4] is not None else ""
        candidates.append(
            CandidatePlace(
                id=int(row[0]),
                external_id=str(row[1]),
                content_type_id=str(row[2]),
                name=str(row[3]),
                category_label=normalize_category_label(raw_category_label, str(row[2])),
                address=str(row[5]) if row[5] is not None else None,
                longitude=Decimal(str(row[6])),
                latitude=Decimal(str(row[7])),
            )
        )
    return candidates


def load_candidate_places_by_ids(place_ids: list[int]) -> dict[int, CandidatePlace] | None:
    """주어진 id 의 장소를 DB 에서 직접 읽는다.

    후보 풀(CANDIDATE_POOL)은 모듈 로드 시 한 번만 채워지므로, 그 뒤에 등록된
    장소는 들어 있지 않다. 사용자가 방금 등록한 장소를 필수 방문지로 지정하면
    후보 풀에서 찾을 수 없어 일정 생성이 실패했다. 그런 id 만 여기서 조회한다.

    반환값은 조회 결과와 조회 불가를 구분한다.

      {...}  조회 성공. 없는 id 는 키에 없다.
      {}     조회는 됐지만 해당 id 가 하나도 없다.
      None   DB 를 읽을 수 없다. 호출부는 이 경우 존재 검증을 하면 안 된다.
    """
    if not place_ids:
        return {}
    dsn, username, password = resolve_db_dsn()
    if not dsn:
        return None
    try:
        import psycopg
    except ModuleNotFoundError:
        return None

    connect_kwargs: dict[str, object] = {"conninfo": dsn, "autocommit": True}
    parsed_dsn = urlsplit(dsn)
    if username and parsed_dsn.username is None:
        connect_kwargs["user"] = username
    if password and parsed_dsn.password is None:
        connect_kwargs["password"] = password

    try:
        with psycopg.connect(**connect_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(CANDIDATE_SELECT + "          AND id = ANY(%s)\n", (place_ids,))
                rows = cursor.fetchall()
    except Exception:
        log.exception("failed to load places by id from database. placeIds=%s", place_ids)
        return None

    return {candidate.id: candidate for candidate in rows_to_candidates(rows)}


def db_runtime_status() -> dict[str, str | bool | None]:
    dsn, _, _ = resolve_db_dsn()
    parsed = urlsplit(dsn) if dsn else None
    return {
        "db_enabled": bool(dsn),
        "db_host": parsed.hostname if parsed else None,
    }


def load_candidate_places_from_db() -> list[CandidatePlace]:
    dsn, username, password = resolve_db_dsn()
    if not dsn:
        log.info("candidate place db load skipped: database dsn not configured")
        return []
    try:
        import psycopg
    except ModuleNotFoundError:
        log.warning("candidate place db load skipped: psycopg not installed")
        return []

    connect_kwargs: dict[str, object] = {
        "conninfo": dsn,
        "autocommit": True,
    }
    parsed_dsn = urlsplit(dsn)
    if username and parsed_dsn.username is None:
        connect_kwargs["user"] = username
    if password and parsed_dsn.password is None:
        connect_kwargs["password"] = password

    query = CANDIDATE_SELECT + "\n        ORDER BY id\n"
    try:
        with psycopg.connect(**connect_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
    except Exception:
        log.exception("candidate place db load failed")
        return []

    candidates = rows_to_candidates(rows)
    log.info("candidate place db load succeeded. count=%s", len(candidates))
    return candidates


def normalize_category_label(raw_label: str, content_type_id: str) -> str:
    normalized = raw_label.strip()
    if not normalized:
        return CONTENT_TYPE_FALLBACK_LABELS.get(content_type_id, "관광지")
    if normalized.startswith("A") and normalized[1:].isdigit():
        return CONTENT_TYPE_FALLBACK_LABELS.get(content_type_id, "관광지")
    return normalized


def load_candidate_places() -> tuple[list[CandidatePlace], str]:
    db_candidates = load_candidate_places_from_db()
    if db_candidates:
        return db_candidates, "database"
    if not CANDIDATE_PLACES_PATH.exists():
        log.warning("candidate_places.json missing. using built-in defaults")
        return DEFAULT_CANDIDATES, "built_in_default"
    try:
        raw_items = json.loads(CANDIDATE_PLACES_PATH.read_text(encoding="utf-8"))
        candidates = [
            CandidatePlace(
                id=int(item["id"]),
                external_id=str(item["external_id"]),
                content_type_id=str(item["content_type_id"]),
                name=str(item["name"]),
                category_label=str(item["category_label"]),
                address=str(item["address"]) if item.get("address") is not None else None,
                longitude=Decimal(str(item["longitude"])),
                latitude=Decimal(str(item["latitude"])),
            )
            for item in raw_items
        ]
        if candidates:
            log.info("candidate place json load succeeded. count=%s", len(candidates))
            return candidates, "json_fallback"
        return DEFAULT_CANDIDATES, "built_in_default"
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        log.exception("candidate place json load failed. using built-in defaults")
        return DEFAULT_CANDIDATES, "built_in_default"


_candidate_pool_lock = Lock()
CANDIDATE_POOL: list[CandidatePlace] = []
CANDIDATE_POOL_SOURCE = "uninitialized"
_candidate_pool_loaded_at = 0.0


def _refresh_candidate_pool(force: bool = False) -> tuple[list[CandidatePlace], str]:
    global CANDIDATE_POOL, CANDIDATE_POOL_SOURCE, _candidate_pool_loaded_at
    now = monotonic()
    with _candidate_pool_lock:
        if (
            not force
            and CANDIDATE_POOL
            and now - _candidate_pool_loaded_at < CANDIDATE_POOL_TTL_SECONDS
        ):
            return CANDIDATE_POOL, CANDIDATE_POOL_SOURCE

        pool, source = load_candidate_places()
        CANDIDATE_POOL = pool
        CANDIDATE_POOL_SOURCE = source
        _candidate_pool_loaded_at = now

        if source != "database":
            log.warning(
                "schedule candidate pool is not backed by the database. source=%s count=%s. "
                "일정 품질이 크게 떨어진다. SPRING_DATASOURCE_* 환경변수를 확인할 것.",
                source,
                len(pool),
            )
        return CANDIDATE_POOL, CANDIDATE_POOL_SOURCE


def current_candidate_pool(force_refresh: bool = False) -> list[CandidatePlace]:
    return _refresh_candidate_pool(force_refresh)[0]


def current_candidate_pool_source(force_refresh: bool = False) -> str:
    return _refresh_candidate_pool(force_refresh)[1]


_refresh_candidate_pool(force=True)


class ScheduleStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, ScheduleResponse] = {}

    def save(self, schedule: ScheduleResponse) -> ScheduleResponse:
        with self._lock:
            self._items[schedule.id] = schedule
        return schedule

    def list(self) -> ScheduleListResponse:
        with self._lock:
            items = sorted(
                self._items.values(),
                key=lambda item: (item.start_date, str(item.id)),
                reverse=True,
            )
        return ScheduleListResponse(items=items)

    def get(self, schedule_id: UUID) -> ScheduleResponse:
        with self._lock:
            schedule = self._items.get(schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return schedule


STORE = ScheduleStore()


def create_schedule(
    request: ScheduleCreateRequest,
    preview_id: UUID | None = None,
    fixed_events_by_day: dict[int, list[FixedEventSpec]] | None = None,
    owner_id: int | None = None,
) -> ScheduleResponse:
    candidate_source = current_candidate_pool_source()
    log.info(
        "schedule build started. previewId=%s, startDate=%s, endDate=%s, candidateSource=%s",
        preview_id,
        request.start_date,
        request.end_date,
        candidate_source,
    )
    planned_days = plan_days(request)
    places_by_day = distribute_must_visit_places(planned_days, request.must_visit_place_ids)
    target_counts = target_stop_counts(planned_days, request)
    days: list[ScheduleDay] = []
    # 앞선 일차에서 쓴 장소를 누적해 다음 일차가 같은 곳을 다시 고르지 않게 한다.
    used_place_ids: set[int] = set()
    for planned_day in planned_days:
        day_place_ids = places_by_day.get(planned_day.day_no, [])
        target_count = max(len(day_place_ids), target_counts.get(planned_day.day_no, 1))
        stops = build_stops(
            planned_day,
            day_place_ids,
            target_count,
            request,
            fixed_events_by_day.get(planned_day.day_no, []) if fixed_events_by_day else [],
            used_place_ids,
        )
        used_place_ids.update(
            stop.place.id for stop in stops if stop.place is not None and stop.place.id is not None
        )
        days.append(
            ScheduleDay(
                dayNo=planned_day.day_no,
                date=planned_day.date,
                startTime=planned_day.start_time,
                endTime=planned_day.end_time,
                startLocation=to_day_location(planned_day.start_location),
                endLocation=to_day_location(planned_day.end_location),
                startLocationSource="REQUEST",
                endLocationSource="REQUEST",
                summary=build_day_summary(planned_day, stops),
                stops=stops,
                finalTransit=build_final_transit(planned_day, stops),
            )
        )

    days, repair_warnings = repair_schedule_days(planned_days, days, target_counts, request)

    schedule = ScheduleResponse(
        id=uuid4(),
        status="DRAFT",
        startDate=request.start_date,
        endDate=request.end_date,
        dailyStartTime=request.daily_start_time,
        dailyEndTime=request.daily_end_time,
        styleSummary=build_style_summary(request),
        days=days,
        evaluation=None,
        previewId=preview_id,
        planningAssumptions=PlanningAssumptions(
            routeCoverage=resolve_route_coverage(days),
            warnings=dedupe_warnings(
                DEFAULT_PLANNING_WARNINGS
                + (["고정 일정 시간을 반영해 일부 방문 순서를 조정했습니다."] if fixed_events_by_day else [])
                + repair_warnings
            )
        ),
    )
    saved = STORE.save(schedule)
    if db_enabled():
        try:
            save_schedule_to_db(saved, condition_request=request, owner_id=owner_id)
        except Exception:
            log.exception("schedule persistence failed. scheduleId=%s", saved.id)
    log.info(
        "schedule build finished. scheduleId=%s, days=%s, routeCoverage=%s",
        saved.id,
        len(saved.days),
        saved.planning_assumptions.route_coverage if saved.planning_assumptions else None,
    )
    return saved


def list_schedules(user_id: int | None = None) -> ScheduleListResponse:
    """일정 목록. user_id 가 있으면 그 사용자의 일정만 준다.

    DB 가 켜진 운영 경로에서는 조회 실패를 메모리 저장소로 숨기지 않는다.
    목록 API 는 소유자 필터가 중요하므로, 조용히 fallback 하면 다른 사용자 데이터가
    섞여 보일 위험이 있다.

    DB 가 아예 없는 로컬 실행에서만 메모리 저장소를 쓴다.
    """
    if db_enabled():
        try:
            return list_schedules_from_db(user_id)
        except Exception:
            log.exception("schedule list db load failed. userId=%s", user_id)
            raise HTTPException(status_code=503, detail="Schedule list unavailable")
    return STORE.list()


def get_schedule(schedule_id: UUID) -> ScheduleResponse:
    if db_enabled():
        try:
            return load_schedule_from_db(schedule_id)
        except HTTPException:
            # 존재하지 않는 일정의 404 는 정상 응답이므로 그대로 올린다.
            raise
        except Exception:
            log.exception("schedule get db load failed. scheduleId=%s", schedule_id)
    return STORE.get(schedule_id)


def update_schedule(schedule_id: UUID, request: ScheduleUpdateRequest) -> ScheduleResponse:
    existing = get_schedule(schedule_id)
    days_by_no = {day.day_no: day.model_copy(deep=True) for day in existing.days}
    existing_stop_index = {
        stop.id: (day.day_no, stop)
        for day in existing.days
        for stop in day.stops
    }

    grouped_stops: dict[int, list[ScheduleStop]] = defaultdict(list)
    for patch_stop in request.stops:
        if patch_stop.day_no not in days_by_no:
            raise HTTPException(status_code=400, detail=f"dayNo {patch_stop.day_no} does not exist")
        if patch_stop.stop_id is not None:
            original = existing_stop_index.get(patch_stop.stop_id)
            if original is None:
                raise HTTPException(status_code=404, detail=f"stopId {patch_stop.stop_id} not found")
            _, existing_stop = original
            updated_stop = existing_stop.model_copy(deep=True)
            updated_stop.order = patch_stop.order
            updated_stop.stay_minutes = patch_stop.stay_minutes
            updated_stop.arrive_at = None
            updated_stop.depart_at = None
            updated_stop.warnings = dedupe_warnings(
                updated_stop.warnings + ["방문 순서 변경에 따라 도착 및 출발 시간이 다시 계산되었습니다."]
            )
            grouped_stops[patch_stop.day_no].append(updated_stop)
            continue

        grouped_stops[patch_stop.day_no].append(
            candidate_stop(
                order=patch_stop.order,
                stay_minutes=patch_stop.stay_minutes,
                candidate=resolve_place_or_400(patch_stop.place_id),
                selection_reasons=["update_requested_place"],
            )
        )

    updated_days: list[ScheduleDay] = []
    for day_no, day in sorted(days_by_no.items()):
        day_stops = sorted(grouped_stops.get(day_no, day.stops), key=lambda stop: stop.order)
        recalculated = recalculate_day(day, day_stops)
        recalculated.final_transit = build_final_transit(planned_day_from_schedule_day(recalculated), recalculated.stops)
        updated_days.append(recalculated)

    updated_schedule = existing.model_copy(deep=True)
    updated_schedule.days = updated_days
    updated_schedule.planning_assumptions = PlanningAssumptions(
        routeCoverage=resolve_route_coverage(updated_days),
        warnings=dedupe_warnings(
            DEFAULT_PLANNING_WARNINGS + ["일정 수정 내용을 반영해 이동 시간과 방문 순서를 다시 계산했습니다."]
        )
    )
    saved = STORE.save(updated_schedule)
    if db_enabled():
        try:
            save_schedule_to_db(saved, condition_request=request_context_from_schedule(saved))
        except Exception:
            log.exception("schedule update persistence failed. scheduleId=%s", saved.id)
    log.info("schedule updated. scheduleId=%s, days=%s", saved.id, len(saved.days))
    return saved


def get_schedule_map(schedule_id: UUID, day_no: int | None) -> ScheduleMapResponse:
    schedule = get_schedule(schedule_id)
    days = [day for day in schedule.days if day_no is None or day.day_no == day_no]
    if not days:
        raise HTTPException(status_code=404, detail="No matching schedule day found")

    markers: list[StopMarker] = []
    route_lines: list[RouteLine] = []
    for day in days:
        previous_name = day.start_location.name if day.start_location else None
        previous_lon = day.start_location.longitude if day.start_location else None
        previous_lat = day.start_location.latitude if day.start_location else None
        for index, stop in enumerate(day.stops, start=1):
            markers.append(
                StopMarker(
                    dayNo=day.day_no,
                    order=stop.order,
                    placeId=stop.place.id,
                    name=stop.place.name,
                    arriveAt=stop.arrive_at,
                    departAt=stop.depart_at,
                    subtitle=stop.place.category_label,
                    riskLevel="NORMAL",
                    longitude=stop.place.longitude,
                    latitude=stop.place.latitude,
                )
            )
            route_lines.extend(route_lines_for_transit(day.day_no, index, stop.inbound_transit, previous_name, stop.place.name, previous_lon, previous_lat, stop.place.longitude, stop.place.latitude))
            previous_name = stop.place.name
            previous_lon = stop.place.longitude
            previous_lat = stop.place.latitude

        if day.end_location and previous_name:
            route_lines.extend(
                route_lines_for_transit(
                    day.day_no,
                    len(day.stops) + 1,
                    day.final_transit,
                    previous_name,
                    day.end_location.name,
                    previous_lon,
                    previous_lat,
                    day.end_location.longitude,
                    day.end_location.latitude,
                )
            )

    first_day = days[0]
    last_day = days[-1]
    return ScheduleMapResponse(
        startMarker=MapMarker(
            name=first_day.start_location.name if first_day.start_location else "출발지",
            longitude=first_day.start_location.longitude if first_day.start_location else None,
            latitude=first_day.start_location.latitude if first_day.start_location else None,
        ),
        endMarker=MapMarker(
            name=last_day.end_location.name if last_day.end_location else "도착지",
            longitude=last_day.end_location.longitude if last_day.end_location else None,
            latitude=last_day.end_location.latitude if last_day.end_location else None,
        ),
        markers=markers,
        routeLines=route_lines,
    )


def plan_days(request: ScheduleCreateRequest) -> list[PlannedDay]:
    trip_days = (request.end_date - request.start_date).days + 1
    condition_by_day = {condition.day_no: condition for condition in request.days}
    planned_days: list[PlannedDay] = []
    for offset in range(trip_days):
        current_date = request.start_date + timedelta(days=offset)
        day_no = offset + 1
        condition = condition_by_day.get(day_no)
        if condition is None:
            start_location = request.start_location if day_no == 1 else request.end_location
            end_location = request.end_location if day_no == trip_days else request.end_location
            planned_days.append(
                PlannedDay(
                    date=current_date,
                    day_no=day_no,
                    start_time=request.daily_start_time,
                    end_time=request.daily_end_time,
                    start_location=start_location,
                    end_location=end_location,
                )
            )
            continue
        planned_days.append(
            PlannedDay(
                date=current_date,
                day_no=day_no,
                start_time=condition.start_time,
                end_time=condition.end_time,
                start_location=condition.start_location,
                end_location=condition.end_location,
            )
        )
    return planned_days


def target_stop_counts(
    planned_days: list[PlannedDay],
    request: ScheduleCreateRequest,
) -> dict[int, int]:
    counts: dict[int, int] = {}
    for planned_day in planned_days:
        available_minutes = int(
            (
                datetime.combine(planned_day.date, planned_day.end_time)
                - datetime.combine(planned_day.date, planned_day.start_time)
            ).total_seconds()
            // 60
        )
        counts[planned_day.day_no] = policy_target_count(available_minutes, request)
    return counts


def distribute_must_visit_places(planned_days: list[PlannedDay], place_ids: list[int]) -> dict[int, list[int]]:
    if not place_ids:
        return {}
    distribution: dict[int, list[int]] = defaultdict(list)
    for index, place_id in enumerate(place_ids):
        target_day = planned_days[index % len(planned_days)].day_no
        distribution[target_day].append(place_id)
    return distribution


def build_stops(
    planned_day: PlannedDay,
    place_ids: list[int],
    target_count: int,
    request: ScheduleCreateRequest,
    fixed_events: list[FixedEventSpec],
    used_place_ids: set[int] | None = None,
) -> list[ScheduleStop]:
    fixed_event_place_ids = [event.place_id for event in fixed_events]
    must_visit_ids = dedupe_ints(place_ids + fixed_event_place_ids)
    effective_target_count = max(target_count, len(must_visit_ids))
    candidates = choose_candidate_places(
        planned_day, request, effective_target_count, must_visit_ids, used_place_ids
    )
    if not candidates:
        return []
    candidates = order_candidates_for_day(planned_day, candidates, fixed_events)

    start_dt = datetime.combine(planned_day.date, planned_day.start_time)
    end_dt = datetime.combine(planned_day.date, planned_day.end_time)
    theme_answer_id = primary_theme_answer_id(request)
    active_slots = active_meal_slots(planned_day)
    assigned_slots: set[str] = set()
    previous_point = planned_day.start_location
    cursor = start_dt
    fixed_events_by_place = {event.place_id: event for event in fixed_events}

    stops: list[ScheduleStop] = []
    for index, candidate in enumerate(candidates, start=1):
        inbound_transit = None
        if previous_point is not None:
            inbound_transit = resolve_transit(
                TransitPoint(previous_point.name, previous_point.longitude, previous_point.latitude),
                TransitPoint(candidate.name, candidate.longitude, candidate.latitude),
                "INBOUND",
                index,
            )
            cursor = cursor + timedelta(minutes=inbound_transit.total_minutes)
        fixed_event = fixed_events_by_place.get(candidate.id)
        if fixed_event is not None:
            wait_minutes = max(0, int((fixed_event.starts_at - cursor).total_seconds() // 60))
            meal_slot_code = None
            stay_minutes = int((fixed_event.ends_at - fixed_event.starts_at).total_seconds() // 60)
            arrive_dt = fixed_event.starts_at
            depart_dt = fixed_event.ends_at
        else:
            meal_slot_code, wait_minutes, cursor = align_meal_arrival(cursor, candidate, active_slots, assigned_slots)
            if meal_slot_code is not None:
                assigned_slots.add(meal_slot_code)
            stay_minutes = visit_duration_minutes(candidate)
            remaining_count = len(candidates) - index
            latest_departure = end_dt - timedelta(minutes=remaining_count * MIN_STAY_MINUTES)
            depart_dt = min(cursor + timedelta(minutes=stay_minutes), latest_departure if latest_departure > cursor else end_dt)
            if depart_dt <= cursor:
                depart_dt = min(end_dt, cursor + timedelta(minutes=MIN_STAY_MINUTES))
            arrive_dt = cursor
        stops.append(
            candidate_stop(
                order=index,
                stay_minutes=max(MIN_STAY_MINUTES, int((depart_dt - arrive_dt).total_seconds() // 60)),
                candidate=candidate,
                selection_reasons=selection_reasons(
                    candidate.id if candidate.id in must_visit_ids else None,
                    theme_answer_id,
                    request,
                    fixed_event is not None,
                ),
                arrive_at=arrive_dt.time(),
                depart_at=depart_dt.time(),
                inbound_transit=inbound_transit,
                meal_time_slot=meal_slot_code,
                waiting_minutes_before=wait_minutes,
                fixed_starts_at=fixed_event.starts_at if fixed_event is not None else None,
                fixed_ends_at=fixed_event.ends_at if fixed_event is not None else None,
            )
        )
        previous_point = ScheduleLocation(name=candidate.name, longitude=candidate.longitude, latitude=candidate.latitude)
        cursor = depart_dt
    return trim_or_extend_stops_to_feasible_window(planned_day, stops)


def candidate_stop(
    order: int,
    stay_minutes: int,
    candidate: CandidatePlace,
    selection_reasons: list[str],
    arrive_at: time | None = None,
    depart_at: time | None = None,
    inbound_transit: ScheduleTransit | None = None,
    meal_time_slot: str | None = None,
    waiting_minutes_before: int = 0,
    fixed_starts_at: datetime | None = None,
    fixed_ends_at: datetime | None = None,
) -> ScheduleStop:
    return ScheduleStop(
        id=uuid4(),
        order=order,
        arriveAt=arrive_at,
        departAt=depart_at,
        stayMinutes=stay_minutes,
        place=SchedulePlace(
            id=candidate.id,
            name=candidate.name,
            category=candidate.content_type_id,
            categoryLabel=candidate.category_label,
            address=candidate.address,
            longitude=candidate.longitude,
            latitude=candidate.latitude,
            primaryImageUrl=None,
            operatingInfo=None,
        ),
        inboundTransit=inbound_transit,
        mealTimeSlot=meal_time_slot,
        waitingMinutesBefore=waiting_minutes_before,
        selectionReasons=selection_reasons,
        fixedStartsAt=fixed_starts_at,
        fixedEndsAt=fixed_ends_at,
        warnings=[
            "운영시간과 상세 안내는 방문 전에 다시 확인해 주세요.",
        ],
    )


def recalculate_day(day: ScheduleDay, stops: list[ScheduleStop]) -> ScheduleDay:
    recalculated = day.model_copy(deep=True)
    if not recalculated.start_time or not recalculated.end_time:
        recalculated.stops = stops
        recalculated.summary = f"{len(stops)}개 방문지 (시간 재계산 대기)"
        return recalculated

    start_dt = datetime.combine(recalculated.date, recalculated.start_time)
    end_dt = datetime.combine(recalculated.date, recalculated.end_time)
    remaining_minutes = max(int((end_dt - start_dt).total_seconds() // 60), MIN_STAY_MINUTES)
    cursor = start_dt
    rebuilt: list[ScheduleStop] = []
    ordered = order_stops_for_day(day, stops)
    active_slots = active_meal_slots(planned_day_from_schedule_day(day))
    assigned_slots: set[str] = set()
    previous_name = day.start_location.name if day.start_location else "출발지"
    previous_lon = day.start_location.longitude if day.start_location else Decimal("129.0403")
    previous_lat = day.start_location.latitude if day.start_location else Decimal("35.1151")
    remaining_stops = len(ordered)
    for stop in ordered:
        remaining_stops -= 1
        inbound_transit = resolve_transit(
            TransitPoint(previous_name, previous_lon, previous_lat),
            TransitPoint(
                stop.place.name,
                stop.place.longitude or previous_lon,
                stop.place.latitude or previous_lat,
            ),
            "INBOUND",
            stop.order,
        )
        cursor = cursor + timedelta(minutes=inbound_transit.total_minutes)
        meal_slot_code, wait_minutes, cursor = align_meal_arrival(
            cursor,
            CandidatePlace(
                id=stop.place.id or 0,
                external_id=str(stop.place.id or 0),
                content_type_id=stop.place.category or "12",
                name=stop.place.name,
                category_label=stop.place.category_label,
                address=stop.place.address,
                longitude=stop.place.longitude or previous_lon,
                latitude=stop.place.latitude or previous_lat,
            ),
            active_slots,
            assigned_slots,
        )
        if meal_slot_code is not None:
            assigned_slots.add(meal_slot_code)
        max_available = max(MIN_STAY_MINUTES, remaining_minutes - remaining_stops * MIN_STAY_MINUTES)
        stay_minutes = min(max(stop.stay_minutes, MIN_STAY_MINUTES), max_available)
        depart_dt = min(cursor + timedelta(minutes=stay_minutes), end_dt)
        rebuilt_stop = stop.model_copy(deep=True)
        rebuilt_stop.arrive_at = cursor.time()
        rebuilt_stop.depart_at = depart_dt.time()
        rebuilt_stop.stay_minutes = int((depart_dt - cursor).total_seconds() // 60)
        rebuilt_stop.inbound_transit = inbound_transit
        rebuilt_stop.meal_time_slot = meal_slot_code
        rebuilt_stop.waiting_minutes_before = wait_minutes
        rebuilt_stop.warnings = dedupe_warnings(
            rebuilt_stop.warnings + ["방문 순서 변경에 따라 도착 및 출발 시간이 다시 계산되었습니다."]
        )
        rebuilt.append(rebuilt_stop)
        remaining_minutes = max(int((end_dt - depart_dt).total_seconds() // 60), 0)
        previous_name = stop.place.name
        previous_lon = stop.place.longitude or previous_lon
        previous_lat = stop.place.latitude or previous_lat
        cursor = depart_dt
    recalculated.stops = rebuilt
    recalculated.summary = build_stop_summary(rebuilt)
    recalculated.final_transit = build_final_transit(planned_day_from_schedule_day(recalculated), rebuilt)
    return recalculated


def to_day_location(location: ScheduleLocation) -> DayLocation:
    return DayLocation(name=location.name, longitude=location.longitude, latitude=location.latitude)


def build_day_summary(planned_day: PlannedDay, place_ids: list[int]) -> str:
    if not place_ids:
        return (
            f"{planned_day.start_location.name} 출발, {planned_day.end_location.name} 도착. "
            "방문지를 추가하면 이동 동선을 반영해 일정을 구성합니다."
        )
    themed = [stop.place.category_label for stop in place_ids if stop.place.category_label]
    summary = f"{len(place_ids)}개 방문지 일정"
    if themed:
        summary += f" ({themed[0]} 중심)"
    return summary


def build_style_summary(request: ScheduleCreateRequest) -> str:
    answer_ids = [answer.answer_id for answer in request.selected_answers]
    if not answer_ids:
        return "추천 일정"
    return " / ".join(answer_ids[:5])


def build_stop_summary(stops: list[ScheduleStop]) -> str:
    if not stops:
        return "방문지 없음"
    categories = [stop.place.category_label for stop in stops if stop.place.category_label]
    if categories:
        return f"{len(stops)}개 방문지 일정 ({categories[0]} 중심)"
    return f"{len(stops)}개 방문지 일정"


def order_candidates_for_day(
    planned_day: PlannedDay,
    candidates: list[CandidatePlace],
    fixed_events: list[FixedEventSpec] | None = None,
) -> list[CandidatePlace]:
    if len(candidates) <= 1:
        return candidates
    fixed_event_positions = {
        event.place_id: event.starts_at
        for event in (fixed_events or [])
    }
    if fixed_event_positions:
        fixed_candidates = [candidate for candidate in candidates if candidate.id in fixed_event_positions]
        flexible_candidates = [candidate for candidate in candidates if candidate.id not in fixed_event_positions]
        ordered_flexible = order_candidates_for_day(planned_day, flexible_candidates, [])
        merged = ordered_flexible[:]
        for candidate in sorted(fixed_candidates, key=lambda item: fixed_event_positions[item.id]):
            insert_at = min(len(merged), max(0, len(merged) // 2))
            merged.insert(insert_at, candidate)
        return merged[: len(candidates)]
    meal_candidates = [candidate for candidate in candidates if is_meal_candidate(candidate)]
    other_candidates = [candidate for candidate in candidates if not is_meal_candidate(candidate)]
    ordered_non_meals = nearest_neighbor_order(
        planned_day.start_location.longitude,
        planned_day.start_location.latitude,
        other_candidates,
    )
    ordered: list[CandidatePlace] = ordered_non_meals[:]
    if meal_candidates:
        meal_order = nearest_neighbor_order(
            planned_day.start_location.longitude,
            planned_day.start_location.latitude,
            meal_candidates,
        )
        positions = desired_meal_positions(len(candidates), len(meal_order), planned_day)
        for position, candidate in zip(positions, meal_order, strict=False):
            insert_at = min(max(0, position), len(ordered))
            ordered.insert(insert_at, candidate)
    remaining = [candidate for candidate in candidates if candidate not in ordered]
    ordered.extend(remaining)
    return ordered[: len(candidates)]


def order_stops_for_day(day: ScheduleDay, stops: list[ScheduleStop]) -> list[ScheduleStop]:
    if len(stops) <= 1:
        return sorted(stops, key=lambda stop: stop.order)
    start_lon = day.start_location.longitude if day.start_location and day.start_location.longitude else Decimal("129.0403")
    start_lat = day.start_location.latitude if day.start_location and day.start_location.latitude else Decimal("35.1151")
    remaining = sorted(stops, key=lambda stop: stop.order)
    ordered: list[ScheduleStop] = []
    current_lon = start_lon
    current_lat = start_lat
    while remaining:
        next_stop = min(
            remaining,
            key=lambda stop: distance_meters(
                current_lon,
                current_lat,
                stop.place.longitude or current_lon,
                stop.place.latitude or current_lat,
            ),
        )
        ordered.append(next_stop)
        remaining.remove(next_stop)
        current_lon = next_stop.place.longitude or current_lon
        current_lat = next_stop.place.latitude or current_lat
    for index, stop in enumerate(ordered, start=1):
        stop.order = index
    return ordered


def nearest_neighbor_order(
    start_lon: Decimal,
    start_lat: Decimal,
    candidates: list[CandidatePlace],
) -> list[CandidatePlace]:
    remaining = candidates[:]
    ordered: list[CandidatePlace] = []
    current_lon = start_lon
    current_lat = start_lat
    while remaining:
        next_candidate = min(
            remaining,
            key=lambda candidate: distance_meters(
                current_lon,
                current_lat,
                candidate.longitude,
                candidate.latitude,
            ),
        )
        ordered.append(next_candidate)
        remaining.remove(next_candidate)
        current_lon = next_candidate.longitude
        current_lat = next_candidate.latitude
    return ordered


def desired_meal_positions(total_count: int, meal_count: int, planned_day: PlannedDay) -> list[int]:
    slots = active_meal_slots(planned_day)
    if not slots or meal_count <= 0:
        return []
    if len(slots) >= 2 and meal_count >= 2 and total_count >= 4:
        return [1, max(2, total_count - 1)][:meal_count]
    return [max(1, total_count // 2)][:meal_count]


def active_meal_slots(planned_day: PlannedDay) -> list[MealSlot]:
    active: list[MealSlot] = []
    for slot in MEAL_SLOTS:
        overlap_start = max_time(planned_day.start_time, slot.start)
        overlap_end = min_time(planned_day.end_time, slot.end)
        if overlap_end > overlap_start and int((datetime.combine(planned_day.date, overlap_end) - datetime.combine(planned_day.date, overlap_start)).total_seconds() // 60) >= 45:
            active.append(slot)
    return active


def align_meal_arrival(
    cursor: datetime,
    candidate: CandidatePlace,
    active_slots: list[MealSlot],
    assigned_slots: set[str],
) -> tuple[str | None, int, datetime]:
    if not is_meal_candidate(candidate):
        return None, 0, cursor
    for slot in active_slots:
        if slot.code in assigned_slots:
            continue
        slot_start = datetime.combine(cursor.date(), slot.start)
        slot_end = datetime.combine(cursor.date(), slot.end)
        if cursor > slot_end:
            continue
        aligned = max(cursor, slot_start)
        wait_minutes = int((aligned - cursor).total_seconds() // 60)
        if wait_minutes > max_early_wait_minutes(slot.code):
            continue
        return slot.code, wait_minutes, aligned
    return None, 0, cursor


def max_early_wait_minutes(slot_code: str) -> int:
    if slot_code == "DINNER":
        return 90
    return 60


def is_meal_candidate(candidate: CandidatePlace) -> bool:
    if candidate.content_type_id == "39":
        return True
    return contains_any(candidate.category_label, "음식", "식당", "카페", "베이커리") or contains_any(
        candidate.name, "맛집", "식당", "카페", "커피", "베이커리"
    )


def visit_duration_minutes(candidate: CandidatePlace) -> int:
    if candidate.content_type_id == "15":
        return 90
    if candidate.content_type_id == "39":
        return 75
    return 60


def estimate_transit_minutes(
    start_lon: Decimal,
    start_lat: Decimal,
    end_lon: Decimal,
    end_lat: Decimal,
) -> int:
    distance = distance_meters(start_lon, start_lat, end_lon, end_lat)
    return max(5, int(round(distance / 900)))


def build_inbound_transit(origin_name: str | None, destination_name: str, transit_minutes: int) -> ScheduleTransit | None:
    if origin_name is None:
        return None
    return ScheduleTransit(
        routeType="PLACEHOLDER",
        routeOrder=0,
        originName=origin_name,
        destinationName=destination_name,
        summary=f"{origin_name} -> {destination_name}",
        departAt=None,
        arriveAt=None,
        totalMinutes=transit_minutes,
        walkMinutes=transit_minutes,
        waitMinutes=0,
        transferCount=0,
        fareAmount=None,
        provider="FASTAPI_MIGRATION",
        realtimeStatus="UNAVAILABLE",
        fallbackUsed=True,
        segments=[],
        warnings=["일부 이동 시간은 직선 거리 기반 추정치가 사용되었습니다."],
    )


def build_final_transit(planned_day: PlannedDay, stops: list[ScheduleStop]) -> ScheduleTransit | None:
    if planned_day.end_location is None:
        return None
    if stops:
        origin = stops[-1].place
        origin_name = origin.name
        origin_lon = origin.longitude or planned_day.start_location.longitude
        origin_lat = origin.latitude or planned_day.start_location.latitude
    else:
        origin_name = planned_day.start_location.name
        origin_lon = planned_day.start_location.longitude
        origin_lat = planned_day.start_location.latitude
    return resolve_transit(
        TransitPoint(origin_name, origin_lon, origin_lat),
        TransitPoint(planned_day.end_location.name, planned_day.end_location.longitude, planned_day.end_location.latitude),
        "FINAL",
        len(stops) + 1,
    )


def max_time(left: time, right: time) -> time:
    return left if left >= right else right


def min_time(left: time, right: time) -> time:
    return left if left <= right else right


def interpolate_point(
    start_lon: Decimal,
    start_lat: Decimal,
    end_lon: Decimal,
    end_lat: Decimal,
    index: int,
    total: int,
) -> tuple[Decimal, Decimal]:
    if total <= 0:
        return start_lon, start_lat
    ratio = Decimal(index) / Decimal(total + 1)
    return (
        start_lon + (end_lon - start_lon) * ratio,
        start_lat + (end_lat - start_lat) * ratio,
    )


def build_coordinates(
    start_lon: Decimal | None,
    start_lat: Decimal | None,
    end_lon: Decimal | None,
    end_lat: Decimal | None,
) -> list[list[Decimal]]:
    if None in {start_lon, start_lat, end_lon, end_lat}:
        return []
    return [[start_lon, start_lat], [end_lon, end_lat]]


def dedupe_warnings(warnings: list[str]) -> list[str]:
    return list(dict.fromkeys(warnings))


def resolve_transit(
    origin: TransitPoint,
    destination: TransitPoint,
    route_type: str,
    route_order: int,
) -> ScheduleTransit:
    try:
        transit, _ = find_route(origin, destination, route_type, route_order)
        return transit
    except Exception:
        minutes = estimate_transit_minutes(
            origin.longitude,
            origin.latitude,
            destination.longitude,
            destination.latitude,
        )
        fallback = build_inbound_transit(origin.name, destination.name, minutes)
        if fallback is None:
            raise
        fallback.route_type = route_type
        fallback.route_order = route_order
        return fallback


def route_lines_for_transit(
    day_no: int,
    route_order: int,
    transit: ScheduleTransit | None,
    start_name: str | None,
    end_name: str | None,
    start_lon: Decimal | None,
    start_lat: Decimal | None,
    end_lon: Decimal | None,
    end_lat: Decimal | None,
) -> list[RouteLine]:
    if transit is None:
        return [
            RouteLine(
                dayNo=day_no,
                routeOrder=route_order,
                lineOrder=1,
                mode="PLACEHOLDER",
                lineName="이동 경로 안내",
                startName=start_name,
                endName=end_name,
                durationMinutes=None,
                distanceMeters=None,
                instruction="상세 대중교통 경로를 불러오지 못해 기본 연결선으로 표시합니다.",
                fallbackUsed=True,
                coordinates=build_coordinates(start_lon, start_lat, end_lon, end_lat),
            )
        ]
    route_lines = getattr(transit, "route_lines", None)
    if route_lines:
        output: list[RouteLine] = []
        for index, line in enumerate(route_lines, start=1):
            payload = dict(line)
            payload["dayNo"] = day_no
            payload["routeOrder"] = route_order
            payload["lineOrder"] = index
            output.append(RouteLine.model_validate(payload))
        return output
    if transit.segments:
        lines: list[RouteLine] = []
        current_start = [start_lon, start_lat]
        for index, segment in enumerate(transit.segments, start=1):
            coordinates = build_coordinates(
                current_start[0],
                current_start[1],
                end_lon if index == len(transit.segments) else current_start[0],
                end_lat if index == len(transit.segments) else current_start[1],
            )
            lines.append(
                RouteLine(
                    dayNo=day_no,
                    routeOrder=route_order,
                    lineOrder=index,
                    mode=segment.mode,
                    lineName=segment.line_name,
                    startName=segment.start_station_name or start_name,
                    endName=segment.end_station_name or end_name,
                    durationMinutes=segment.duration_minutes,
                    distanceMeters=segment.distance_meters,
                    instruction=segment.instruction,
                    fallbackUsed=transit.fallback_used,
                    coordinates=coordinates,
                )
            )
        return lines
    return [
        RouteLine(
            dayNo=day_no,
            routeOrder=route_order,
            lineOrder=1,
            mode=transit.route_type or "PLACEHOLDER",
            lineName=transit.summary,
            startName=start_name,
            endName=end_name,
            durationMinutes=transit.total_minutes,
            distanceMeters=None,
            instruction=transit.summary,
            fallbackUsed=transit.fallback_used,
            coordinates=build_coordinates(start_lon, start_lat, end_lon, end_lat),
        )
    ]


def resolve_route_coverage(days: list[ScheduleDay]) -> str:
    if any(day.final_transit is None for day in days):
        return "SKELETON_ONLY"
    return "ATTRACTION_ROUTES_ONLY"


def planned_day_from_schedule_day(day: ScheduleDay) -> PlannedDay:
    start_location = day.start_location or DayLocation(name="출발지", longitude=Decimal("129.0403"), latitude=Decimal("35.1151"))
    end_location = day.end_location or start_location
    return PlannedDay(
        date=day.date,
        day_no=day.day_no,
        start_time=day.start_time or time(9, 0),
        end_time=day.end_time or time(18, 0),
        start_location=ScheduleLocation(
            name=start_location.name,
            longitude=start_location.longitude or Decimal("129.0403"),
            latitude=start_location.latitude or Decimal("35.1151"),
        ),
        end_location=ScheduleLocation(
            name=end_location.name,
            longitude=end_location.longitude or Decimal("129.0403"),
            latitude=end_location.latitude or Decimal("35.1151"),
        ),
    )


def request_context_from_schedule(schedule: ScheduleResponse) -> ScheduleCreateRequest:
    selected_answers: list[dict[str, str]] = []
    for token in (schedule.style_summary or "").split(" / "):
        normalized = token.strip()
        if not normalized:
            continue
        if normalized.startswith("THEME_"):
            selected_answers.append({"questionId": "THEME", "answerId": normalized})
        elif normalized.startswith("PACE_"):
            selected_answers.append({"questionId": "PACE", "answerId": normalized})
        elif normalized.startswith("MOBILITY_"):
            selected_answers.append({"questionId": "MOBILITY", "answerId": normalized})
        elif normalized.startswith("TRANSIT_"):
            selected_answers.append({"questionId": "TRANSIT", "answerId": normalized})

    first_day = schedule.days[0]
    last_day = schedule.days[-1]
    start_location = first_day.start_location or DayLocation(name="출발지", longitude=Decimal("129.0403"), latitude=Decimal("35.1151"))
    end_location = last_day.end_location or start_location
    return ScheduleCreateRequest.model_validate(
        {
            "startDate": str(schedule.start_date),
            "endDate": str(schedule.end_date),
            "dailyStartTime": (schedule.daily_start_time or time(9, 0)).isoformat(),
            "dailyEndTime": (schedule.daily_end_time or time(18, 0)).isoformat(),
            "startLocation": {
                "name": start_location.name,
                "longitude": start_location.longitude or Decimal("129.0403"),
                "latitude": start_location.latitude or Decimal("35.1151"),
            },
            "endLocation": {
                "name": end_location.name,
                "longitude": end_location.longitude or Decimal("129.0403"),
                "latitude": end_location.latitude or Decimal("35.1151"),
            },
            "selectedAnswers": selected_answers or [{"questionId": "THEME", "answerId": "THEME_NATURE"}],
            "mustVisitPlaceIds": [],
        }
    )


def policy_target_count(available_minutes: int, request: ScheduleCreateRequest) -> int:
    if available_minutes <= 120:
        return 1
    if available_minutes < 240:
        return 2
    if has_answer(request, "PACE_PACKED"):
        if available_minutes >= 480:
            return 5
        if available_minutes >= 360:
            return 4
        return 3
    if has_answer(request, "PACE_RELAXED"):
        if available_minutes >= 360:
            return 3
        return 3
    if available_minutes >= 480:
        return 4
    if available_minutes >= 360:
        return 4
    return 3


def primary_theme_answer_id(request: ScheduleCreateRequest) -> str | None:
    for answer in request.selected_answers:
        if answer.question_id == "THEME":
            return answer.answer_id
    return None


def has_answer(request: ScheduleCreateRequest, answer_id: str) -> bool:
    return any(answer.answer_id == answer_id for answer in request.selected_answers)


def placeholder_place_name(theme_answer_id: str | None, index: int, place_id: int | None) -> str:
    if place_id is not None:
        return f"Place {place_id}"
    names = THEME_PLACEHOLDER_NAMES.get(theme_answer_id or "", [])
    if not names:
        return f"추천 방문지 {index}"
    return names[(index - 1) % len(names)]


def placeholder_category(theme_answer_id: str | None) -> tuple[str | None, str]:
    return THEME_CATEGORY_LABELS.get(theme_answer_id or "", (None, "미확인"))


def selection_reasons(
    place_id: int | None,
    theme_answer_id: str | None,
    request: ScheduleCreateRequest,
    is_fixed_event: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if place_id is not None:
        reasons.append("must_visit_seed")
    else:
        reasons.append("daily_target_fill")
    if is_fixed_event:
        reasons.append("fixed_event")
    if theme_answer_id is not None:
        reasons.append(f"theme:{theme_answer_id}")
    if has_answer(request, "PACE_PACKED"):
        reasons.append("pace:packed")
    if has_answer(request, "PACE_RELAXED"):
        reasons.append("pace:relaxed")
    return reasons


def resolve_place_or_400(place_id: int) -> CandidatePlace:
    """장소 id 하나를 후보 풀 또는 DB 에서 해석한다.

    후보 풀은 모듈 로드 시 한 번만 채워지므로 그 뒤 등록된 장소는 들어 있지 않다.
    존재하지 않는 장소로 일정을 만들면 저장 단계에서 반드시 실패하므로,
    가짜 장소를 지어내지 않고 400 으로 거절한다.
    """
    for candidate in current_candidate_pool():
        if candidate.id == place_id:
            return candidate
    resolved = (load_candidate_places_by_ids([place_id]) or {}).get(place_id)
    if resolved is None:
        raise HTTPException(status_code=400, detail=f"placeId {place_id} does not exist")
    return resolved


def choose_candidate_places(
    planned_day: PlannedDay,
    request: ScheduleCreateRequest,
    target_count: int,
    must_visit_ids: list[int],
    used_place_ids: set[int] | None = None,
) -> list[CandidatePlace]:
    """한 일차의 방문지를 고른다.

    used_place_ids 는 앞선 일차에서 이미 쓴 장소다. 이 값을 받지 않으면 일차마다
    같은 후보 풀을 같은 기준으로 정렬해 같은 상위 N개를 골라, 2일 일정의 모든 날이
    똑같은 장소로 채워진다.

    후보가 모자라면 재사용을 허용한다. 같은 장소가 반복되는 편이 빈 일차보다 낫다.
    """
    resolved: list[CandidatePlace] = []
    seen_ids: set[int] = set()
    already_used = set(used_place_ids or ())
    selected_experience_types: list[str] = []
    selected_semantic_groups: list[str] = []
    # 후보 풀은 모듈 로드 시 한 번만 채워지므로 그 뒤 등록된 장소는 들어 있지 않다.
    # 풀에 없는 id 만 DB 에서 한 번에 읽어온다.
    candidate_pool = current_candidate_pool()
    pool_by_id = {item.id: item for item in candidate_pool}
    missing_ids = [pid for pid in must_visit_ids if pid not in pool_by_id]
    resolved_from_db = load_candidate_places_by_ids(missing_ids) or {}

    for place_id in must_visit_ids:
        candidate = pool_by_id.get(place_id) or resolved_from_db.get(place_id)
        if candidate is None:
            # 존재하지 않는 장소로 일정을 만들면 저장 단계에서 반드시 실패한다.
            # 가짜 장소를 지어내지 않고 요청을 거절한다.
            raise HTTPException(
                status_code=400,
                detail=f"mustVisitPlaceId {place_id} does not exist",
            )
        resolved.append(candidate)
        seen_ids.add(candidate.id)
        profile = classify_place(candidate.name, candidate.category_label, candidate.content_type_id)
        selected_experience_types.append(profile.experience_type)
        selected_semantic_groups.append(profile.semantic_group)

    remaining = max(0, target_count - len(resolved))
    if remaining == 0:
        return resolved[:target_count]

    theme_answer_id = primary_theme_answer_id(request)
    ranked = sorted(
        (
            candidate
            for candidate in candidate_pool
            if candidate.id not in seen_ids and not is_day_endpoint(candidate, planned_day)
        ),
        key=lambda candidate: candidate_rank(candidate, planned_day, request, theme_answer_id),
    )
    # 앞선 일차에서 쓴 장소는 뒤로 미룬다. 완전히 배제하지 않는 이유는 후보 풀이
    # 작을 때 일차가 비는 것을 막기 위해서다.
    unused_first = [c for c in ranked if c.id not in already_used]
    reusable = [c for c in ranked if c.id in already_used]
    ranked = unused_first + reusable
    for candidate in ranked:
        if len(resolved) >= target_count:
            break
        profile = classify_place(candidate.name, candidate.category_label, candidate.content_type_id)
        if exceeds_strong_preference_diversity(
            profile.experience_type,
            profile.semantic_group,
            selected_experience_types,
            selected_semantic_groups,
        ):
            continue
        resolved.append(candidate)
        seen_ids.add(candidate.id)
        selected_experience_types.append(profile.experience_type)
        selected_semantic_groups.append(profile.semantic_group)
    if len(resolved) < target_count:
        for candidate in ranked:
            if len(resolved) >= target_count:
                break
            if candidate.id in seen_ids:
                continue
            resolved.append(candidate)
            seen_ids.add(candidate.id)
    return resolved[:target_count]


SAME_PLACE_RADIUS_METERS = 300
COINCIDENT_RADIUS_METERS = 30


def normalized_place_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"[\s·・()\[\]{},\-]", "", name).lower()


def is_day_endpoint(candidate: CandidatePlace, planned_day: PlannedDay) -> bool:
    """후보가 그 일차의 출발지 또는 도착지와 같은 장소인지.

    출발지에 도착해 체류하는 방문지는 의미가 없는데, candidate_rank 의 마지막 정렬
    기준이 출발·도착지까지의 최단 거리라서 출발지가 장소로 등록돼 있으면 거리 0 으로
    항상 1순위가 됐다. 실제로 "부산역"에서 출발하는 일정의 첫 방문지가 60분 체류하는
    "부산역"이었다.

    좌표만으로 판단하지 않는 이유는 같은 장소라도 출처에 따라 좌표가 100m 넘게
    차이나기 때문이다. 반대로 이름만으로 판단하면 지점명이 같은 다른 장소를 지운다.
    """
    for endpoint in (planned_day.start_location, planned_day.end_location):
        if endpoint is None:
            continue
        distance = distance_meters(
            endpoint.longitude, endpoint.latitude, candidate.longitude, candidate.latitude
        )
        if distance <= COINCIDENT_RADIUS_METERS:
            return True
        if distance <= SAME_PLACE_RADIUS_METERS and normalized_place_name(
            endpoint.name
        ) == normalized_place_name(candidate.name):
            return True
    return False


def candidate_rank(
    candidate: CandidatePlace,
    planned_day: PlannedDay,
    request: ScheduleCreateRequest,
    theme_answer_id: str | None,
) -> tuple[int, int, int, int, int, float]:
    theme_penalty = theme_penalty_score(candidate, theme_answer_id)
    pace_penalty = pace_penalty_score(candidate, request)
    mobility_penalty = mobility_penalty_score(candidate, request)
    transit_penalty = transfer_penalty_score(candidate, planned_day, request)
    content_priority = content_type_priority(candidate.content_type_id)
    distance = min(
        distance_meters(
            planned_day.start_location.longitude,
            planned_day.start_location.latitude,
            candidate.longitude,
            candidate.latitude,
        ),
        distance_meters(
            planned_day.end_location.longitude,
            planned_day.end_location.latitude,
            candidate.longitude,
            candidate.latitude,
        ),
    )
    return (theme_penalty, mobility_penalty, transit_penalty, pace_penalty, content_priority, distance)


def low_mobility_profile(request: ScheduleCreateRequest) -> bool:
    return any(
        has_answer(request, answer_id)
        for answer_id in (
            "COMPANION_PARENTS",
            "COMPANION_FAMILY_WITH_CHILD",
            "MOBILITY_LOW_WALK",
            "MOBILITY_AVOID_HILLS_STAIRS",
        )
    )



def trim_or_extend_stops_to_feasible_window(
    planned_day: PlannedDay,
    stops: list[ScheduleStop],
) -> list[ScheduleStop]:
    if not stops:
        return stops
    start_dt = datetime.combine(planned_day.date, planned_day.start_time)
    end_dt = datetime.combine(planned_day.date, planned_day.end_time)
    total_minutes = 0
    for stop in stops:
        total_minutes += stop.stay_minutes + (stop.inbound_transit.total_minutes if stop.inbound_transit else 0)
        total_minutes += stop.waiting_minutes_before
    available_minutes = int((end_dt - start_dt).total_seconds() // 60)
    if total_minutes <= available_minutes:
        return stops

    overrun = total_minutes - available_minutes
    adjusted = [stop.model_copy(deep=True) for stop in stops]
    for stop in reversed(adjusted):
        if overrun <= 0:
            break
        if stop.fixed_starts_at is not None:
            continue
        reducible = max(0, stop.stay_minutes - MIN_STAY_MINUTES)
        reduction = min(reducible, overrun)
        if reduction > 0:
            stop.stay_minutes -= reduction
            if stop.depart_at and stop.arrive_at:
                arrive_dt = datetime.combine(planned_day.date, stop.arrive_at)
                stop.depart_at = (arrive_dt + timedelta(minutes=stop.stay_minutes)).time()
            stop.warnings = dedupe_warnings(stop.warnings + ["하루 일정 안에 맞추기 위해 일부 체류 시간이 조정되었습니다."])
            overrun -= reduction
    return adjusted


def dedupe_ints(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))


def repair_schedule_days(
    planned_days: list[PlannedDay],
    days: list[ScheduleDay],
    target_counts: dict[int, int],
    request: ScheduleCreateRequest,
) -> tuple[list[ScheduleDay], list[str]]:
    repaired = [day.model_copy(deep=True) for day in days]
    warnings: list[str] = []
    changed = True
    while changed:
        changed = False
        for index, day in enumerate(repaired):
            overrun = day_overrun_minutes(day)
            if overrun <= 0:
                continue
            repaired_day = recalculate_day(day, day.stops)
            if day_overrun_minutes(repaired_day) <= 0:
                repaired[index] = repaired_day
                warnings.append("일정 시간을 맞추기 위해 일부 방문지 체류 시간이 조정되었습니다.")
                changed = True
                continue
            moved = move_last_optional_stop(
                repaired,
                index,
                planned_days,
                target_counts,
            )
            if moved:
                warnings.append("일정 시간을 맞추기 위해 일부 방문지가 다른 날짜로 이동되었습니다.")
                changed = True
        for index, day in enumerate(repaired):
            expanded = try_expand_underfilled_day(
                repaired,
                index,
                planned_days[index],
                target_counts,
                request,
            )
            if expanded is not None:
                repaired[index] = expanded
                warnings.append("하루 활동 시간이 너무 짧지 않도록 방문지를 추가로 배치했습니다.")
                changed = True
    for index, day in enumerate(repaired):
        repaired[index] = recalculate_day(day, day.stops)
    return repaired, dedupe_warnings(warnings)


def day_overrun_minutes(day: ScheduleDay) -> int:
    if not day.start_time or not day.end_time:
        return 0
    available = int(
        (
            datetime.combine(day.date, day.end_time)
            - datetime.combine(day.date, day.start_time)
        ).total_seconds() // 60
    )
    planned = sum(
        stop.stay_minutes
        + stop.waiting_minutes_before
        + (stop.inbound_transit.total_minutes if stop.inbound_transit else 0)
        for stop in day.stops
    )
    if day.final_transit is not None:
        planned += day.final_transit.total_minutes
    return max(0, planned - available)


def day_used_minutes(day: ScheduleDay) -> int:
    planned = sum(
        stop.stay_minutes
        + stop.waiting_minutes_before
        + (stop.inbound_transit.total_minutes if stop.inbound_transit else 0)
        for stop in day.stops
    )
    if day.final_transit is not None:
        planned += day.final_transit.total_minutes
    return planned


def day_available_minutes(day: ScheduleDay) -> int:
    if not day.start_time or not day.end_time:
        return 0
    return int(
        (
            datetime.combine(day.date, day.end_time)
            - datetime.combine(day.date, day.start_time)
        ).total_seconds() // 60
    )


def day_underfilled(day: ScheduleDay) -> bool:
    available = day_available_minutes(day)
    if available <= 0:
        return False
    used = day_used_minutes(day)
    unused = available - used
    return unused >= EARLY_FINISH_BUFFER_MINUTES and used < int(available * MIN_DAY_UTILIZATION_RATIO)


def move_last_optional_stop(
    repaired: list[ScheduleDay],
    source_index: int,
    planned_days: list[PlannedDay],
    target_counts: dict[int, int],
) -> bool:
    source_day = repaired[source_index]
    movable_stops = [
        stop for stop in reversed(source_day.stops)
        if stop.fixed_starts_at is None and "must_visit_seed" not in stop.selection_reasons
    ]
    if not movable_stops:
        return False
    stop_to_move = movable_stops[0]
    for target_index, target_day in enumerate(repaired):
        if target_index == source_index:
            continue
        if len(target_day.stops) >= max(target_counts.get(target_day.day_no, 1), MAX_STOPS_PER_DAY):
            continue
        candidate_stops = target_day.stops + [stop_to_move.model_copy(deep=True)]
        recalc_target = recalculate_day(target_day, candidate_stops)
        if day_overrun_minutes(recalc_target) > 0:
            continue
        remaining_source = [stop for stop in source_day.stops if stop.id != stop_to_move.id]
        repaired[source_index] = recalculate_day(source_day, remaining_source)
        repaired[target_index] = recalc_target
        return True
    return False


def fixed_event_specs_from_day(day: ScheduleDay) -> list[FixedEventSpec]:
    specs: list[FixedEventSpec] = []
    for stop in day.stops:
        if stop.fixed_starts_at is None or stop.fixed_ends_at is None or stop.place.id is None:
            continue
        specs.append(
            FixedEventSpec(
                place_id=stop.place.id,
                name=stop.place.name,
                starts_at=stop.fixed_starts_at,
                ends_at=stop.fixed_ends_at,
            )
        )
    return specs


def try_expand_underfilled_day(
    repaired: list[ScheduleDay],
    index: int,
    planned_day: PlannedDay,
    target_counts: dict[int, int],
    request: ScheduleCreateRequest,
) -> ScheduleDay | None:
    current_day = repaired[index]
    if not day_underfilled(current_day):
        return None
    if len(current_day.stops) >= MAX_STOPS_PER_DAY:
        return None

    current_place_ids = [
        stop.place.id for stop in current_day.stops
        if stop.place is not None and stop.place.id is not None
    ]
    used_elsewhere = {
        stop.place.id
        for day_idx, day in enumerate(repaired)
        if day_idx != index
        for stop in day.stops
        if stop.place is not None and stop.place.id is not None
    }
    fixed_events = fixed_event_specs_from_day(current_day)
    attempt_target = max(target_counts.get(current_day.day_no, len(current_place_ids)), len(current_place_ids))
    best_day = current_day
    best_used = day_used_minutes(current_day)

    while attempt_target < MAX_STOPS_PER_DAY:
        attempt_target += 1
        rebuilt_stops = build_stops(
            planned_day,
            current_place_ids,
            attempt_target,
            request,
            fixed_events,
            used_elsewhere,
        )
        if len(rebuilt_stops) <= len(best_day.stops):
            continue
        rebuilt_day = current_day.model_copy(deep=True)
        rebuilt_day.stops = rebuilt_stops
        rebuilt_day.summary = build_stop_summary(rebuilt_stops)
        rebuilt_day.final_transit = build_final_transit(planned_day_from_schedule_day(rebuilt_day), rebuilt_stops)
        rebuilt_day = recalculate_day(rebuilt_day, rebuilt_stops)
        if day_overrun_minutes(rebuilt_day) > 0:
            continue
        rebuilt_used = day_used_minutes(rebuilt_day)
        if rebuilt_used <= best_used:
            continue
        best_day = rebuilt_day
        best_used = rebuilt_used
        if not day_underfilled(rebuilt_day):
            break

    if best_day is current_day:
        return None
    return best_day


def distance_meters(
    start_lon: Decimal,
    start_lat: Decimal,
    end_lon: Decimal,
    end_lat: Decimal,
) -> float:
    lon1 = float(start_lon)
    lat1 = float(start_lat)
    lon2 = float(end_lon)
    lat2 = float(end_lat)
    from math import asin, cos, radians, sin, sqrt

    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    start_lat_rad = radians(lat1)
    end_lat_rad = radians(lat2)
    haversine = sin(delta_lat / 2) ** 2 + cos(start_lat_rad) * cos(end_lat_rad) * sin(delta_lon / 2) ** 2
    return 2 * 6_371_000.0 * asin(sqrt(haversine))


def contains_any(value: str | None, *tokens: str) -> bool:
    normalized = (value or "").lower()
    return any(token.lower() in normalized for token in tokens)


def exceeds_strong_preference_diversity(
    experience_type: str,
    semantic_group: str,
    selected_experience_types: list[str],
    selected_semantic_groups: list[str],
) -> bool:
    same_type_count = sum(1 for current in selected_experience_types if current == experience_type)
    same_group_count = sum(1 for current in selected_semantic_groups if current == semantic_group)
    return same_type_count >= 2 or same_group_count >= 3


def candidate_themes(candidate: CandidatePlace) -> list[str]:
    ordered: list[str] = []
    for theme in THEME_CONTENT_TYPE_RULES.get(candidate.content_type_id, []):
        if theme not in ordered:
            ordered.append(theme)
    text = normalize_candidate_text(candidate)
    for theme, keywords in THEME_KEYWORD_RULES.items():
        if any(keyword.lower() in text for keyword in keywords) and theme not in ordered:
            ordered.append(theme)
    return ordered


def theme_penalty_score(candidate: CandidatePlace, theme_answer_id: str | None) -> int:
    if theme_answer_id is None:
        return 0
    themes = candidate_themes(candidate)
    if theme_answer_id in themes:
        return 0
    if theme_answer_id == "THEME_HEALING" and any(theme in themes for theme in ("THEME_NATURE", "THEME_FOOD")):
        return 1
    return 3


def pace_penalty_score(candidate: CandidatePlace, request: ScheduleCreateRequest) -> int:
    if has_answer(request, "PACE_PACKED"):
        if candidate.content_type_id in {"12", "15", "28"}:
            return 0
        if candidate.content_type_id == "39":
            return 2
        return 1
    if has_answer(request, "PACE_RELAXED"):
        if candidate.content_type_id in {"12", "14", "39"}:
            return 0
        return 1
    return 0


def mobility_penalty_score(candidate: CandidatePlace, request: ScheduleCreateRequest) -> int:
    if not low_mobility_profile(request):
        return 0
    return 3 if mobility_burden(candidate) else 0


def mobility_burden(candidate: CandidatePlace) -> bool:
    return contains_any(candidate.name, "감천", "달맞이", "전망대", "계단", "언덕")


def transfer_penalty_score(
    candidate: CandidatePlace,
    planned_day: PlannedDay,
    request: ScheduleCreateRequest,
) -> int:
    start_gap = neighborhood_gap(candidate.longitude, candidate.latitude, planned_day.start_location.longitude, planned_day.start_location.latitude)
    end_gap = neighborhood_gap(candidate.longitude, candidate.latitude, planned_day.end_location.longitude, planned_day.end_location.latitude)
    gap = min(start_gap, end_gap)
    penalty = gap
    nearest_distance = min(
        distance_meters(planned_day.start_location.longitude, planned_day.start_location.latitude, candidate.longitude, candidate.latitude),
        distance_meters(planned_day.end_location.longitude, planned_day.end_location.latitude, candidate.longitude, candidate.latitude),
    )
    if nearest_distance > 12_000:
        penalty += 1
    if has_answer(request, "TRANSIT_SIMPLE"):
        penalty += gap
    return penalty


def content_type_priority(content_type_id: str) -> int:
    if content_type_id == "12":
        return 0
    if content_type_id in {"15", "28"}:
        return 1
    if content_type_id in {"14", "38"}:
        return 2
    if content_type_id == "39":
        return 3
    return 4


def normalize_candidate_text(candidate: CandidatePlace) -> str:
    return f"{candidate.name} {candidate.category_label} {candidate.address or ''}".lower()


def neighborhood_gap(
    longitude_a: Decimal,
    latitude_a: Decimal,
    longitude_b: Decimal,
    latitude_b: Decimal,
) -> int:
    return int(distance_meters(longitude_a, latitude_a, longitude_b, latitude_b) // 3000)
