import json
from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO
from unittest import TestCase, main
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import HTTPException

import app as data_app
from spontaneous.models import SpontaneousCourseRequest, SpontaneousCourseResponse, TransportMode
from spontaneous.places import infer_place_themes, is_open_now
from spontaneous.routing import route_result_from_minutes, search_route
from tests.test_spontaneous_destination_routing_limit import START, START_AT
from tests.test_spontaneous_destination_selection import TOURAPI_PLACES_BY_ZONE


PLACES = TOURAPI_PLACES_BY_ZONE["BUSAN_GWANGALLI"]
CAFE = next(place for place in PLACES if "CAFE" in infer_place_themes(place))
ACTIVITY = next(place for place in PLACES if "SEA" in infer_place_themes(place))
LODGING = next(place for place in PLACES if place["contenttypeid"] == "32")


def course_request(themes=("CAFE",), mode=TransportMode.CAR):
    return SpontaneousCourseRequest(
        destinationId="BUSAN_GWANGALLI",
        startLocation=START,
        startAt=START_AT.replace(hour=10),
        returnBy=START_AT.replace(hour=18),
        desiredThemes=list(themes),
        transportMode=mode,
    )


def route_provider(mode, origin, destination, departure_at, cache=None):
    return route_result_from_minutes(
        mode=mode,
        provider="test",
        travel_minutes=180 if origin == START else 10,
        departure_at=departure_at,
    )


@contextmanager
def provider_boundaries(places=None, opening_hours="11:00~18:00", provider=route_provider):
    # Only external boundaries are replaced. Real TourAPI place records, course
    # generation, sequential timing, opening-hours parsing and cache run normally.
    detail = {"response": {"body": {"items": {"item": [{"opentimefood": opening_hours}]}}}}
    with (
        patch("spontaneous.time_window.current_kst_time", return_value=START_AT),
        patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
        patch("app.search_places_by_zone", return_value=[CAFE] if places is None else places),
        patch("spontaneous.places.urlopen", side_effect=lambda *args, **kwargs:
              BytesIO(json.dumps(detail).encode())) as detail_http,
        patch("spontaneous.course.search_route", side_effect=provider) as routes,
    ):
        yield detail_http, routes


class SpontaneousCourseEndpointTest(TestCase):
    def assert_rejected(self, payload, reason, detail="COURSE_NOT_FEASIBLE"):
        with self.assertLogs("data.app", level="INFO") as logs:
            with self.assertRaises(HTTPException) as error:
                data_app.create_spontaneous_course(payload)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(error.exception.detail, detail)
        message = "\n".join(logs.output)
        self.assertIn("failureReason=" + reason, message)
        for field in (
            "destinationId=BUSAN_GWANGALLI", "transportMode=", "desiredThemes=",
            "startAt=", "returnBy=", "placesBeforeFilter=", "placesAfterFilter=",
            "requiredRoles=", "availableRoles=", "courseStopCount=",
        ):
            self.assertIn(field, message)
        for private_value in ("test-key", str(START.latitude), str(START.longitude), CAFE["title"]):
            self.assertNotIn(private_value, message)

    def test_closed_at_start_but_open_at_arrival_is_kept_and_detail_is_cached(self):
        payload = course_request()
        self.assertFalse(is_open_now("11:00~18:00", payload.startAt))
        with provider_boundaries() as (detail_http, routes):
            response = data_app.create_spontaneous_course(payload)

        course = SpontaneousCourseResponse.model_validate(response)
        self.assertEqual(len(course.course), 1)
        self.assertEqual(course.course[0].contentId, CAFE["contentid"])
        self.assertEqual(course.course[0].arrivalAt.hour, 13)
        self.assertEqual(course.course[0].departureAt.hour, 14)
        self.assertEqual(detail_http.call_count, 1)
        self.assertEqual(routes.call_count, 2)

    def test_closed_at_arrival_or_departure_still_rejects_required_place(self):
        for hours in ("14:00~18:00", "11:00~13:30"):
            with self.subTest(hours=hours), provider_boundaries(opening_hours=hours):
                self.assert_rejected(course_request(), "PLACE_CLOSED_AT_VISIT_TIME")

    def test_existing_all_day_course_success_and_response_shape_are_preserved(self):
        with provider_boundaries(opening_hours="00:00~23:59"):
            with self.assertLogs("data.app", level="INFO") as logs:
                response = data_app.create_spontaneous_course(course_request())
        self.assertIn("spontaneous course created.", "\n".join(logs.output))
        self.assertEqual(set(response), {
            "destinationId", "name", "transportMode", "returnTravelMinutes",
            "estimatedReturnAt", "returnBy", "course",
        })
        self.assertNotIn("failureReason", response)
        self.assertEqual(set(response["course"][0]), set(SpontaneousCourseResponse.model_validate(response).course[0].model_dump()))

    def test_closed_optional_cafe_is_removed_and_timeline_recalculated(self):
        with provider_boundaries(places=[ACTIVITY, CAFE], opening_hours="00:00~01:00"):
            response = data_app.create_spontaneous_course(course_request(themes=("SEA",)))
        self.assertEqual([stop["contentId"] for stop in response["course"]], [ACTIVITY["contentid"]])

    def test_no_candidates_logs_filter_counts(self):
        with provider_boundaries(places=[LODGING]):
            self.assert_rejected(course_request(), "NO_PLACES_AFTER_CANDIDATE_FILTER")

    def test_missing_required_role_is_distinguished(self):
        with provider_boundaries(places=[ACTIVITY]):
            self.assert_rejected(course_request(), "MISSING_REQUIRED_ROLE")

    def test_missing_required_theme_is_distinguished(self):
        with provider_boundaries(places=[ACTIVITY]):
            self.assert_rejected(course_request(themes=("SEA", "CULTURE")), "MISSING_REQUIRED_THEME")

    def test_outbound_and_return_no_route_are_logged(self):
        for missing_return in (False, True):
            def provider(mode, origin, destination, departure_at, cache=None):
                if not missing_return or destination == START:
                    return None
                return route_provider(mode, origin, destination, departure_at, cache)

            with self.subTest(missing_return=missing_return), provider_boundaries(provider=provider):
                self.assert_rejected(course_request(), "NO_ROUTE")

    def test_required_course_exceeding_return_time_is_logged(self):
        payload = course_request()
        payload.returnBy = payload.startAt + timedelta(hours=4)
        with provider_boundaries():
            self.assert_rejected(payload, "RETURN_TIME_EXCEEDED")

    def test_empty_initial_course_is_repaired_when_candidates_exist(self):
        with provider_boundaries(), patch("app.generate_course", return_value=[]):
            response = data_app.create_spontaneous_course(course_request(themes=()))
        self.assertEqual([stop["contentId"] for stop in response["course"]], [CAFE["contentid"]])

    def test_tmap_429_reaches_course_http_503_without_retry(self):
        payload = course_request(mode=TransportMode.PUBLIC_TRANSIT)
        with (
            provider_boundaries(),
            patch("spontaneous.course.search_route", wraps=search_route),
            patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
            patch("spontaneous.routing.urlopen", side_effect=HTTPError(
                "https://apis.openapi.sk.com/transit/routes", 429, "quota", None, None
            )) as tmap_http,
        ):
            with self.assertRaises(HTTPException) as error:
                data_app.create_spontaneous_course(payload)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail, "TMAP_QUOTA_EXCEEDED")
        self.assertEqual(tmap_http.call_count, 1)


if __name__ == "__main__":
    main()
