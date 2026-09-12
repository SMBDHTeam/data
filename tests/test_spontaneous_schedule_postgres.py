"""Opt-in PostgreSQL integration tests for spontaneous schedule persistence.

The suite only runs when both environment variables below are set so it cannot
accidentally touch a shared database::

    SPONTANEOUS_TEST_POSTGRES_ISOLATED=1
    SPONTANEOUS_TEST_POSTGRES_DSN=postgresql://...

Each run creates a uniquely named schema and drops it after the suite.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from unittest import TestCase, mock, skipUnless
from uuid import UUID, uuid4

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from schedule.persistence import load_schedule, save_spontaneous_schedule, uuid_from_day
from spontaneous.schedule_service import schedule_from_snapshot
from tests.test_spontaneous_schedule_save import snapshot


POSTGRES_DSN = os.getenv("SPONTANEOUS_TEST_POSTGRES_DSN")
POSTGRES_ISOLATED = os.getenv("SPONTANEOUS_TEST_POSTGRES_ISOLATED") == "1"


SCHEMA_DDL = """
CREATE TABLE schedule_creation_requests (
    id UUID PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL,
    preview_id UUID,
    request_hash VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    schedule_id UUID,
    response_status INTEGER,
    response_json TEXT,
    last_error_code VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    user_id BIGINT NOT NULL,
    request_type VARCHAR(32) NOT NULL,
    spontaneous_preview_id UUID
);
CREATE UNIQUE INDEX uq_schedule_creation_requests_user_key
    ON schedule_creation_requests (user_id, idempotency_key)
    WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX uq_schedule_creation_requests_user_preview
    ON schedule_creation_requests (user_id, spontaneous_preview_id)
    WHERE user_id IS NOT NULL AND spontaneous_preview_id IS NOT NULL;

CREATE TABLE places (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    external_content_id VARCHAR(255) NOT NULL,
    content_type_id VARCHAR(32),
    name VARCHAR(255) NOT NULL,
    category VARCHAR(255),
    address VARCHAR(255),
    longitude NUMERIC(11, 8) NOT NULL,
    latitude NUMERIC(10, 8) NOT NULL,
    primary_image_url TEXT,
    source_modified_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    last_synced_at TIMESTAMP,
    ingestion_status VARCHAR(32),
    ingestion_retry_count INTEGER,
    ingestion_last_error TEXT,
    ingestion_next_retry_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    hidden_at TIMESTAMP,
    UNIQUE (source, external_content_id)
);
CREATE TABLE place_operating_infos (
    place_id BIGINT PRIMARY KEY REFERENCES places(id),
    opening_hours_text TEXT,
    closed_days_text TEXT,
    requires_manual_check BOOLEAN
);

