from __future__ import annotations

import datetime as dt
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ScheduleLocation(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    longitude: Decimal
    latitude: Decimal

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class SelectedAnswer(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    question_id: str = Field(alias="questionId")
    answer_id: str = Field(alias="answerId")

    @field_validator("question_id", "answer_id")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class DayCondition(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    day_no: int = Field(alias="dayNo")
    start_time: time = Field(alias="startTime")
    end_time: time = Field(alias="endTime")
    start_location: ScheduleLocation = Field(alias="startLocation")
    end_location: ScheduleLocation = Field(alias="endLocation")

    @model_validator(mode="after")
    def validate_day_condition(self) -> "DayCondition":
        if self.day_no <= 0:
            raise ValueError("dayNo must be positive")
        if self.end_time <= self.start_time:
            raise ValueError("endTime must be after startTime")
        return self


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    start_date: date = Field(alias="startDate")
    end_date: date = Field(alias="endDate")
    daily_start_time: time = Field(alias="dailyStartTime")
    daily_end_time: time = Field(alias="dailyEndTime")
    start_location: ScheduleLocation = Field(alias="startLocation")
    end_location: ScheduleLocation = Field(alias="endLocation")
    selected_answers: list[SelectedAnswer] = Field(alias="selectedAnswers")
    must_visit_place_ids: list[int] = Field(default_factory=list, alias="mustVisitPlaceIds")
    days: list[DayCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_schedule_request(self) -> "ScheduleCreateRequest":
        if self.end_date < self.start_date:
            raise ValueError("endDate must be on or after startDate")
        if self.daily_end_time <= self.daily_start_time:
            raise ValueError("dailyEndTime must be after dailyStartTime")
        if not self.selected_answers:
            raise ValueError("selectedAnswers must not be empty")
        if any(place_id <= 0 for place_id in self.must_visit_place_ids):
            raise ValueError("mustVisitPlaceIds must contain only positive integers")

        trip_days = (self.end_date - self.start_date).days + 1
        if len(self.days) > trip_days:
            raise ValueError("days cannot exceed the trip length")
        if self.days:
            expected = set(range(1, trip_days + 1))
            actual = {day.day_no for day in self.days}
            if len(actual) != len(self.days):
                raise ValueError("dayNo values must be unique")
            if not actual.issubset(expected):
                raise ValueError("dayNo must be within the trip range")
        return self


class ScheduleUpdateStop(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    stop_id: UUID | None = Field(default=None, alias="stopId")
    place_id: int | None = Field(default=None, alias="placeId")
    day_no: int = Field(alias="dayNo")
    order: int
    stay_minutes: int = Field(alias="stayMinutes")

    @model_validator(mode="after")
    def validate_stop(self) -> "ScheduleUpdateStop":
        if self.day_no <= 0 or self.order <= 0:
            raise ValueError("dayNo and order must be positive")
        if self.stay_minutes < 30:
            raise ValueError("stayMinutes must be at least 30")
        if (self.stop_id is None) == (self.place_id is None):
            raise ValueError("exactly one of stopId or placeId must be provided")
        if self.place_id is not None and self.place_id <= 0:
            raise ValueError("placeId must be positive")
        return self


class ScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    stops: list[ScheduleUpdateStop]

    @model_validator(mode="after")
    def validate_stops(self) -> "ScheduleUpdateRequest":
        if not self.stops:
            raise ValueError("stops must not be empty")
        return self


class OperatingInfo(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    opening_hours_text: str | None = Field(default=None, alias="openingHoursText")
    closed_days_text: str | None = Field(default=None, alias="closedDaysText")
    requires_manual_check: bool = Field(default=True, alias="requiresManualCheck")


class SchedulePlace(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: int | None = None
    name: str
    category: str | None = None
    category_label: str = Field(default="미확인", alias="categoryLabel")
    address: str | None = None
    longitude: Decimal | None = None
    latitude: Decimal | None = None
    primary_image_url: str | None = Field(default=None, alias="primaryImageUrl")
    operating_info: OperatingInfo | None = Field(default=None, alias="operatingInfo")


class ScheduleSegment(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    order: int
    mode: str
    line_name: str | None = Field(default=None, alias="lineName")
    start_station_id: str | None = Field(default=None, alias="startStationId")
    start_station_name: str | None = Field(default=None, alias="startStationName")
    end_station_id: str | None = Field(default=None, alias="endStationId")
    end_station_name: str | None = Field(default=None, alias="endStationName")
    instruction: str | None = None
    duration_minutes: int = Field(default=0, alias="durationMinutes")
    distance_meters: int | None = Field(default=None, alias="distanceMeters")
    station_count: int | None = Field(default=None, alias="stationCount")
    wait_minutes: int = Field(default=0, alias="waitMinutes")
    realtime_status: str = Field(default="UNAVAILABLE", alias="realtimeStatus")


class ScheduleTransit(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    route_type: str | None = Field(default=None, alias="routeType")
    route_order: int = Field(default=0, alias="routeOrder")
    origin_name: str | None = Field(default=None, alias="originName")
    destination_name: str | None = Field(default=None, alias="destinationName")
    summary: str | None = None
    depart_at: time | None = Field(default=None, alias="departAt")
    arrive_at: time | None = Field(default=None, alias="arriveAt")
    total_minutes: int = Field(default=0, alias="totalMinutes")
    walk_minutes: int = Field(default=0, alias="walkMinutes")
    wait_minutes: int = Field(default=0, alias="waitMinutes")
    transfer_count: int = Field(default=0, alias="transferCount")
    fare_amount: int | None = Field(default=None, alias="fareAmount")
    provider: str = "FASTAPI_MIGRATION"
    realtime_status: str = Field(default="UNAVAILABLE", alias="realtimeStatus")
    fallback_used: bool = Field(default=True, alias="fallbackUsed")
    segments: list[ScheduleSegment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ScheduleStop(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: UUID
    order: int
    arrive_at: time | None = Field(default=None, alias="arriveAt")
    depart_at: time | None = Field(default=None, alias="departAt")
    stay_minutes: int = Field(alias="stayMinutes")
    place: SchedulePlace
    inbound_transit: ScheduleTransit | None = Field(default=None, alias="inboundTransit")
    meal_time_slot: str | None = Field(default=None, alias="mealTimeSlot")
    waiting_minutes_before: int = Field(default=0, alias="waitingMinutesBefore")
    selection_reasons: list[str] = Field(default_factory=list, alias="selectionReasons")
    warnings: list[str] = Field(default_factory=list)
    fixed_starts_at: datetime | None = Field(default=None, alias="fixedStartsAt")
    fixed_ends_at: datetime | None = Field(default=None, alias="fixedEndsAt")


class DayLocation(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    longitude: Decimal | None = None
    latitude: Decimal | None = None


class ScheduleDay(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    day_no: int = Field(alias="dayNo")
    date: date
    start_time: time | None = Field(default=None, alias="startTime")
    end_time: time | None = Field(default=None, alias="endTime")
    start_location: DayLocation | None = Field(default=None, alias="startLocation")
    end_location: DayLocation | None = Field(default=None, alias="endLocation")
    start_location_source: str | None = Field(default=None, alias="startLocationSource")
    end_location_source: str | None = Field(default=None, alias="endLocationSource")
    summary: str | None = None
    stops: list[ScheduleStop] = Field(default_factory=list)
    final_transit: ScheduleTransit | None = Field(default=None, alias="finalTransit")


class PlanningAssumptions(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    time_zone: str | None = Field(default="Asia/Seoul", alias="timeZone")
    lodging_mode: str | None = Field(default="UNSPECIFIED", alias="lodgingMode")
    route_coverage: str | None = Field(default="SKELETON_ONLY", alias="routeCoverage")
    warnings: list[str] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: UUID
    status: str
    start_date: date = Field(alias="startDate")
    end_date: date = Field(alias="endDate")
    daily_start_time: time | None = Field(default=None, alias="dailyStartTime")
    daily_end_time: time | None = Field(default=None, alias="dailyEndTime")
    style_summary: str = Field(alias="styleSummary")
    days: list[ScheduleDay]
    evaluation: dict[str, Any] | None = None
    preview_id: UUID | None = Field(default=None, alias="previewId")
    planning_assumptions: PlanningAssumptions | None = Field(
        default=None, alias="planningAssumptions"
    )


class ScheduleListResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    items: list[ScheduleResponse]


class MapMarker(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    longitude: Decimal | None = None
    latitude: Decimal | None = None


class StopMarker(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    day_no: int = Field(alias="dayNo")
    order: int
    place_id: int | None = Field(default=None, alias="placeId")
    name: str
    arrive_at: time | None = Field(default=None, alias="arriveAt")
    depart_at: time | None = Field(default=None, alias="departAt")
    subtitle: str | None = None
    risk_level: str = Field(default="NORMAL", alias="riskLevel")
    longitude: Decimal | None = None
    latitude: Decimal | None = None


class RouteLine(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    day_no: int = Field(alias="dayNo")
    route_order: int = Field(alias="routeOrder")
    line_order: int = Field(alias="lineOrder")
    mode: str
    line_name: str | None = Field(default=None, alias="lineName")
    start_name: str | None = Field(default=None, alias="startName")
    end_name: str | None = Field(default=None, alias="endName")
    duration_minutes: int | None = Field(default=None, alias="durationMinutes")
    distance_meters: int | None = Field(default=None, alias="distanceMeters")
    instruction: str | None = None
    fallback_used: bool = Field(default=True, alias="fallbackUsed")
    coordinates: list[list[Decimal]] = Field(default_factory=list)


class ScheduleMapResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    start_marker: MapMarker | None = Field(default=None, alias="startMarker")
    end_marker: MapMarker | None = Field(default=None, alias="endMarker")
    markers: list[StopMarker] = Field(default_factory=list)
    route_lines: list[RouteLine] = Field(default_factory=list, alias="routeLines")


class PreviewLocation(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    address: str | None = None
    longitude: Decimal
    latitude: Decimal

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class PreviewLodgingNightStay(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    date: date
    location: PreviewLocation


class PreviewLodgingPlan(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    mode: str
    base_location: PreviewLocation | None = Field(default=None, alias="baseLocation")
    night_stays: list[PreviewLodgingNightStay] = Field(default_factory=list, alias="nightStays")


class PreviewEndConstraint(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    type: str
    location: PreviewLocation
    target_at: str = Field(alias="targetAt")
    buffer_minutes: int | None = Field(default=None, alias="bufferMinutes")


class PreviewSelectedAnswer(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    question_id: str = Field(alias="questionId")
    answer_ids: list[str] = Field(alias="answerIds")

    @model_validator(mode="after")
    def validate_answer_ids(self) -> "PreviewSelectedAnswer":
        if not self.answer_ids:
            raise ValueError("answerIds must not be empty")
        self.answer_ids = [answer_id.strip() for answer_id in self.answer_ids if answer_id.strip()]
        if not self.answer_ids:
            raise ValueError("answerIds must not be empty")
        return self


class PreviewFixedEvent(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    client_event_id: str = Field(alias="clientEventId")
    name: str
    place_id: int = Field(alias="placeId")
    starts_at: str = Field(alias="startsAt")
    ends_at: str = Field(alias="endsAt")


class PreviewDayOverride(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    date: date
    available_from: time | None = Field(default=None, alias="availableFrom")
    available_until: time | None = Field(default=None, alias="availableUntil")
    start_location: PreviewLocation | None = Field(default=None, alias="startLocation")
    end_location: PreviewLocation | None = Field(default=None, alias="endLocation")


class SchedulePreviewCreateRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    start_date: date = Field(alias="startDate")
    end_date: date = Field(alias="endDate")
    start_location: PreviewLocation = Field(alias="startLocation")
    start_time: time | None = Field(default=None, alias="startTime")
    lodging_plan: PreviewLodgingPlan = Field(alias="lodgingPlan")
    end_constraint: PreviewEndConstraint | None = Field(default=None, alias="endConstraint")
    selected_answers: list[PreviewSelectedAnswer] = Field(alias="selectedAnswers")
    must_visit_place_ids: list[int] = Field(default_factory=list, alias="mustVisitPlaceIds")
    fixed_events: list[PreviewFixedEvent] = Field(default_factory=list, alias="fixedEvents")
    day_overrides: list[PreviewDayOverride] = Field(default_factory=list, alias="dayOverrides")
    custom_prompt: str | None = Field(default=None, alias="customPrompt")
    time_zone: str | None = Field(default=None, alias="timeZone")

    @model_validator(mode="after")
    def validate_preview_request(self) -> "SchedulePreviewCreateRequest":
        if self.end_date < self.start_date:
            raise ValueError("endDate must be on or after startDate")
        if not self.selected_answers:
            raise ValueError("selectedAnswers must not be empty")
        if any(place_id <= 0 for place_id in self.must_visit_place_ids):
            raise ValueError("mustVisitPlaceIds must contain only positive integers")
        return self


class SchedulePreviewScheduleRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    preview_id: UUID = Field(alias="previewId")


class SchedulePreviewLocationResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    address: str | None = None
    longitude: Decimal
    latitude: Decimal


class ResolvedDay(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    date: date
    available_from: time = Field(alias="availableFrom")
    available_until: time = Field(alias="availableUntil")
    start_location: SchedulePreviewLocationResponse = Field(alias="startLocation")
    end_location: SchedulePreviewLocationResponse = Field(alias="endLocation")
    start_location_source: str = Field(alias="startLocationSource")
    end_location_source: str = Field(alias="endLocationSource")


class ResolvedEndConstraint(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    type: str
    target_at: str = Field(alias="targetAt")
    applied_buffer_minutes: int = Field(alias="appliedBufferMinutes")
    available_until: time = Field(alias="availableUntil")


class AppliedDefault(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    field_path: str = Field(alias="fieldPath")
    resolved_value: Any = Field(alias="resolvedValue")
    reason_code: str = Field(alias="reasonCode")


class InterpretedPrompt(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    preferences: list[str] = Field(default_factory=list)
    unrecognized_texts: list[str] = Field(default_factory=list, alias="unrecognizedTexts")
    source: str = "RULE_BASED"
    confidence: int = 100


class PreviewWarning(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    code: str
    date: dt.date | None = None
    message: str


class PreviewConflict(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    code: str
    message: str
    field_path: str | None = Field(default=None, alias="fieldPath")
    conflict_date: dt.date | None = Field(default=None, alias="conflictDate")
    required_minutes: int | None = Field(default=None, alias="requiredMinutes")
    available_minutes: int | None = Field(default=None, alias="availableMinutes")
    adjustable_fields: list[str] = Field(default_factory=list, alias="adjustableFields")


class SchedulePreviewResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    preview_id: UUID = Field(alias="previewId")
    status: str
    can_generate: bool = Field(alias="canGenerate")
    expires_at: datetime = Field(alias="expiresAt")
    time_zone: str = Field(alias="timeZone")
    lodging_mode: str = Field(alias="lodgingMode")
    route_coverage: str = Field(alias="routeCoverage")
    resolved_days: list[ResolvedDay] = Field(default_factory=list, alias="resolvedDays")
    resolved_end_constraint: ResolvedEndConstraint | None = Field(default=None, alias="resolvedEndConstraint")
    applied_defaults: list[AppliedDefault] = Field(default_factory=list, alias="appliedDefaults")
    interpreted_prompt: InterpretedPrompt = Field(alias="interpretedPrompt")
    warnings: list[PreviewWarning] = Field(default_factory=list)
    conflicts: list[PreviewConflict] = Field(default_factory=list)
    schedule_id: UUID | None = Field(default=None, alias="scheduleId")
