from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException

try:
    import psycopg
    from psycopg.rows import dict_row

    from schedule_models import (
        AppliedDefault,
        DayLocation,
        InterpretedPrompt,
        OperatingInfo,
        PlanningAssumptions,
        PreviewConflict,
        PreviewWarning,
        ResolvedDay,
        ResolvedEndConstraint,
        RouteLine,
        ScheduleCreateRequest,
        ScheduleDay,
        ScheduleListResponse,
        ScheduleLocation,
        ScheduleMapResponse,
        SchedulePlace,
        SchedulePreviewCreateRequest,
        SchedulePreviewLocationResponse,
        SchedulePreviewResponse,
        ScheduleResponse,
        ScheduleSegment,
        ScheduleStop,
        ScheduleTransit,
        StopMarker,
        MapMarker,
    )
except ModuleNotFoundError:  # pragma: no cover
    import psycopg
    from psycopg.rows import dict_row

    from data.schedule_models import (
        AppliedDefault,
        DayLocation,
        InterpretedPrompt,
        OperatingInfo,
        PlanningAssumptions,
        PreviewConflict,
        PreviewWarning,
        ResolvedDay,
        ResolvedEndConstraint,
        RouteLine,
        ScheduleCreateRequest,
        ScheduleDay,
        ScheduleListResponse,
        ScheduleLocation,
        ScheduleMapResponse,
        SchedulePlace,
        SchedulePreviewCreateRequest,
        SchedulePreviewLocationResponse,
        SchedulePreviewResponse,
        ScheduleResponse,
        ScheduleSegment,
        ScheduleStop,
        ScheduleTransit,
        StopMarker,
        MapMarker,
    )


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


def db_enabled() -> bool:
    dsn, _, _ = resolve_db_dsn()
    return bool(dsn)


def connect():
    dsn, username, password = resolve_db_dsn()
    if not dsn:
        raise RuntimeError("Database DSN is not configured")
    kwargs: dict[str, Any] = {"conninfo": dsn, "autocommit": False, "row_factory": dict_row}
    parsed = urlsplit(dsn)
    if username and parsed.username is None:
        kwargs["user"] = username
    if password and parsed.password is None:
        kwargs["password"] = password
    return psycopg.connect(**kwargs)


