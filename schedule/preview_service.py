from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Lock
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4

from fastapi import HTTPException

from schedule.models import (
    AppliedDefault,
    DayCondition,
    InterpretedPrompt,
    PlanningAssumptions,
    PreviewConflict,
    PreviewLocation,
    PreviewWarning,
    ResolvedDay,
    ResolvedEndConstraint,
    ScheduleCreateRequest,
    ScheduleLocation,
    SchedulePreviewCreateRequest,
    SchedulePreviewLocationResponse,
    SchedulePreviewResponse,
    SchedulePreviewScheduleRequest,
    SelectedAnswer,
)
from schedule.persistence import db_enabled, load_preview, mark_preview_consumed, save_preview
from schedule.service import FixedEventSpec

DEFAULT_TIME_ZONE = "Asia/Seoul"
DEFAULT_DAY_START = time(10, 0)
DEFAULT_DAY_END = time(20, 0)
MIN_AVAILABLE_MINUTES = 180
PREVIEW_EXPIRATION_MINUTES = 30
MAX_STOPS_PER_DAY = 5
SERVICE_ZONE = ZoneInfo(DEFAULT_TIME_ZONE)


@dataclass
class PreviewRecord:
    response: SchedulePreviewResponse
    create_request: ScheduleCreateRequest
    fixed_events_by_day: dict[int, list[FixedEventSpec]]


class PreviewStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, PreviewRecord] = {}

    def save(self, record: PreviewRecord) -> SchedulePreviewResponse:
        with self._lock:
            self._items[record.response.preview_id] = record
        return record.response

    def get_record(self, preview_id: UUID) -> PreviewRecord:
        with self._lock:
            record = self._items.get(preview_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Schedule preview not found")
        if datetime.now(SERVICE_ZONE) > record.response.expires_at and record.response.status != "CONSUMED":
            expired = record.response.model_copy(deep=True)
            expired.status = "EXPIRED"
            expired.can_generate = False
            with self._lock:
                self._items[preview_id] = PreviewRecord(expired, record.create_request, record.fixed_events_by_day)
            return PreviewRecord(expired, record.create_request, record.fixed_events_by_day)
        return record

    def mark_consumed(self, preview_id: UUID, schedule_id: UUID) -> SchedulePreviewResponse:
        record = self.get_record(preview_id)
        updated = record.response.model_copy(deep=True)
        updated.status = "CONSUMED"
        updated.can_generate = False
        updated.schedule_id = schedule_id
        with self._lock:
            self._items[preview_id] = PreviewRecord(updated, record.create_request, record.fixed_events_by_day)
        return updated


PREVIEW_STORE = PreviewStore()


def create_preview(request: SchedulePreviewCreateRequest) -> SchedulePreviewResponse:
    validate_request(request)
    resolved_days = resolve_days(request)
    applied_defaults = collect_defaults(request, resolved_days)
    warnings = collect_warnings(request)
    conflicts = collect_conflicts(resolved_days)
    conflicts.extend(collect_fixed_event_conflicts(request, resolved_days))
    status = "READY" if not conflicts else "REQUIRES_ACTION"
    interpreted_prompt = interpret_prompt(request.custom_prompt)
    preview_id = uuid4()
    response = SchedulePreviewResponse(
        previewId=preview_id,
        status=status,
        canGenerate=status == "READY",
        expiresAt=datetime.now(SERVICE_ZONE) + timedelta(minutes=PREVIEW_EXPIRATION_MINUTES),
        timeZone=request.time_zone or DEFAULT_TIME_ZONE,
        lodgingMode=request.lodging_plan.mode,
        routeCoverage=resolve_route_coverage(request),
        resolvedDays=resolved_days,
        resolvedEndConstraint=resolve_end_constraint(request, resolved_days),
        appliedDefaults=applied_defaults,
        interpretedPrompt=interpreted_prompt,
        warnings=warnings,
        conflicts=conflicts,
        scheduleId=None,
    )
    create_request = build_create_request(request, resolved_days)
    PREVIEW_STORE.save(
        PreviewRecord(
            response=response,
            create_request=create_request,
            fixed_events_by_day=fixed_events_by_day(request, resolved_days),
        )
    )
    if db_enabled():
        try:
            save_preview(response, request)
        except Exception:
            pass
    return response


def get_preview(preview_id: UUID) -> SchedulePreviewResponse:
    try:
        return PREVIEW_STORE.get_record(preview_id).response
    except HTTPException:
        if not db_enabled():
            raise
        preview, request = load_preview(preview_id)
        resolved_days = preview.resolved_days
        record = PreviewRecord(
            response=preview,
            create_request=build_create_request(request, resolved_days),
            fixed_events_by_day=fixed_events_by_day(request, resolved_days),
        )
        PREVIEW_STORE.save(record)
        return record.response


def consume_preview(
    request: SchedulePreviewScheduleRequest,
) -> tuple[SchedulePreviewResponse, ScheduleCreateRequest, dict[int, list[FixedEventSpec]]]:
    record = PREVIEW_STORE.get_record(request.preview_id)
    if record.response.status == "CONSUMED":
        raise HTTPException(status_code=409, detail="Preview already consumed")
    if record.response.status != "READY":
        raise HTTPException(status_code=400, detail="Preview is not ready for schedule generation")
    return record.response, record.create_request, record.fixed_events_by_day


def attach_schedule_to_preview(preview_id: UUID, schedule_id: UUID) -> None:
    PREVIEW_STORE.mark_consumed(preview_id, schedule_id)
    if db_enabled():
        try:
            mark_preview_consumed(preview_id)
        except Exception:
            pass


def validate_request(request: SchedulePreviewCreateRequest) -> None:
    trip_days = (request.end_date - request.start_date).days + 1
    if trip_days < 1 or trip_days > 4:
        raise HTTPException(status_code=400, detail="Trip length must be between 1 and 4 days")
    validate_lodging(request, trip_days)
    validate_end_constraint(request)
    validate_overrides(request)
    validate_fixed_events(request, trip_days)
    validate_place_limits(request, trip_days)


def resolve_days(request: SchedulePreviewCreateRequest) -> list[ResolvedDay]:
    trip_days = (request.end_date - request.start_date).days + 1
    overrides = {override.date: override for override in request.day_overrides}
    night_stays = {stay.date: stay.location for stay in request.lodging_plan.night_stays}
    resolved: list[ResolvedDay] = []
    for offset in range(trip_days):
        current_date = request.start_date + timedelta(days=offset)
        override = overrides.get(current_date)
        available_from = override.available_from if override and override.available_from else request.start_time or DEFAULT_DAY_START
        available_until = override.available_until if override and override.available_until else DEFAULT_DAY_END
        if offset == trip_days - 1 and request.end_constraint is not None:
            available_until = min(available_until, apply_end_constraint_cutoff(request.end_constraint).time())
        start_location, start_source = resolve_day_start_location(request, current_date, offset, override, night_stays)
        end_location, end_source = resolve_day_end_location(request, current_date, offset, trip_days, override, night_stays)
        resolved.append(
            ResolvedDay(
                date=current_date,
                availableFrom=available_from,
                availableUntil=available_until,
                startLocation=to_response_location(start_location),
                endLocation=to_response_location(end_location),
                startLocationSource=start_source,
                endLocationSource=end_source,
            )
        )
    return resolved


def collect_defaults(
    request: SchedulePreviewCreateRequest,
    resolved_days: list[ResolvedDay],
) -> list[AppliedDefault]:
    defaults: list[AppliedDefault] = []
    if not request.time_zone:
        defaults.append(
            AppliedDefault(
                fieldPath="timeZone",
                resolvedValue=DEFAULT_TIME_ZONE,
                reasonCode="DEFAULT_TIME_ZONE",
            )
        )
    for day in resolved_days:
        if request.start_time is None and day.available_from == DEFAULT_DAY_START:
            defaults.append(
                AppliedDefault(
                    fieldPath=f"resolvedDays[{day.date.isoformat()}].availableFrom",
                    resolvedValue=DEFAULT_DAY_START.isoformat(),
                    reasonCode="DEFAULT_DAY_START",
                )
            )
            break
    return defaults


def collect_warnings(request: SchedulePreviewCreateRequest) -> list[PreviewWarning]:
    warnings: list[PreviewWarning] = [
        PreviewWarning(
            code="FASTAPI_PREVIEW_MIGRATION",
            date=None,
            message="입력한 여행 조건을 바탕으로 미리보기 일정을 구성했습니다.",
        )
    ]
    if request.fixed_events:
        warnings.append(
            PreviewWarning(
                code="FIXED_EVENT_NOT_APPLIED",
                date=None,
                message="고정 일정 시간은 반영되었으며, 세부 이동 순서는 일정 확정 과정에서 다시 조정될 수 있습니다.",
            )
        )
    if request.custom_prompt:
        warnings.append(
            PreviewWarning(
                code="CUSTOM_PROMPT_RULE_BASED_ONLY",
                date=None,
                message="Custom prompt is interpreted with rule-based heuristics only.",
            )
        )
    return warnings


def collect_conflicts(resolved_days: list[ResolvedDay]) -> list[PreviewConflict]:
    conflicts: list[PreviewConflict] = []
    for day in resolved_days:
        available_minutes = int(
            (
                datetime.combine(day.date, day.available_until)
                - datetime.combine(day.date, day.available_from)
            ).total_seconds()
            // 60
        )
        if available_minutes < MIN_AVAILABLE_MINUTES:
            conflicts.append(
                PreviewConflict(
                    code="INSUFFICIENT_AVAILABLE_MINUTES",
                    message="Available time is too short for reliable day planning.",
                    fieldPath="dayOverrides",
                    conflictDate=day.date,
                    requiredMinutes=MIN_AVAILABLE_MINUTES,
                    availableMinutes=available_minutes,
                    adjustableFields=["dayOverrides.availableFrom", "dayOverrides.availableUntil"],
                )
            )
    return conflicts


def collect_fixed_event_conflicts(
    request: SchedulePreviewCreateRequest,
    resolved_days: list[ResolvedDay],
) -> list[PreviewConflict]:
    day_window = {day.date: (day.available_from, day.available_until) for day in resolved_days}
    grouped: dict[date, list[tuple[datetime, datetime, str]]] = {}
    conflicts: list[PreviewConflict] = []
    for event in request.fixed_events:
        start_local = parse_offset_datetime(event.starts_at).astimezone(SERVICE_ZONE)
        end_local = parse_offset_datetime(event.ends_at).astimezone(SERVICE_ZONE)
        window = day_window.get(start_local.date())
        if window is None:
            continue
        available_from, available_until = window
        if start_local.time() < available_from or end_local.time() > available_until:
            conflicts.append(
                PreviewConflict(
                    code="FIXED_EVENT_OUTSIDE_AVAILABLE_WINDOW",
                    message=f"{event.name} fixed event does not fit within the available time window.",
                    fieldPath="fixedEvents",
                    conflictDate=start_local.date(),
                    requiredMinutes=int((end_local - start_local).total_seconds() // 60),
                    availableMinutes=int(
                        (
                            datetime.combine(start_local.date(), available_until)
                            - datetime.combine(start_local.date(), available_from)
                        ).total_seconds() // 60
                    ),
                    adjustableFields=["fixedEvents", "dayOverrides.availableFrom", "dayOverrides.availableUntil"],
                )
            )
        grouped.setdefault(start_local.date(), []).append((start_local.replace(tzinfo=None), end_local.replace(tzinfo=None), event.name))
    for conflict_date, events in grouped.items():
        events.sort(key=lambda item: item[0])
        for index in range(1, len(events)):
            previous_end = events[index - 1][1]
            current_start = events[index][0]
            if current_start < previous_end:
                conflicts.append(
                    PreviewConflict(
                        code="FIXED_EVENT_OVERLAP",
                        message=f"{events[index - 1][2]} and {events[index][2]} overlap.",
                        fieldPath="fixedEvents",
                        conflictDate=conflict_date,
                        requiredMinutes=None,
                        availableMinutes=None,
                        adjustableFields=["fixedEvents"],
                    )
                )
    return conflicts


def validate_lodging(request: SchedulePreviewCreateRequest, trip_days: int) -> None:
    mode = request.lodging_plan.mode
    if mode == "UNDECIDED":
        if request.lodging_plan.base_location is not None or request.lodging_plan.night_stays:
            raise HTTPException(status_code=400, detail="UNDECIDED lodging cannot include baseLocation or nightStays")
        return
    if mode == "FIXED_BASE":
        if request.lodging_plan.base_location is None:
            raise HTTPException(status_code=400, detail="FIXED_BASE lodging requires baseLocation")
        return
    if mode == "PER_NIGHT":
        expected_dates = {request.start_date + timedelta(days=index) for index in range(max(0, trip_days - 1))}
        actual_dates = {stay.date for stay in request.lodging_plan.night_stays}
        if actual_dates != expected_dates:
            raise HTTPException(status_code=400, detail="PER_NIGHT lodging requires every night stay location")
        return
    raise HTTPException(status_code=400, detail="Unsupported lodging mode")


def validate_end_constraint(request: SchedulePreviewCreateRequest) -> None:
    if request.end_constraint is None:
        return
    if request.end_constraint.type not in {"ARRIVE_BY", "TRAIN_DEPARTURE", "FLIGHT_DEPARTURE"}:
        raise HTTPException(status_code=400, detail="Unsupported endConstraint type")
    target_at = parse_offset_datetime(request.end_constraint.target_at)
    if target_at.astimezone(SERVICE_ZONE).date() != request.end_date:
        raise HTTPException(status_code=400, detail="endConstraint targetAt must be on endDate")


def validate_overrides(request: SchedulePreviewCreateRequest) -> None:
    seen_dates: set[date] = set()
    for override in request.day_overrides:
        if override.date in seen_dates:
            raise HTTPException(status_code=400, detail="dayOverrides dates must be unique")
        seen_dates.add(override.date)
        if override.date < request.start_date or override.date > request.end_date:
            raise HTTPException(status_code=400, detail="dayOverride date must be within trip range")
        if (
            override.available_from is not None
            and override.available_until is not None
            and override.available_from >= override.available_until
        ):
            raise HTTPException(status_code=400, detail="dayOverride availableFrom must be before availableUntil")
        if request.end_constraint is not None and override.date == request.end_date and override.end_location is not None:
            raise HTTPException(status_code=400, detail="Final day endLocation override conflicts with endConstraint")


def validate_fixed_events(request: SchedulePreviewCreateRequest, trip_days: int) -> None:
    client_ids: set[str] = set()
    place_ids: set[int] = set()
    count_by_date: dict[date, int] = {}
    for event in request.fixed_events:
        if event.client_event_id in client_ids:
            raise HTTPException(status_code=400, detail="fixedEvents clientEventId must be unique")
        client_ids.add(event.client_event_id)
        if event.place_id in place_ids:
            raise HTTPException(status_code=400, detail="fixedEvents placeId must be unique")
        place_ids.add(event.place_id)
        starts_at = parse_offset_datetime(event.starts_at)
        ends_at = parse_offset_datetime(event.ends_at)
        if starts_at >= ends_at:
            raise HTTPException(status_code=400, detail="fixedEvent startsAt must be before endsAt")
        start_local = starts_at.astimezone(SERVICE_ZONE)
        end_local = ends_at.astimezone(SERVICE_ZONE)
        if start_local.date() != end_local.date():
            raise HTTPException(status_code=400, detail="fixedEvent must start and end on the same local date")
        if start_local.date() < request.start_date or start_local.date() > request.end_date:
            raise HTTPException(status_code=400, detail="fixedEvent date must be within trip range")
        count_by_date[start_local.date()] = count_by_date.get(start_local.date(), 0) + 1
        if count_by_date[start_local.date()] > MAX_STOPS_PER_DAY:
            raise HTTPException(status_code=400, detail="Too many fixed events on a single day")


def validate_place_limits(request: SchedulePreviewCreateRequest, trip_days: int) -> None:
    unique_place_ids = set(request.must_visit_place_ids)
    unique_place_ids.update(event.place_id for event in request.fixed_events)
    if len(unique_place_ids) > trip_days * MAX_STOPS_PER_DAY:
        raise HTTPException(status_code=400, detail="Too many must-visit places for trip length")


def interpret_prompt(custom_prompt: str | None) -> InterpretedPrompt:
    if not custom_prompt:
        return InterpretedPrompt(preferences=[], unrecognizedTexts=[])
    preferences: list[str] = []
    lowered = custom_prompt.lower()
    if "맛집" in custom_prompt or "food" in lowered:
        preferences.append("THEME_FOOD")
    if "바다" in custom_prompt or "해변" in custom_prompt or "ocean" in lowered:
        preferences.append("THEME_NATURE")
    if "여유" in custom_prompt or "힐링" in custom_prompt or "relax" in lowered:
        preferences.append("PACE_RELAXED")
    return InterpretedPrompt(
        preferences=preferences,
        unrecognizedTexts=[] if preferences else [custom_prompt],
    )


def resolve_route_coverage(request: SchedulePreviewCreateRequest) -> str:
    return "ATTRACTION_ROUTES_ONLY" if request.lodging_plan.mode == "UNDECIDED" else "FULL"


def resolve_end_constraint(
    request: SchedulePreviewCreateRequest,
    resolved_days: list[ResolvedDay],
) -> ResolvedEndConstraint | None:
    if request.end_constraint is None:
        return None
    last_day = resolved_days[-1]
    return ResolvedEndConstraint(
        type=request.end_constraint.type,
        targetAt=request.end_constraint.target_at,
        appliedBufferMinutes=default_buffer_minutes(request.end_constraint),
        availableUntil=apply_end_constraint_cutoff(request.end_constraint).time(),
    )


def build_create_request(
    request: SchedulePreviewCreateRequest,
    resolved_days: list[ResolvedDay],
) -> ScheduleCreateRequest:
    selected_answers = [
        SelectedAnswer(questionId=answer.question_id, answerId=answer.answer_ids[0])
        for answer in request.selected_answers
    ]
    day_conditions = [
        DayCondition(
            dayNo=index + 1,
            startTime=day.available_from,
            endTime=day.available_until,
            startLocation=ScheduleLocation(
                name=day.start_location.name,
                longitude=day.start_location.longitude,
                latitude=day.start_location.latitude,
            ),
            endLocation=ScheduleLocation(
                name=day.end_location.name,
                longitude=day.end_location.longitude,
                latitude=day.end_location.latitude,
            ),
        )
        for index, day in enumerate(resolved_days)
    ]
    return ScheduleCreateRequest(
        startDate=request.start_date,
        endDate=request.end_date,
        dailyStartTime=resolved_days[0].available_from,
        dailyEndTime=resolved_days[0].available_until,
        startLocation=ScheduleLocation(
            name=resolved_days[0].start_location.name,
            longitude=resolved_days[0].start_location.longitude,
            latitude=resolved_days[0].start_location.latitude,
        ),
        endLocation=ScheduleLocation(
            name=resolved_days[-1].end_location.name,
            longitude=resolved_days[-1].end_location.longitude,
            latitude=resolved_days[-1].end_location.latitude,
        ),
        selectedAnswers=selected_answers,
        mustVisitPlaceIds=request.must_visit_place_ids,
        days=day_conditions,
    )


def resolve_day_start_location(
    request: SchedulePreviewCreateRequest,
    current_date,
    offset: int,
    override,
    night_stays: dict,
) -> tuple[PreviewLocation, str]:
    if override and override.start_location is not None:
        return override.start_location, "DAY_OVERRIDE"
    if offset == 0:
        return request.start_location, "REQUEST"
    previous_night = night_stays.get(current_date - timedelta(days=1))
    if previous_night is not None:
        return previous_night, "LODGING_NIGHT_STAY"
    if request.lodging_plan.mode == "FIXED_BASE" and request.lodging_plan.base_location is not None:
        return request.lodging_plan.base_location, "LODGING_BASE"
    return request.start_location, "REQUEST"


def resolve_day_end_location(
    request: SchedulePreviewCreateRequest,
    current_date,
    offset: int,
    trip_days: int,
    override,
    night_stays: dict,
) -> tuple[PreviewLocation, str]:
    if override and override.end_location is not None:
        return override.end_location, "DAY_OVERRIDE"
    same_day_night = night_stays.get(current_date)
    if same_day_night is not None:
        return same_day_night, "LODGING_NIGHT_STAY"
    if request.lodging_plan.mode == "FIXED_BASE" and request.lodging_plan.base_location is not None:
        return request.lodging_plan.base_location, "LODGING_BASE"
    if offset == trip_days - 1 and request.end_constraint is not None:
        return request.end_constraint.location, "END_CONSTRAINT"
    return request.start_location, "REQUEST"


def to_response_location(location: PreviewLocation) -> SchedulePreviewLocationResponse:
    return SchedulePreviewLocationResponse(
        name=location.name,
        address=location.address,
        longitude=location.longitude,
        latitude=location.latitude,
    )


def fixed_events_by_day(
    request: SchedulePreviewCreateRequest,
    resolved_days: list[ResolvedDay],
) -> dict[int, list[FixedEventSpec]]:
    day_no_by_date = {day.date: index + 1 for index, day in enumerate(resolved_days)}
    grouped: dict[int, list[FixedEventSpec]] = {}
    for event in request.fixed_events:
        start_dt = parse_offset_datetime(event.starts_at).astimezone(SERVICE_ZONE).replace(tzinfo=None)
        end_dt = parse_offset_datetime(event.ends_at).astimezone(SERVICE_ZONE).replace(tzinfo=None)
        day_no = day_no_by_date[start_dt.date()]
        grouped.setdefault(day_no, []).append(
            FixedEventSpec(
                place_id=event.place_id,
                name=event.name,
                starts_at=start_dt,
                ends_at=end_dt,
            )
        )
    for day_no, events in grouped.items():
        grouped[day_no] = sorted(events, key=lambda event: event.starts_at)
    return grouped


def parse_offset_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_buffer_minutes(end_constraint) -> int:
    if end_constraint.buffer_minutes is not None:
        return end_constraint.buffer_minutes
    if end_constraint.type == "TRAIN_DEPARTURE":
        return 30
    if end_constraint.type == "FLIGHT_DEPARTURE":
        return 90
    return 0


def apply_end_constraint_cutoff(end_constraint) -> datetime:
    target = parse_offset_datetime(end_constraint.target_at).astimezone(SERVICE_ZONE)
    return target - timedelta(minutes=default_buffer_minutes(end_constraint))
