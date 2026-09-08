from random import Random
from unittest import TestCase, main
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import HTTPException

import app as data_app
from spontaneous.models import Coordinate, TransportMode
from spontaneous.routing import RoutingApiError, search_tmap_transit_route
from tests.test_spontaneous_destination_routing_limit import START, START_AT, request
from tests.test_spontaneous_destination_selection import TOURAPI_PLACES_BY_ZONE


def http_error(status):
    return HTTPError("https://apis.openapi.sk.com/transit/routes", status, "provider error", None, None)


class TmapTransitErrorTest(TestCase):
    def test_http_429_is_distinguished_from_other_provider_errors_without_retry(self):
        for status in (429, 400, 401, 403, 500, 502, 503):
            with (
                self.subTest(status=status),
                patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
                patch("spontaneous.routing.urlopen", side_effect=http_error(status)) as http,
            ):
                with self.assertRaises(RoutingApiError) as error:
                    search_tmap_transit_route(
                        START, Coordinate(latitude=35.15, longitude=129.12), START_AT
                    )
                self.assertEqual(error.exception.provider, "TMAP_TRANSIT")
                self.assertEqual(error.exception.status_code, 503 if status == 429 else 502)
                self.assertEqual(error.exception.detail,
                                 "TMAP_QUOTA_EXCEEDED" if status == 429 else "EXTERNAL_ROUTING_API_ERROR")
                self.assertEqual(http.call_count, 1)

    def test_destination_endpoint_preserves_quota_detail_and_two_candidate_limit(self):
        for status in (429, 500):
            with (
                self.subTest(status=status),
                patch.dict("os.environ", {"SKT_API_KEY": "test-key"}),
                patch("app.search_places_by_zone", side_effect=lambda zone, places_cache=None:
                      TOURAPI_PLACES_BY_ZONE[zone.destination_id]),
                patch("spontaneous.service.random", Random(42)),
                patch("spontaneous.routing.urlopen", side_effect=http_error(status)) as http,
            ):
                with self.assertRaises(HTTPException) as error:
                    data_app.recommend_spontaneous_destinations(request(TransportMode.PUBLIC_TRANSIT))
                self.assertEqual(error.exception.status_code, 503 if status == 429 else 502)
                self.assertEqual(error.exception.detail,
                                 "TMAP_QUOTA_EXCEEDED" if status == 429 else "EXTERNAL_ROUTING_API_ERROR")
                self.assertEqual(http.call_count, 2)


if __name__ == "__main__":
    main()
