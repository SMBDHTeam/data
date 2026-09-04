from datetime import datetime, timedelta, timezone
from unittest import TestCase, main
from unittest.mock import patch

from fastapi import HTTPException

import app as data_app
import spontaneous.routing as routing
from spontaneous.destinations import DestinationZone
from spontaneous.models import (
    Coordinate,
    SpontaneousDestinationRequest,
    TransportMode,
    TransportOption,
)
from spontaneous.routing import RouteResult
from spontaneous.service import MAX_DESTINATION_RECOMMENDATIONS


KST = timezone(timedelta(hours=9))
START = Coordinate(latitude=35.115203, longitude=129.041550)
START_AT = datetime(2026, 9, 5, 9, 0, tzinfo=KST)
RETURN_BY = datetime(2026, 9, 5, 23, 0, tzinfo=KST)


def zone_number(zone: DestinationZone) -> int:
    return int(zone.destination_id.rsplit("_", 1)[1])


def coordinate_key(coordinate: Coordinate) -> tuple[float, float]:
    return (
        round(coordinate.latitude, 6),
        round(coordinate.longitude, 6),
    )


def make_zones(count: int = 8) -> list[DestinationZone]:
    return [
        DestinationZone(
            destination_id=f"ZONE_{index}",
            name=f"Zone {index}",
            anchor_name=f"Anchor {index}",
            center_latitude=35.0 + index * 0.001,
            center_longitude=129.0 + index * 0.001,
            radius_meters=1000,
        )
        for index in range(1, count + 1)
    ]


def request(mode: TransportMode) -> SpontaneousDestinationRequest:
    return SpontaneousDestinationRequest(
        startLocation=START,
        startAt=START_AT,
        returnBy=RETURN_BY,
        transportMode=mode,
        desiredThemes=[],
    )


def successful_transport(mode: TransportMode) -> TransportOption:
    return TransportOption(
        mode=mode,
        available=True,
        outboundMinutes=10,
        returnMinutes=10,
        availableStayMinutes=820,
    )


