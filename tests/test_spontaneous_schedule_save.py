from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock
from uuid import uuid4

from fastapi import HTTPException

from spontaneous.models import Coordinate, SpontaneousCourseRequest, TransportMode
from spontaneous.preview import create_preview_token, verify_preview_token
from spontaneous.schedule_service import schedule_from_snapshot, save_spontaneous_preview


KST = timezone(timedelta(hours=9))


def transit(route_type, route_order, departure, arrival):
    return {
        "routeType": route_type,
        "routeOrder": route_order,
        "originName": "출발지",
        "destinationName": "해변",
        "summary": "출발지 → 해변",
        "departAt": departure.time().isoformat(),
        "arriveAt": arrival.time().isoformat(),
        "departAtDateTime": departure.isoformat(),
        "arriveAtDateTime": arrival.isoformat(),
        "totalMinutes": int((arrival - departure).total_seconds() // 60),
        "walkMinutes": 0,
        "waitMinutes": 0,
        "transferCount": 0,
        "fareAmount": None,
        "provider": "TMAP",
        "realtimeStatus": "UNAVAILABLE",
        "fallbackUsed": True,
        "segments": [],
        "warnings": ["상세 선형 없음"],
        "route_lines": [{
            "mode": "CAR",
            "lineName": None,
            "startName": "출발지",
            "endName": "해변",
            "durationMinutes": int((arrival - departure).total_seconds() // 60),
            "distanceMeters": None,
            "instruction": "출발지 → 해변",
            "fallbackUsed": True,
            "coordinates": [[129.04, 35.11], [129.12, 35.15]],
        }],
    }


def snapshot():
    start = datetime(2026, 9, 11, 23, 30, tzinfo=KST)
    arrival = datetime(2026, 9, 12, 0, 5, tzinfo=KST)
    departure = datetime(2026, 9, 12, 1, 5, tzinfo=KST)
    returned = datetime(2026, 9, 12, 1, 40, tzinfo=KST)
    request = SpontaneousCourseRequest(
        destinationId="BUSAN_GWANGALLI",
        startLocation=Coordinate(latitude=35.11, longitude=129.04, name="부산역"),
        startAt=start,
        returnBy=datetime(2026, 9, 12, 2, 0, tzinfo=KST),
        desiredThemes=["SEA"],
        transportMode=TransportMode.CAR,
    )
    return {
        "request": request.model_dump(mode="json"),
        "destination": {"destinationId": "BUSAN_GWANGALLI", "name": "광안리·민락"},
        "course": [{
            "order": 1,
            "role": "ACTIVITY",
            "name": "해변",
            "contentId": "123",
            "contentTypeId": "12",
            "latitude": 35.15,
            "longitude": 129.12,
            "travelMinutesFromPrevious": 35,
            "arrivalAt": arrival.isoformat(),
            "departureAt": departure.isoformat(),
            "stayMinutes": 60,
            "themes": ["SEA"],
            "inboundTransit": transit("INBOUND", 1, start, arrival),
            "placeSnapshot": {
                "source": "TOUR_API",
                "externalContentId": "123",
                "contentTypeId": "12",
                "name": "해변",
                "category": "A01011200",
                "address": "부산광역시",
                "longitude": 129.12,
                "latitude": 35.15,
                "primaryImageUrl": "https://example.test/image.jpg",
            },
        }],
        "returnTravelMinutes": 35,
        "estimatedReturnAt": returned.isoformat(),
        "finalTransit": transit("FINAL", 2, departure, returned),
    }


class SpontaneousScheduleSaveTest(TestCase):
    def test_signed_preview_rejects_tampering_and_other_user(self):
        preview_id, token, _ = create_preview_token(snapshot(), owner_id=7)
        self.assertEqual(verify_preview_token(token, preview_id, 7), snapshot())

        with self.assertRaises(HTTPException) as tampered:
            verify_preview_token(token[:-1] + ("A" if token[-1] != "A" else "B"), preview_id, 7)
        self.assertEqual(tampered.exception.detail, "SPONTANEOUS_PREVIEW_INVALID")

        with self.assertRaises(HTTPException) as cross_user:
            verify_preview_token(token, preview_id, 8)
        self.assertEqual(cross_user.exception.status_code, 403)

    def test_signed_preview_rejects_expired_token(self):
        issued_at = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        with mock.patch("spontaneous.preview.datetime") as clock:
            clock.now.return_value = issued_at
            clock.fromisoformat.side_effect = datetime.fromisoformat
            preview_id, token, expires_at = create_preview_token(snapshot(), owner_id=7)
            clock.now.return_value = expires_at

            with self.assertRaises(HTTPException) as expired:
                verify_preview_token(token, preview_id, 7)

        self.assertEqual(expired.exception.status_code, 410)
        self.assertEqual(expired.exception.detail, "SPONTANEOUS_PREVIEW_EXPIRED")

    def test_cross_midnight_schedule_preserves_full_datetimes_and_metadata(self):
        schedule, places = schedule_from_snapshot(snapshot(), uuid4())

        self.assertEqual(schedule.schedule_type, "SPONTANEOUS")
        self.assertEqual(schedule.start_date.isoformat(), "2026-09-11")
        self.assertEqual(schedule.end_date.isoformat(), "2026-09-12")
        self.assertEqual(schedule.days[0].stops[0].arrive_at_datetime.day, 12)
        self.assertEqual(schedule.days[0].final_transit.arrive_at_datetime.hour, 1)
        self.assertIsNone(schedule.days[0].stops[0].fixed_starts_at)
        self.assertEqual(schedule.spontaneous_metadata["destinationId"], "BUSAN_GWANGALLI")
        self.assertEqual(places[0]["externalContentId"], "123")

    def test_save_requires_auth_key_and_database_before_persistence(self):
        preview_id, token, _ = create_preview_token(snapshot(), owner_id=7)
        with self.assertRaises(HTTPException) as unauthenticated:
            save_spontaneous_preview(preview_id, token, None, "key")
        self.assertEqual(unauthenticated.exception.status_code, 401)

        with self.assertRaises(HTTPException) as missing_key:
            save_spontaneous_preview(preview_id, token, 7, None)
        self.assertEqual(missing_key.exception.detail, "IDEMPOTENCY_KEY_REQUIRED")

        with mock.patch("spontaneous.schedule_service.db_enabled", return_value=False):
            with self.assertRaises(HTTPException) as no_database:
                save_spontaneous_preview(preview_id, token, 7, "key")
        self.assertEqual(no_database.exception.status_code, 503)