def save_preview(preview: SchedulePreviewResponse, request: SchedulePreviewCreateRequest) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule_previews (
                    id, status, start_date, end_date, time_zone, lodging_mode, route_coverage,
                    input_json, resolved_days_json, resolved_end_constraint_json, applied_defaults_json,
                    interpreted_prompt_json, warnings_json, conflicts_json, expires_at, consumed_at, created_at
                ) VALUES (
                    %(id)s, %(status)s, %(start_date)s, %(end_date)s, %(time_zone)s, %(lodging_mode)s, %(route_coverage)s,
                    %(input_json)s, %(resolved_days_json)s, %(resolved_end_constraint_json)s, %(applied_defaults_json)s,
                    %(interpreted_prompt_json)s, %(warnings_json)s, %(conflicts_json)s, %(expires_at)s, %(consumed_at)s, %(created_at)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    resolved_days_json = EXCLUDED.resolved_days_json,
                    resolved_end_constraint_json = EXCLUDED.resolved_end_constraint_json,
                    applied_defaults_json = EXCLUDED.applied_defaults_json,
                    interpreted_prompt_json = EXCLUDED.interpreted_prompt_json,
                    warnings_json = EXCLUDED.warnings_json,
                    conflicts_json = EXCLUDED.conflicts_json,
                    expires_at = EXCLUDED.expires_at,
                    consumed_at = EXCLUDED.consumed_at
                """,
                {
                    "id": preview.preview_id,
                    "status": preview.status,
                    "start_date": preview.resolved_days[0].date if preview.resolved_days else request.start_date,
                    "end_date": preview.resolved_days[-1].date if preview.resolved_days else request.end_date,
                    "time_zone": preview.time_zone,
                    "lodging_mode": preview.lodging_mode,
                    "route_coverage": preview.route_coverage,
                    "input_json": json_dumps_model(request),
                    "resolved_days_json": json.dumps([dump_model(day) for day in preview.resolved_days], ensure_ascii=False),
                    "resolved_end_constraint_json": json.dumps(
                        dump_model(preview.resolved_end_constraint) if preview.resolved_end_constraint else None,
                        ensure_ascii=False,
                    ),
                    "applied_defaults_json": json.dumps([dump_model(item) for item in preview.applied_defaults], ensure_ascii=False),
                    "interpreted_prompt_json": json_dumps_model(preview.interpreted_prompt),
                    "warnings_json": json.dumps([dump_model(item) for item in preview.warnings], ensure_ascii=False),
                    "conflicts_json": json.dumps([dump_model(item) for item in preview.conflicts], ensure_ascii=False),
                    "expires_at": preview.expires_at,
                    "consumed_at": None if preview.schedule_id is None else datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc),
                },
            )
        conn.commit()


def load_preview(preview_id: UUID) -> tuple[SchedulePreviewResponse, SchedulePreviewCreateRequest]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT preview.*, schedule.id AS schedule_id
                FROM schedule_previews preview
                LEFT JOIN schedules schedule ON schedule.preview_id = preview.id
                WHERE preview.id = %s
                """,
                (preview_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Schedule preview not found")
    preview = SchedulePreviewResponse.model_validate(
        {
            "previewId": row["id"],
            "status": row["status"],
            "canGenerate": row["status"] == "READY",
            "expiresAt": row["expires_at"],
            "timeZone": row["time_zone"],
            "lodgingMode": row["lodging_mode"],
            "routeCoverage": row["route_coverage"],
            "resolvedDays": json.loads(row["resolved_days_json"]),
            "resolvedEndConstraint": json.loads(row["resolved_end_constraint_json"]) if row["resolved_end_constraint_json"] else None,
            "appliedDefaults": json.loads(row["applied_defaults_json"]),
            "interpretedPrompt": json.loads(row["interpreted_prompt_json"]),
            "warnings": json.loads(row["warnings_json"]),
            "conflicts": json.loads(row["conflicts_json"]),
            "scheduleId": row["schedule_id"],
        }
    )
    request = SchedulePreviewCreateRequest.model_validate(json.loads(row["input_json"]))
    return preview, request


def mark_preview_consumed(preview_id: UUID) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_previews SET status = 'CONSUMED', consumed_at = %s WHERE id = %s",
                (datetime.now(timezone.utc), preview_id),
            )
        conn.commit()


def save_schedule(schedule: ScheduleResponse, condition_request: ScheduleCreateRequest) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            place_ids = sorted({stop.place.id for day in schedule.days for stop in day.stops if stop.place.id is not None})
            if place_ids:
                cur.execute("SELECT id FROM places WHERE id = ANY(%s)", (place_ids,))
                found_ids = {int(row["id"]) for row in cur.fetchall()}
                missing = [place_id for place_id in place_ids if place_id not in found_ids]
                if missing:
                    raise RuntimeError(f"Missing place ids for schedule persistence: {missing[:5]}")

            delete_schedule_children(cur, schedule.id)
            cur.execute(
                """
                INSERT INTO schedules (
                    id, status, start_date, end_date, daily_start_time, daily_end_time,
                    start_place_name, start_longitude, start_latitude, end_place_name, end_longitude, end_latitude,
                    preview_id, time_zone, lodging_mode, route_coverage, planning_warnings_json,
                    style_summary, condition_json, created_at, updated_at
                ) VALUES (
                    %(id)s, %(status)s, %(start_date)s, %(end_date)s, %(daily_start_time)s, %(daily_end_time)s,
                    %(start_place_name)s, %(start_longitude)s, %(start_latitude)s, %(end_place_name)s, %(end_longitude)s, %(end_latitude)s,
                    %(preview_id)s, %(time_zone)s, %(lodging_mode)s, %(route_coverage)s, %(planning_warnings_json)s,
                    %(style_summary)s, %(condition_json)s, COALESCE((SELECT created_at FROM schedules WHERE id = %(id)s), %(now)s), %(now)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    daily_start_time = EXCLUDED.daily_start_time,
                    daily_end_time = EXCLUDED.daily_end_time,
                    start_place_name = EXCLUDED.start_place_name,
                    start_longitude = EXCLUDED.start_longitude,
                    start_latitude = EXCLUDED.start_latitude,
                    end_place_name = EXCLUDED.end_place_name,
                    end_longitude = EXCLUDED.end_longitude,
                    end_latitude = EXCLUDED.end_latitude,
                    preview_id = EXCLUDED.preview_id,
                    time_zone = EXCLUDED.time_zone,
                    lodging_mode = EXCLUDED.lodging_mode,
                    route_coverage = EXCLUDED.route_coverage,
                    planning_warnings_json = EXCLUDED.planning_warnings_json,
                    style_summary = EXCLUDED.style_summary,
                    condition_json = EXCLUDED.condition_json,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "id": schedule.id,
                    "status": schedule.status,
                    "start_date": schedule.start_date,
                    "end_date": schedule.end_date,
                    "daily_start_time": schedule.daily_start_time,
                    "daily_end_time": schedule.daily_end_time,
                    "start_place_name": schedule.days[0].start_location.name if schedule.days and schedule.days[0].start_location else "출발지",
                    "start_longitude": schedule.days[0].start_location.longitude if schedule.days and schedule.days[0].start_location else None,
                    "start_latitude": schedule.days[0].start_location.latitude if schedule.days and schedule.days[0].start_location else None,
                    "end_place_name": schedule.days[-1].end_location.name if schedule.days and schedule.days[-1].end_location else None,
                    "end_longitude": schedule.days[-1].end_location.longitude if schedule.days and schedule.days[-1].end_location else None,
                    "end_latitude": schedule.days[-1].end_location.latitude if schedule.days and schedule.days[-1].end_location else None,
                    "preview_id": schedule.preview_id,
                    "time_zone": schedule.planning_assumptions.time_zone if schedule.planning_assumptions else "Asia/Seoul",
                    "lodging_mode": schedule.planning_assumptions.lodging_mode if schedule.planning_assumptions else "UNSPECIFIED",
                    "route_coverage": schedule.planning_assumptions.route_coverage if schedule.planning_assumptions else "SKELETON_ONLY",
                    "planning_warnings_json": json.dumps(
                        schedule.planning_assumptions.warnings if schedule.planning_assumptions else [],
                        ensure_ascii=False,
                    ),
                    "style_summary": schedule.style_summary,
                    "condition_json": json_dumps_model(condition_request),
                    "now": datetime.now(),
                },
            )
            for day in schedule.days:
                save_day(cur, schedule.id, day)
        conn.commit()


def delete_schedule_children(cur, schedule_id: UUID) -> None:
    cur.execute("DELETE FROM schedule_fixed_events WHERE schedule_id = %s", (schedule_id,))
    cur.execute(
        """
        DELETE FROM transit_route_lines
        WHERE transit_route_id IN (
            SELECT route.id
            FROM transit_routes route
            JOIN schedule_days day ON day.id = route.schedule_day_id
            WHERE day.schedule_id = %s
        )
        """,
        (schedule_id,),
    )
    cur.execute(
        """
        DELETE FROM transit_segments
        WHERE transit_route_id IN (
            SELECT route.id
            FROM transit_routes route
            JOIN schedule_days day ON day.id = route.schedule_day_id
            WHERE day.schedule_id = %s
        )
        """,
        (schedule_id,),
    )
    cur.execute(
        "DELETE FROM transit_routes WHERE schedule_day_id IN (SELECT id FROM schedule_days WHERE schedule_id = %s)",
        (schedule_id,),
    )
    cur.execute(
        "DELETE FROM schedule_stops WHERE schedule_day_id IN (SELECT id FROM schedule_days WHERE schedule_id = %s)",
        (schedule_id,),
    )
    cur.execute("DELETE FROM schedule_days WHERE schedule_id = %s", (schedule_id,))


def save_day(cur, schedule_id: UUID, day: ScheduleDay) -> None:
    cur.execute(
        """
        INSERT INTO schedule_days (
            id, schedule_id, day_no, date, start_time, end_time,
            start_place_name, start_longitude, start_latitude, end_place_name, end_longitude, end_latitude,
            start_location_source, end_location_source
        ) VALUES (
            %(id)s, %(schedule_id)s, %(day_no)s, %(date)s, %(start_time)s, %(end_time)s,
            %(start_place_name)s, %(start_longitude)s, %(start_latitude)s, %(end_place_name)s, %(end_longitude)s, %(end_latitude)s,
            %(start_location_source)s, %(end_location_source)s
        )
        """,
        {
            "id": uuid_from_day(day),
            "schedule_id": schedule_id,
            "day_no": day.day_no,
            "date": day.date,
            "start_time": day.start_time,
            "end_time": day.end_time,
            "start_place_name": day.start_location.name if day.start_location else None,
            "start_longitude": day.start_location.longitude if day.start_location else None,
            "start_latitude": day.start_location.latitude if day.start_location else None,
            "end_place_name": day.end_location.name if day.end_location else None,
            "end_longitude": day.end_location.longitude if day.end_location else None,
            "end_latitude": day.end_location.latitude if day.end_location else None,
            "start_location_source": day.start_location_source or "REQUEST",
            "end_location_source": day.end_location_source or "REQUEST",
        },
    )
    for stop in day.stops:
        save_stop(cur, schedule_id, day, stop)
    if day.final_transit is not None:
        save_transit(cur, day, None, day.final_transit)


def save_stop(cur, schedule_id: UUID, day: ScheduleDay, stop: ScheduleStop) -> None:
    cur.execute(
        """
        INSERT INTO schedule_stops (
            id, schedule_day_id, place_id, stop_order, stay_minutes, selection_reasons_json, warnings_json,
            fixed_starts_at, fixed_ends_at
        ) VALUES (
            %(id)s, %(schedule_day_id)s, %(place_id)s, %(stop_order)s, %(stay_minutes)s, %(selection_reasons_json)s, %(warnings_json)s,
            %(fixed_starts_at)s, %(fixed_ends_at)s
        )
        """,
        {
            "id": stop.id,
            "schedule_day_id": uuid_from_day(day),
            "place_id": stop.place.id,
            "stop_order": stop.order,
            "stay_minutes": stop.stay_minutes,
            "selection_reasons_json": json.dumps(stop.selection_reasons, ensure_ascii=False),
            "warnings_json": json.dumps(stop.warnings, ensure_ascii=False),
            "fixed_starts_at": as_offset(stop.fixed_starts_at),
            "fixed_ends_at": as_offset(stop.fixed_ends_at),
        },
    )
    if stop.fixed_starts_at is not None and stop.fixed_ends_at is not None:
        cur.execute(
            """
            INSERT INTO schedule_fixed_events (
                id, schedule_id, schedule_stop_id, client_event_id, name, starts_at, ends_at, created_at
            ) VALUES (
                gen_random_uuid(), %(schedule_id)s, %(schedule_stop_id)s, %(client_event_id)s, %(name)s, %(starts_at)s, %(ends_at)s, %(created_at)s
            )
            """,
            {
                "schedule_id": schedule_id,
                "schedule_stop_id": stop.id,
                "client_event_id": stop.id.hex,
                "name": stop.place.name,
                "starts_at": as_offset(stop.fixed_starts_at),
                "ends_at": as_offset(stop.fixed_ends_at),
                "created_at": datetime.now(timezone.utc),
            },
        )
    if stop.inbound_transit is not None:
        save_transit(cur, day, stop, stop.inbound_transit)


def save_transit(cur, day: ScheduleDay, stop: ScheduleStop | None, transit: ScheduleTransit) -> None:
    route_id = deterministic_route_id(day.day_no, stop.id if stop else None, transit.route_order, transit.route_type or "FINAL")
    cur.execute(
        """
        INSERT INTO transit_routes (
            id, schedule_day_id, schedule_stop_id, route_type, route_order, total_minutes, fare_amount,
            provider, realtime_status, fallback_used, warnings_json, raw_json
        ) VALUES (
            %(id)s, %(schedule_day_id)s, %(schedule_stop_id)s, %(route_type)s, %(route_order)s, %(total_minutes)s, %(fare_amount)s,
            %(provider)s, %(realtime_status)s, %(fallback_used)s, %(warnings_json)s, %(raw_json)s
        )
        """,
        {
            "id": route_id,
            "schedule_day_id": uuid_from_day(day),
            "schedule_stop_id": stop.id if stop else None,
            "route_type": transit.route_type or ("FINAL" if stop is None else "INBOUND"),
            "route_order": transit.route_order or (stop.order if stop else len(day.stops) + 1),
            "total_minutes": transit.total_minutes,
            "fare_amount": transit.fare_amount,
            "provider": transit.provider or "FASTAPI_MIGRATION",
            "realtime_status": transit.realtime_status,
            "fallback_used": transit.fallback_used,
            "warnings_json": json.dumps(transit.warnings, ensure_ascii=False),
            "raw_json": json_dumps_model(transit),
        },
    )
    for segment in transit.segments:
        cur.execute(
            """
            INSERT INTO transit_segments (
                id, transit_route_id, segment_order, mode, line_name, start_station_id, start_station_name,
                end_station_id, end_station_name, instruction, duration_minutes, distance_meters,
                station_count, wait_minutes, realtime_status
            ) VALUES (
                gen_random_uuid(), %(transit_route_id)s, %(segment_order)s, %(mode)s, %(line_name)s, %(start_station_id)s, %(start_station_name)s,
                %(end_station_id)s, %(end_station_name)s, %(instruction)s, %(duration_minutes)s, %(distance_meters)s,
                %(station_count)s, %(wait_minutes)s, %(realtime_status)s
            )
            """,
            {
                "transit_route_id": route_id,
                "segment_order": segment.order,
                "mode": segment.mode,
                "line_name": segment.line_name,
                "start_station_id": segment.start_station_id,
                "start_station_name": segment.start_station_name,
                "end_station_id": segment.end_station_id,
                "end_station_name": segment.end_station_name,
                "instruction": segment.instruction,
                "duration_minutes": segment.duration_minutes,
                "distance_meters": segment.distance_meters,
                "station_count": segment.station_count,
                "wait_minutes": segment.wait_minutes,
                "realtime_status": segment.realtime_status,
            },
        )
    cur.execute(
        """
        INSERT INTO transit_route_lines (
            id, transit_route_id, line_order, mode, line_name, coordinates_json, duration_minutes, distance_meters, instruction, fallback_used
        ) VALUES (
            gen_random_uuid(), %(transit_route_id)s, %(line_order)s, %(mode)s, %(line_name)s, %(coordinates_json)s, %(duration_minutes)s,
            %(distance_meters)s, %(instruction)s, %(fallback_used)s
        )
        """,
        {
            "transit_route_id": route_id,
            "line_order": 1,
            "mode": transit.route_type or ("FINAL" if stop is None else "INBOUND"),
            "line_name": transit.summary or transit.destination_name,
            "coordinates_json": "[]",
            "duration_minutes": transit.total_minutes,
            "distance_meters": None,
            "instruction": transit.summary,
            "fallback_used": transit.fallback_used,
        },
    )


def load_schedule(schedule_id: UUID) -> ScheduleResponse:
    items = load_schedules([schedule_id]).items
    if not items:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return items[0]


def list_schedules() -> ScheduleListResponse:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM schedules ORDER BY start_date ASC, created_at DESC")
            ids = [row["id"] for row in cur.fetchall()]
    return load_schedules(ids)


def load_schedules(schedule_ids: list[UUID]) -> ScheduleListResponse:
    if not schedule_ids:
        return ScheduleListResponse(items=[])
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM schedules WHERE id = ANY(%s)", (schedule_ids,))
            schedule_rows = {row["id"]: row for row in cur.fetchall()}
            cur.execute("SELECT * FROM schedule_days WHERE schedule_id = ANY(%s) ORDER BY day_no ASC", (schedule_ids,))
            day_rows = cur.fetchall()
            day_ids = [row["id"] for row in day_rows]
            cur.execute(
                """
                SELECT stop.*, place.name AS place_name, place.category, place.address, place.longitude, place.latitude,
                       place.primary_image_url, place.content_type_id,
                       info.opening_hours_text, info.closed_days_text, info.requires_manual_check
                FROM schedule_stops stop
                JOIN places place ON place.id = stop.place_id
                LEFT JOIN place_operating_infos info ON info.place_id = place.id
                WHERE stop.schedule_day_id = ANY(%s)
                ORDER BY stop.stop_order ASC
                """,
                (day_ids or [UUID(int=0)],),
            )
            stop_rows = cur.fetchall()
            stop_ids = [row["id"] for row in stop_rows]
            cur.execute(
                "SELECT * FROM schedule_fixed_events WHERE schedule_stop_id = ANY(%s)",
                (stop_ids or [UUID(int=0)],),
            )
            fixed_rows = {row["schedule_stop_id"]: row for row in cur.fetchall()}
            cur.execute(
                "SELECT * FROM transit_routes WHERE schedule_day_id = ANY(%s) ORDER BY route_order ASC",
                (day_ids or [UUID(int=0)],),
            )
            route_rows = cur.fetchall()
            route_ids = [row["id"] for row in route_rows]
            cur.execute(
                "SELECT * FROM transit_segments WHERE transit_route_id = ANY(%s) ORDER BY segment_order ASC",
                (route_ids or [UUID(int=0)],),
            )
            segment_rows = cur.fetchall()
            cur.execute(
                "SELECT * FROM transit_route_lines WHERE transit_route_id = ANY(%s) ORDER BY line_order ASC",
                (route_ids or [UUID(int=0)],),
            )
            line_rows = cur.fetchall()

    segments_by_route: dict[UUID, list[dict[str, Any]]] = {}
    for row in segment_rows:
        segments_by_route.setdefault(row["transit_route_id"], []).append(row)
    lines_by_route: dict[UUID, list[dict[str, Any]]] = {}
    for row in line_rows:
        lines_by_route.setdefault(row["transit_route_id"], []).append(row)
    routes_by_day: dict[UUID, list[dict[str, Any]]] = {}
    routes_by_stop: dict[UUID, dict[str, Any]] = {}
    for row in route_rows:
        row["segments"] = segments_by_route.get(row["id"], [])
        row["lines"] = lines_by_route.get(row["id"], [])
        routes_by_day.setdefault(row["schedule_day_id"], []).append(row)
        if row["schedule_stop_id"] is not None:
            routes_by_stop[row["schedule_stop_id"]] = row
    stops_by_day: dict[UUID, list[ScheduleStop]] = {}
    for row in stop_rows:
        place = SchedulePlace(
            id=row["place_id"],
            name=row["place_name"],
            category=row["content_type_id"],
            categoryLabel=row["category"] or "미확인",
            address=row["address"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            primaryImageUrl=row["primary_image_url"],
            operatingInfo=OperatingInfo(
                openingHoursText=row["opening_hours_text"],
                closedDaysText=row["closed_days_text"],
                requiresManualCheck=bool(row["requires_manual_check"]) if row["requires_manual_check"] is not None else True,
            ) if row["opening_hours_text"] is not None or row["closed_days_text"] is not None else None,
        )
        fixed = fixed_rows.get(row["id"])
        inbound = route_to_model(routes_by_stop.get(row["id"]))
        stop = ScheduleStop(
            id=row["id"],
            order=row["stop_order"],
            stayMinutes=row["stay_minutes"],
            place=place,
            inboundTransit=inbound,
            selectionReasons=json.loads(row["selection_reasons_json"] or "[]"),
            warnings=json.loads(row["warnings_json"] or "[]"),
            fixedStartsAt=fixed["starts_at"] if fixed else row["fixed_starts_at"],
            fixedEndsAt=fixed["ends_at"] if fixed else row["fixed_ends_at"],
        )
        stops_by_day.setdefault(row["schedule_day_id"], []).append(stop)

    days_by_schedule: dict[UUID, list[ScheduleDay]] = {}
    for row in day_rows:
        final_route = next((route for route in routes_by_day.get(row["id"], []) if route["schedule_stop_id"] is None), None)
        day = ScheduleDay(
            dayNo=row["day_no"],
            date=row["date"],
            startTime=row["start_time"],
            endTime=row["end_time"],
            startLocation=DayLocation(name=row["start_place_name"], longitude=row["start_longitude"], latitude=row["start_latitude"]) if row["start_place_name"] else None,
            endLocation=DayLocation(name=row["end_place_name"], longitude=row["end_longitude"], latitude=row["end_latitude"]) if row["end_place_name"] else None,
            startLocationSource=row["start_location_source"],
            endLocationSource=row["end_location_source"],
            summary=f"{len(stops_by_day.get(row['id'], []))}개 방문지",
            stops=stops_by_day.get(row["id"], []),
            finalTransit=route_to_model(final_route),
        )
        days_by_schedule.setdefault(row["schedule_id"], []).append(day)

    items: list[ScheduleResponse] = []
    for schedule_id in schedule_ids:
        row = schedule_rows.get(schedule_id)
        if row is None:
            continue
        warnings = json.loads(row["planning_warnings_json"] or "[]")
        items.append(
            ScheduleResponse(
                id=row["id"],
                status=row["status"],
                startDate=row["start_date"],
                endDate=row["end_date"],
                dailyStartTime=row["daily_start_time"],
                dailyEndTime=row["daily_end_time"],
                styleSummary=row["style_summary"] or "",
                days=days_by_schedule.get(row["id"], []),
                evaluation=None,
                previewId=row["preview_id"],
                planningAssumptions=PlanningAssumptions(
                    timeZone=row["time_zone"] or "Asia/Seoul",
                    lodgingMode=row["lodging_mode"] or "UNSPECIFIED",
                    routeCoverage=row["route_coverage"] or "SKELETON_ONLY",
                    warnings=warnings,
                ),
            )
        )
    return ScheduleListResponse(items=items)


def route_to_model(row: dict[str, Any] | None) -> ScheduleTransit | None:
    if row is None:
        return None
    segments = [
        ScheduleSegment(
            order=segment["segment_order"],
            mode=segment["mode"],
            lineName=segment["line_name"],
            startStationId=segment["start_station_id"],
            startStationName=segment["start_station_name"],
            endStationId=segment["end_station_id"],
            endStationName=segment["end_station_name"],
            instruction=segment["instruction"],
            durationMinutes=segment["duration_minutes"],
            distanceMeters=segment["distance_meters"],
            stationCount=segment["station_count"],
            waitMinutes=segment["wait_minutes"],
            realtimeStatus=segment["realtime_status"],
        )
        for segment in row.get("segments", [])
    ]
    raw = json.loads(row["raw_json"]) if row.get("raw_json") else {}
    return ScheduleTransit.model_validate(
        {
            "routeType": row["route_type"],
            "routeOrder": row["route_order"],
            "originName": raw.get("originName"),
            "destinationName": raw.get("destinationName"),
            "summary": raw.get("summary"),
            "departAt": raw.get("departAt"),
            "arriveAt": raw.get("arriveAt"),
            "totalMinutes": row["total_minutes"],
            "walkMinutes": raw.get("walkMinutes", row["total_minutes"]),
            "waitMinutes": raw.get("waitMinutes", 0),
            "transferCount": raw.get("transferCount", 0),
            "fareAmount": row["fare_amount"],
            "provider": row["provider"],
            "realtimeStatus": row["realtime_status"],
            "fallbackUsed": row["fallback_used"],
            "segments": [dump_model(segment) for segment in segments],
            "warnings": json.loads(row["warnings_json"] or "[]"),
        }
    )


def json_dumps_model(model: Any) -> str:
    return json.dumps(dump_model(model), ensure_ascii=False)


def dump_model(model: Any) -> Any:
    if model is None:
        return None
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", by_alias=True)
    return model


def uuid_from_day(day: ScheduleDay) -> UUID:
    namespace = UUID("12345678-1234-5678-1234-567812345678")
    import uuid as uuid_pkg

    return uuid_pkg.uuid5(namespace, f"{day.date.isoformat()}:{day.day_no}:{day.start_time}:{day.end_time}")


def deterministic_route_id(day_no: int, stop_id: UUID | None, route_order: int, route_type: str) -> UUID:
    namespace = UUID("87654321-4321-8765-4321-876543218765")
    import uuid as uuid_pkg

    return uuid_pkg.uuid5(namespace, f"{day_no}:{stop_id}:{route_order}:{route_type}")


def as_offset(value: datetime | None):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