class SpontaneousDestinationRoutingLimitTest(TestCase):
    def setUp(self):
        self.zones = make_zones()
        self.zone_by_coordinate = {
            (
                round(zone.center_latitude, 6),
                round(zone.center_longitude, 6),
            ): zone.destination_id
            for zone in self.zones
        }

    def patch_preranking(self):
        return patch.multiple(
            data_app,
            DESTINATION_ZONES=self.zones,
            search_places_by_zone=lambda zone, places_cache=None: [
                {"contentid": zone.destination_id}
            ],
            filter_course_candidates=lambda places: places,
            calculate_zone_theme_score=lambda places, desired_themes: 1.0,
            has_coarse_course_viability=lambda places, desired_themes: True,
            calculate_destination_score=lambda zone, start_location, theme_score: (
                100.0 - zone_number(zone),
                theme_score,
                zone_number(zone) * 100,
            ),
        )

    def get_transport_call_counter(self, outcomes):
        calls = []

        def fake_get_transport_option(
            origin,
            destination,
            mode,
            start_at,
            return_by,
            cache=None,
        ):
            zone_id = self.zone_by_coordinate[coordinate_key(destination)]
            calls.append(zone_id)
            outcome = outcomes.get(zone_id, "success")
            if outcome == "success":
                return successful_transport(mode)
            return TransportOption(
                mode=mode,
                available=False,
                unavailableReason=outcome,
            )

        return calls, fake_get_transport_option

    def test_public_transit_routes_only_top_two_candidates_and_returns_two(self):
        get_transport_calls = []
        search_route_calls = []
        tmap_transit_calls = []
        original_get_transport_option = data_app.get_transport_option
        original_search_route = routing.search_route

        def wrapped_get_transport_option(*args, **kwargs):
            get_transport_calls.append(args[1])
            return original_get_transport_option(*args, **kwargs)

        def wrapped_search_route(*args, **kwargs):
            search_route_calls.append(args)
            return original_search_route(*args, **kwargs)

        def fake_tmap_transit_route(origin, destination, departure_at, cache=None):
            tmap_transit_calls.append((origin, destination, departure_at))
            return RouteResult(
                travelMinutes=10,
                requestedDepartureAt=departure_at,
                departureAt=departure_at,
                arrivalAt=departure_at + timedelta(minutes=10),
                mode=TransportMode.PUBLIC_TRANSIT,
                provider="TMAP_TRANSIT",
            )

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=wrapped_get_transport_option):
                with patch("spontaneous.routing.search_route", side_effect=wrapped_search_route):
                    with patch(
                        "spontaneous.routing.search_tmap_transit_route",
                        side_effect=fake_tmap_transit_route,
                    ):
                        response = data_app.recommend_spontaneous_destinations(
                            request(TransportMode.PUBLIC_TRANSIT)
                        )

        self.assertEqual(
            [item.destinationId for item in response.destinations],
            ["ZONE_1", "ZONE_2"],
        )
        self.assertEqual(len(response.destinations), 2)
        self.assertEqual(len(get_transport_calls), 2)
        self.assertEqual(len(search_route_calls), 4)
        self.assertEqual(len(tmap_transit_calls), 4)

    def test_public_transit_one_success_one_no_route_does_not_try_third(self):
        calls, fake_get_transport_option = self.get_transport_call_counter(
            {"ZONE_1": "success", "ZONE_2": "NO_ROUTE"}
        )

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                response = data_app.recommend_spontaneous_destinations(
                    request(TransportMode.PUBLIC_TRANSIT)
                )

        self.assertEqual(calls, ["ZONE_1", "ZONE_2"])
        self.assertEqual(
            [item.destinationId for item in response.destinations],
            ["ZONE_1"],
        )

    def test_public_transit_two_no_routes_returns_not_found_without_third(self):
        calls, fake_get_transport_option = self.get_transport_call_counter(
            {"ZONE_1": "NO_ROUTE", "ZONE_2": "NO_ROUTE"}
        )

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                with self.assertRaises(HTTPException) as error:
                    data_app.recommend_spontaneous_destinations(
                        request(TransportMode.PUBLIC_TRANSIT)
                    )

        self.assertEqual(calls, ["ZONE_1", "ZONE_2"])
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(error.exception.detail, "DESTINATIONS_NOT_FOUND")

    def test_public_transit_provider_error_mapping_is_preserved(self):
        calls, fake_get_transport_option = self.get_transport_call_counter(
            {
                "ZONE_1": "EXTERNAL_ROUTING_API_ERROR",
                "ZONE_2": "NO_ROUTE",
            }
        )

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                with self.assertRaises(HTTPException) as error:
                    data_app.recommend_spontaneous_destinations(
                        request(TransportMode.PUBLIC_TRANSIT)
                    )

        self.assertEqual(calls, ["ZONE_1", "ZONE_2"])
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(error.exception.detail, "EXTERNAL_ROUTING_API_ERROR")

    def test_car_still_returns_existing_recommendation_limit(self):
        calls, fake_get_transport_option = self.get_transport_call_counter({})

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                response = data_app.recommend_spontaneous_destinations(
                    request(TransportMode.CAR)
                )

        self.assertEqual(len(response.destinations), MAX_DESTINATION_RECOMMENDATIONS)
        self.assertEqual(calls, ["ZONE_1", "ZONE_2", "ZONE_3", "ZONE_4", "ZONE_5"])

    def test_walk_still_returns_existing_recommendation_limit(self):
        calls, fake_get_transport_option = self.get_transport_call_counter({})

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                response = data_app.recommend_spontaneous_destinations(
                    request(TransportMode.WALK)
                )

        self.assertEqual(len(response.destinations), MAX_DESTINATION_RECOMMENDATIONS)
        self.assertEqual(calls, ["ZONE_1", "ZONE_2", "ZONE_3", "ZONE_4", "ZONE_5"])

    def test_public_response_shape_does_not_expose_internal_score(self):
        calls, fake_get_transport_option = self.get_transport_call_counter({})

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                response = data_app.recommend_spontaneous_destinations(
                    request(TransportMode.PUBLIC_TRANSIT)
                )

        self.assertEqual(calls, ["ZONE_1", "ZONE_2"])
        payload = response.model_dump()
        self.assertEqual(set(payload), {"destinations"})
        self.assertEqual(
            set(payload["destinations"][0]),
            {
                "destinationId",
                "name",
                "themeScore",
                "distanceMeters",
                "transport",
            },
        )
        self.assertNotIn("score", payload["destinations"][0])

    def test_pre_ranking_order_is_deterministic_before_routing_limit(self):
        calls, fake_get_transport_option = self.get_transport_call_counter(
            {"ZONE_1": "NO_ROUTE", "ZONE_2": "NO_ROUTE"}
        )

        with self.patch_preranking():
            with patch("app.get_transport_option", side_effect=fake_get_transport_option):
                with self.assertRaises(HTTPException):
                    data_app.recommend_spontaneous_destinations(
                        request(TransportMode.PUBLIC_TRANSIT)
                    )

        self.assertEqual(calls, ["ZONE_1", "ZONE_2"])


if __name__ == "__main__":
    main()
