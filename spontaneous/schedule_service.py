from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from schedule.models import (
    DayLocation,
    PlanningAssumptions,
    ScheduleDay,
    SchedulePlace,
    ScheduleResponse,
    ScheduleStop,
    ScheduleTransit,
)
from schedule.persistence import (
    db_enabled,
    load_schedule,
    save_spontaneous_schedule as persist_spontaneous_schedule,
)
from spontaneous.preview import preview_request_hash, verify_preview_token


def save_spontaneous_preview(
    preview_id: UUID,
    preview_token: str,
    owner_id: int | None,
    idempotency_key: str | None,
) -> ScheduleResponse:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    if idempotency_key is None or not idempotency_key.strip() or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="IDEMPOTENCY_KEY_REQUIRED")
    if not db_enabled():
        raise HTTPException(status_code=503, detail="SCHEDULE_DATABASE_REQUIRED")

    snapshot = verify_preview_token(preview_token, preview_id, owner_id)
    schedule, place_snapshots = schedule_from_snapshot(snapshot, preview_id)
    schedule_id = persist_spontaneous_schedule(
        schedule,
        place_snapshots,
        owner_id,
        idempotency_key.strip(),
        preview_request_hash(preview_token),
        preview_id,
    )
    return load_schedule(schedule_id)


def schedule_from_snapshot(
    snapshot: dict[str, Any],
    preview_id: UUID,
) -> tuple[ScheduleResponse, list[dict[str, Any]]]:
    request = _required_dict(snapshot, "request")
    destination = _required_dict(snapshot, "destination")
    course = snapshot.get("course")
    if not isinstance(course, list) or not course:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")

    start_at = _offset_datetime(request.get("startAt"))
    return_by = _offset_datetime(request.get("returnBy"))
    estimated_return_at = _offset_datetime(snapshot.get("estimatedReturnAt"))
    if estimated_return_at > return_by:
        raise HTTPException(status_code=422, detail="SPONTANEOUS_RETURN_TIME_EXCEEDED")
    start_location_raw = _required_dict(request, "startLocation")
    start_location = DayLocation(
        name=str(start_location_raw.get("name") or "출발지"),
        longitude=Decimal(str(start_location_raw["longitude"])),
        latitude=Decimal(str(start_location_raw["latitude"])),
    )

    stops: list[ScheduleStop] = []
    place_snapshots: list[dict[str, Any]] = []
    for item in course:
        place_snapshot = _required_dict(item, "placeSnapshot")
        place_snapshots.append(place_snapshot)
        arrival_at = _offset_datetime(item.get("arrivalAt"))
        departure_at = _offset_datetime(item.get("departureAt"))
        inbound = ScheduleTransit.model_validate(item.get("inboundTransit"))
        stops.append(
            ScheduleStop(
                id=uuid4(),
                order=int(item["order"]),
                arriveAt=arrival_at.timetz().replace(tzinfo=None),
                departAt=departure_at.timetz().replace(tzinfo=None),
                arriveAtDateTime=arrival_at,
                departAtDateTime=departure_at,
                stayMinutes=int(item["stayMinutes"]),
                role=item.get("role"),
                themes=list(item.get("themes") or []),
                place=SchedulePlace(
                    id=None,
                    name=str(place_snapshot["name"]),
                    category=place_snapshot.get("contentTypeId"),
                    categoryLabel=_category_label(place_snapshot.get("contentTypeId")),
                    address=place_snapshot.get("address"),
                    longitude=Decimal(str(place_snapshot["longitude"])),
                    latitude=Decimal(str(place_snapshot["latitude"])),
                    primaryImageUrl=place_snapshot.get("primaryImageUrl"),
                ),
                inboundTransit=inbound,
                selectionReasons=["즉흥여행 미리보기에서 사용자가 저장한 방문지입니다."],
                warnings=[],
            )
        )

    final_transit = ScheduleTransit.model_validate(snapshot.get("finalTransit"))
    transport_mode = str(request["transportMode"])
    desired_themes = [str(value) for value in request.get("desiredThemes") or []]
    destination_name = str(destination["name"])
    theme_text = ", ".join(desired_themes)
    warnings = _dedupe(
        warning
        for transit in [*(stop.inbound_transit for stop in stops), final_transit]
        if transit is not None
        for warning in transit.warnings
    )
    schedule = ScheduleResponse(
        id=uuid4(),
        status="CONFIRMED",
        scheduleType="SPONTANEOUS",
        startDate=start_at.date(),
        endDate=estimated_return_at.date(),
        dailyStartTime=start_at.timetz().replace(tzinfo=None),
        dailyEndTime=estimated_return_at.timetz().replace(tzinfo=None),
        styleSummary=f"즉흥여행 · {destination_name}" + (f" · {theme_text}" if theme_text else ""),
        transportMode=transport_mode,
        startAt=start_at,
        returnBy=return_by,
        estimatedReturnAt=estimated_return_at,
        spontaneousMetadata={
            "schemaVersion": 1,
            "previewId": str(preview_id),
            "destinationId": destination["destinationId"],
            "destinationName": destination_name,
            "desiredThemes": desired_themes,
            "startLocation": start_location_raw,
            "returnLocation": start_location_raw,
        },
        days=[
            ScheduleDay(
                dayNo=1,
                date=start_at.date(),
                startTime=start_at.timetz().replace(tzinfo=None),
                endTime=estimated_return_at.timetz().replace(tzinfo=None),
                startLocation=start_location,
                endLocation=start_location,
                startLocationSource="SPONTANEOUS_REQUEST",
                endLocationSource="SPONTANEOUS_RETURN",
                summary=f"{destination_name} 즉흥여행 {len(stops)}개 방문지",
                stops=stops,
                finalTransit=final_transit,
            )
        ],
        evaluation=None,
        previewId=None,
        planningAssumptions=PlanningAssumptions(
            timeZone="Asia/Seoul",
            lodgingMode="NOT_APPLICABLE",
            routeCoverage="SPONTANEOUS_PROVIDER_ROUTE",
            warnings=warnings,
        ),
    )
    return schedule, place_snapshots


def _required_dict(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")
    return value


def _offset_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID") from exc
    if parsed.utcoffset() is None:
        raise HTTPException(status_code=400, detail="SPONTANEOUS_PREVIEW_INVALID")
    return parsed


def _category_label(content_type_id: Any) -> str:
    return {
        "12": "관광지",
        "14": "문화시설",
        "15": "축제·공연",
        "28": "레포츠",
        "32": "숙박",
        "38": "쇼핑",
        "39": "음식점",
    }.get(str(content_type_id), "관광지")


def _dedupe(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