CREATE TABLE schedules (
    id UUID PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    daily_start_time TIME NOT NULL,
    daily_end_time TIME NOT NULL,
    start_place_name VARCHAR(255),
    start_longitude NUMERIC(11, 8),
    start_latitude NUMERIC(10, 8),
    end_place_name VARCHAR(255),
    end_longitude NUMERIC(11, 8),
    end_latitude NUMERIC(10, 8),
    preview_id UUID,
    time_zone VARCHAR(64),
    lodging_mode VARCHAR(64),
    route_coverage VARCHAR(64),
    planning_warnings_json TEXT,
    style_summary TEXT,
    condition_json TEXT,
    user_id BIGINT NOT NULL,
    schedule_type VARCHAR(32) NOT NULL DEFAULT 'PLANNED',
    transport_mode VARCHAR(32),
    start_at TIMESTAMPTZ,
    return_by TIMESTAMPTZ,
    estimated_return_at TIMESTAMPTZ,
    spontaneous_metadata_json TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE schedule_days (
    id UUID PRIMARY KEY,
    schedule_id UUID NOT NULL REFERENCES schedules(id),
    day_no INTEGER NOT NULL,
    date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    start_place_name VARCHAR(255),
    start_longitude NUMERIC(11, 8),
    start_latitude NUMERIC(10, 8),
    end_place_name VARCHAR(255),
    end_longitude NUMERIC(11, 8),
    end_latitude NUMERIC(10, 8),
    start_location_source VARCHAR(32),
    end_location_source VARCHAR(32)
);
CREATE TABLE schedule_stops (
    id UUID PRIMARY KEY,
    schedule_day_id UUID NOT NULL REFERENCES schedule_days(id),
    place_id BIGINT NOT NULL REFERENCES places(id),
    stop_order INTEGER NOT NULL,
    stay_minutes INTEGER NOT NULL,
    arrive_at TIME,
    depart_at TIME,
    arrive_at_datetime TIMESTAMPTZ,
    depart_at_datetime TIMESTAMPTZ,
    role VARCHAR(64),
    themes_json TEXT,
    selection_reasons_json TEXT,
    warnings_json TEXT,
    fixed_starts_at TIMESTAMPTZ,
    fixed_ends_at TIMESTAMPTZ
);
CREATE TABLE schedule_fixed_events (
    id UUID PRIMARY KEY,
    schedule_id UUID NOT NULL REFERENCES schedules(id),
    schedule_stop_id UUID NOT NULL REFERENCES schedule_stops(id),
    client_event_id VARCHAR(255),
    name VARCHAR(255),
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ
);
CREATE TABLE transit_routes (
    id UUID PRIMARY KEY,
    schedule_day_id UUID NOT NULL REFERENCES schedule_days(id),
    schedule_stop_id UUID REFERENCES schedule_stops(id),
    route_type VARCHAR(32),
    route_order INTEGER,
    total_minutes INTEGER,
    fare_amount INTEGER,
    provider VARCHAR(64),
    realtime_status VARCHAR(64),
    fallback_used BOOLEAN,
    warnings_json TEXT,
    raw_json TEXT,
    depart_at_datetime TIMESTAMPTZ,
    arrive_at_datetime TIMESTAMPTZ
);
CREATE TABLE transit_segments (
    id UUID PRIMARY KEY,
    transit_route_id UUID NOT NULL REFERENCES transit_routes(id),
    segment_order INTEGER,
    mode VARCHAR(64),
    line_name VARCHAR(255),
    start_station_id VARCHAR(255),
    start_station_name VARCHAR(255),
    end_station_id VARCHAR(255),
    end_station_name VARCHAR(255),
    instruction TEXT,
    duration_minutes INTEGER,
    distance_meters INTEGER,
    station_count INTEGER,
    wait_minutes INTEGER,
    realtime_status VARCHAR(64)
);
CREATE TABLE transit_route_lines (
    id UUID PRIMARY KEY,
    transit_route_id UUID NOT NULL REFERENCES transit_routes(id),
    line_order INTEGER,
    mode VARCHAR(64),
    line_name VARCHAR(255),
    coordinates_json TEXT,
    duration_minutes INTEGER,
    distance_meters INTEGER,
    instruction TEXT,
    fallback_used BOOLEAN
);
"""


@skipUnless(
    POSTGRES_DSN and POSTGRES_ISOLATED,
    "requires an explicitly marked isolated PostgreSQL database",
)
class SpontaneousSchedulePostgresIntegrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = f"spontaneous_test_{uuid4().hex}"
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{cls.schema}"')
        with cls.connect() as conn:
            conn.execute(SCHEMA_DDL)

    @classmethod
    def tearDownClass(cls):
        try:
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        finally:
            super().tearDownClass()

    @classmethod
    def connect(cls):
        return psycopg.connect(
            POSTGRES_DSN,
            row_factory=dict_row,
            options=f"-c search_path={cls.schema},public",
        )

    def make_snapshot(self, content_id: str):
        value = deepcopy(snapshot())
        value["course"][0]["contentId"] = content_id
        value["course"][0]["placeSnapshot"]["externalContentId"] = content_id
        return value

    def save(self, content_id: str, preview_id: UUID, key: str, request_hash: str):
        value = self.make_snapshot(content_id)
        schedule, places = schedule_from_snapshot(value, preview_id)
        return save_spontaneous_schedule(
            schedule,
            places,
            owner_id=7,
            idempotency_key=key,
            request_hash=request_hash,
            spontaneous_preview_id=preview_id,
        )

    def count(self, table: str, where: str = "TRUE", parameters=()):
        allowed = {
            "schedule_creation_requests",
            "places",
            "schedules",
            "schedule_days",
            "schedule_stops",
            "transit_routes",
        }
        if table not in allowed:
            raise ValueError("unsupported table")
        with self.connect() as conn:
            return conn.execute(
                f"SELECT count(*) AS count FROM {table} WHERE {where}", parameters
            ).fetchone()["count"]

    def test_concurrent_different_keys_create_only_one_schedule_for_preview(self):
        preview_id = uuid4()

        def attempt(key):
            try:
                return self.save("concurrent-place", preview_id, key, "same-request")
            except HTTPException as exception:
                return exception

        with mock.patch("schedule.persistence.connect", side_effect=self.connect):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, ("concurrent-a", "concurrent-b")))

            schedule_id = next(result for result in results if isinstance(result, UUID))
            winning_key = "concurrent-a" if results[0] == schedule_id else "concurrent-b"
            duplicate = next(
                result for result in results if isinstance(result, HTTPException)
            )
            self.assertEqual(duplicate.detail, "SPONTANEOUS_PREVIEW_ALREADY_SAVED")
            self.assertEqual(duplicate.headers["X-Schedule-Id"], str(schedule_id))
            self.assertEqual(
                self.save(
                    "concurrent-place", preview_id, winning_key, "same-request"
                ),
                schedule_id,
            )

            with self.assertRaises(HTTPException) as reused:
                self.save(
                    "different-request-place",
                    uuid4(),
                    winning_key,
                    "different-request",
                )

        self.assertEqual(reused.exception.detail, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.count("schedules", "id = %s", (schedule_id,)), 1)
        self.assertEqual(
            self.count("schedule_creation_requests", "spontaneous_preview_id = %s", (preview_id,)),
            1,
        )

    def test_concurrent_same_key_replays_one_completed_schedule(self):
        preview_id = uuid4()

        def attempt(_):
            return self.save(
                "same-key-place", preview_id, "same-key", "same-key-request"
            )

        with mock.patch("schedule.persistence.connect", side_effect=self.connect):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, range(2)))

        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count("schedules", "id = %s", (results[0],)), 1)
        self.assertEqual(
            self.count(
                "schedule_creation_requests",
                "user_id = %s AND idempotency_key = %s",
                (7, "same-key"),
            ),
            1,
        )

    def test_failure_rolls_back_request_place_schedule_day_stop_and_route(self):
        preview_id = uuid4()
        value = self.make_snapshot("rollback-place")
        schedule, places = schedule_from_snapshot(value, preview_id)
        schedule_day_id = uuid_from_day(schedule.id, schedule.days[0])

        with mock.patch("schedule.persistence.connect", side_effect=self.connect), mock.patch(
            "schedule.persistence.save_transit", side_effect=RuntimeError("forced transit failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "forced transit failure"):
                save_spontaneous_schedule(
                    schedule,
                    places,
                    owner_id=7,
                    idempotency_key="rollback-key",
                    request_hash="rollback-request",
                    spontaneous_preview_id=preview_id,
                )

        self.assertEqual(
            self.count(
                "schedule_creation_requests",
                "spontaneous_preview_id = %s",
                (preview_id,),
            ),
            0,
        )
        self.assertEqual(
            self.count("places", "external_content_id = %s", ("rollback-place",)),
            0,
        )
        self.assertEqual(self.count("schedules", "id = %s", (schedule.id,)), 0)
        self.assertEqual(self.count("schedule_days", "schedule_id = %s", (schedule.id,)), 0)
        self.assertEqual(
            self.count(
                "schedule_stops",
                "schedule_day_id = %s",
                (schedule_day_id,),
            ),
            0,
        )
        self.assertEqual(
            self.count(
                "transit_routes",
                "schedule_day_id = %s",
                (schedule_day_id,),
            ),
            0,
        )

        with mock.patch("schedule.persistence.connect", side_effect=self.connect):
            retried_schedule_id = save_spontaneous_schedule(
                schedule,
                places,
                owner_id=7,
                idempotency_key="rollback-key",
                request_hash="rollback-request",
                spontaneous_preview_id=preview_id,
            )

        self.assertEqual(retried_schedule_id, schedule.id)
        self.assertEqual(self.count("schedules", "id = %s", (schedule.id,)), 1)

    def test_hidden_tour_api_place_cannot_be_saved(self):
        preview_id = uuid4()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO places (
                    source, external_content_id, name, longitude, latitude, hidden_at, created_at, updated_at
                ) VALUES ('TOUR_API', 'hidden-place', 'hidden', 129.12, 35.15, now(), now(), now())
                """
            )

        with mock.patch("schedule.persistence.connect", side_effect=self.connect):
            with self.assertRaises(HTTPException) as hidden:
                self.save("hidden-place", preview_id, "hidden-key", "hidden-request")

        self.assertEqual(hidden.exception.status_code, 422)
        self.assertEqual(hidden.exception.detail, "SPONTANEOUS_PLACE_HIDDEN")
        self.assertEqual(
            self.count(
                "schedule_creation_requests",
                "spontaneous_preview_id = %s",
                (preview_id,),
            ),
            0,
        )

    def test_postgres_round_trip_keeps_cross_midnight_mode_and_return_limit(self):
        preview_id = uuid4()
        with mock.patch("schedule.persistence.connect", side_effect=self.connect):
            schedule_id = self.save(
                "round-trip-place",
                preview_id,
                "round-trip-key",
                "round-trip-request",
            )
            restored = load_schedule(schedule_id)

        self.assertEqual(restored.transport_mode, "CAR")
        self.assertEqual(restored.start_at.isoformat(), "2026-09-11T23:30:00+09:00")
        self.assertEqual(restored.return_by.isoformat(), "2026-09-12T02:00:00+09:00")
        self.assertEqual(
            restored.days[0].stops[0].arrive_at_datetime.isoformat(),
            "2026-09-12T00:05:00+09:00",
        )
        self.assertEqual(
            restored.days[0].final_transit.arrive_at_datetime.isoformat(),
            "2026-09-12T01:40:00+09:00",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
