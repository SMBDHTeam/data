import json
from io import BytesIO
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from spontaneous.image_urls import normalize_tourapi_image_url
from spontaneous.places import (
    MAX_DETAIL_IMAGE_LOOKUPS_PER_PLACE,
    enrich_course_place_images,
    find_related_tourapi_places,
    search_place_image,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tourapi_destination_places.json"
FIXTURE_ZONES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["zones"]
FIXTURE_PLACES = [place for places in FIXTURE_ZONES.values() for place in places]
HWANGNYEONG_LOOKOUT = next(
    place for place in FIXTURE_PLACES if place["contentid"] == "2733472"
)
HWANGNYEONG_MOUNTAIN = next(
    place for place in FIXTURE_PLACES if place["contentid"] == "128290"
)
HWANGNYEONG_IMAGE = (
    "https://tong.visitkorea.or.kr/cms/resource/51/2732751_image2_1.jpg"
)


def image_response(items):
    return BytesIO(json.dumps({
        "response": {
            "header": {"resultCode": "0000"},
            "body": {"items": {"item": items}},
        }
    }).encode())


def selected_place(content_id="123", **raw_images):
    return {
        "contentId": content_id,
        "raw": raw_images,
    }


def tourapi_place(
    content_id,
    title,
    *,
    content_type_id="12",
    category="A01010400",
    longitude="129.0827285679",
    latitude="35.1579341561",
    **images,
):
    return {
        "contentid": str(content_id),
        "contenttypeid": content_type_id,
        "cat3": category,
        "title": title,
        "mapx": longitude,
        "mapy": latitude,
        **images,
    }


def course_place(record):
    return {
        "contentId": record["contentid"],
        "contentTypeId": record["contenttypeid"],
        "name": record["title"],
        "longitude": float(record["mapx"]),
        "latitude": float(record["mapy"]),
        "raw": dict(record),
    }


class SpontaneousPlaceImageTest(TestCase):
    def test_list_image_fields_are_upgraded_to_https(self):
        for field in ("firstimage", "firstimage2"):
            with self.subTest(field=field):
                place = selected_place(
                    **{
                        field: (
                            "  http://tong.visitkorea.or.kr/cms/resource/44/"
                            "2868344_image2_1.jpg?width=800&quality=90  "
                        )
                    }
                )

                with patch("spontaneous.places.search_place_image") as lookup:
                    enrich_course_place_images([place], image_cache={})

                lookup.assert_not_called()
                self.assertEqual(
                    place["raw"][field],
                    "https://tong.visitkorea.or.kr/cms/resource/44/"
                    "2868344_image2_1.jpg?width=800&quality=90",
                )

    def test_firstimage_skips_detail_lookup(self):
        place = selected_place(firstimage="https://tourapi.example/first.jpg")

        with patch("spontaneous.places.search_place_image") as lookup:
            enrich_course_place_images([place], image_cache={})

        lookup.assert_not_called()
        self.assertNotIn("_detailImageUrl", place)

    def test_firstimage2_skips_detail_lookup(self):
        place = selected_place(firstimage2="https://tourapi.example/second.jpg")

        with patch("spontaneous.places.search_place_image") as lookup:
            enrich_course_place_images([place], image_cache={})

        lookup.assert_not_called()
        self.assertNotIn("_detailImageUrl", place)

    def test_exact_detail_image_skips_related_candidates(self):
        selected = course_place(HWANGNYEONG_LOOKOUT)
        related = {
            **HWANGNYEONG_MOUNTAIN,
            "firstimage": "https://tourapi.example/related.jpg",
        }

        with patch(
            "spontaneous.places.search_place_image",
            return_value="https://tourapi.example/exact.jpg",
        ) as lookup:
            enrich_course_place_images(
                [selected],
                image_cache={},
                related_places=[related],
            )

        self.assertEqual(
            selected["_detailImageUrl"],
            "https://tourapi.example/exact.jpg",
        )
        lookup.assert_called_once_with("2733472", image_cache={})

    def test_hwangnyeong_lookout_uses_related_fixture_detail_image(self):
        selected = course_place(HWANGNYEONG_LOOKOUT)
        requested_content_ids = []

        def detail_image_response(url, **kwargs):
            content_id = parse_qs(urlparse(url).query)["contentId"][0]
            requested_content_ids.append(content_id)
            items = (
                [{"originimgurl": HWANGNYEONG_IMAGE}]
                if content_id == "128290"
                else []
            )
            return image_response(items)

        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", side_effect=detail_image_response),
        ):
            enrich_course_place_images(
                [selected],
                image_cache={},
                related_places=FIXTURE_PLACES,
            )

        self.assertEqual(selected["_detailImageUrl"], HWANGNYEONG_IMAGE)
        self.assertEqual(requested_content_ids, ["2733472", "128290"])

    def test_related_list_image_avoids_related_detail_lookup(self):
        for field in ("firstimage", "firstimage2"):
            with self.subTest(field=field):
                selected = course_place(HWANGNYEONG_LOOKOUT)
                related = {
                    **HWANGNYEONG_MOUNTAIN,
                    field: (
                        "http://tong.visitkorea.or.kr/cms/resource/related.jpg"
                    ),
                }

                with patch(
                    "spontaneous.places.search_place_image",
                    return_value=None,
                ) as lookup:
                    enrich_course_place_images(
                        [selected],
                        image_cache={},
                        related_places=[related],
                    )

                self.assertEqual(
                    selected["_detailImageUrl"],
                    "https://tong.visitkorea.or.kr/cms/resource/related.jpg",
                )
                lookup.assert_called_once_with("2733472", image_cache={})

    def test_related_detail_is_queried_only_after_its_list_images_are_absent(self):
        selected = course_place(HWANGNYEONG_LOOKOUT)

        def image_for(content_id, image_cache=None):
            return HWANGNYEONG_IMAGE if str(content_id) == "128290" else None

        with patch(
            "spontaneous.places.search_place_image",
            side_effect=image_for,
        ) as lookup:
            enrich_course_place_images(
                [selected],
                image_cache={},
                related_places=[HWANGNYEONG_MOUNTAIN],
            )

        self.assertEqual(selected["_detailImageUrl"], HWANGNYEONG_IMAGE)
        self.assertEqual(
            [call.args[0] for call in lookup.call_args_list],
            ["2733472", "128290"],
        )

    def test_unrelated_restaurant_or_cafe_at_same_coordinates_is_rejected(self):
        cases = (
            ("바다식당", "산들식당", "A05020100"),
            ("블루카페", "레드카페", "A05020900"),
        )
        for selected_name, candidate_name, category in cases:
            with self.subTest(selected_name=selected_name):
                selected_record = tourapi_place(
                    "100", selected_name, content_type_id="39", category=category
                )
                candidate = tourapi_place(
                    "200",
                    candidate_name,
                    content_type_id="39",
                    category=category,
                    firstimage="https://tourapi.example/wrong.jpg",
                )
                selected = course_place(selected_record)

                with patch(
                    "spontaneous.places.search_place_image", return_value=None
                ) as lookup:
                    enrich_course_place_images(
                        [selected], image_cache={}, related_places=[candidate]
                    )

                self.assertIsNone(selected["_detailImageUrl"])
                lookup.assert_called_once_with("100", image_cache={})

    def test_same_name_outside_thirty_meters_is_rejected(self):
        selected_record = tourapi_place("100", "테스트산 전망대")
        distant = tourapi_place(
            "200",
            "테스트산",
            latitude="35.1589341561",
            firstimage="https://tourapi.example/wrong.jpg",
        )

        self.assertEqual(
            find_related_tourapi_places(course_place(selected_record), [distant]),
            [],
        )

    def test_generic_overlap_and_short_or_empty_core_are_rejected(self):
        cases = (
            ("블루 전망대", "레드 전망대"),
            ("산 전망대", "산"),
            ("전망대", "전망대"),
            ("()", ""),
        )
        for selected_name, candidate_name in cases:
            with self.subTest(selected_name=selected_name, candidate_name=candidate_name):
                selected = course_place(tourapi_place("100", selected_name))
                candidate = tourapi_place("200", candidate_name)
                self.assertEqual(
                    find_related_tourapi_places(selected, [candidate]),
                    [],
                )

    def test_name_matching_uses_nfkc_casefold_spacing_and_punctuation(self):
        selected = course_place(tourapi_place("100", "ＡＢＣ (관광지)"))
        candidate = tourapi_place("200", "abc")

        self.assertEqual(
            [place["contentid"] for place in find_related_tourapi_places(
                selected, [candidate]
            )],
            ["200"],
        )

    def test_parenthesized_branch_names_are_not_discarded(self):
        selected = course_place(tourapi_place("100", "스타카페(광안점)"))
        candidate = tourapi_place("200", "스타카페(서면점)")

        self.assertEqual(find_related_tourapi_places(selected, [candidate]), [])

    def test_different_content_type_or_invalid_identity_is_rejected(self):
        selected = course_place(tourapi_place("100", "테스트산 전망대"))
        cases = (
            tourapi_place("200", "테스트산", content_type_id="14"),
            tourapi_place("invalid", "테스트산"),
            tourapi_place("1" * 256, "테스트산"),
            tourapi_place("200", "테스트산", latitude="invalid"),
        )

        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    find_related_tourapi_places(selected, [candidate]),
                    [],
                )

    def test_food_and_cafe_categories_cannot_cross_match(self):
        selected = course_place(tourapi_place(
            "100", "테스트장소", content_type_id="39", category="A05020100"
        ))
        cafe = tourapi_place(
            "200", "테스트장소", content_type_id="39", category="A05020900"
        )

        self.assertEqual(find_related_tourapi_places(selected, [cafe]), [])

    def test_related_candidate_order_is_deterministic(self):
        selected = course_place(tourapi_place("100", "테스트산 전망대"))
        higher_id = tourapi_place(
            "300", "테스트산", firstimage="https://tourapi.example/300.jpg"
        )
        lower_id = tourapi_place(
            "200", "테스트산", firstimage="https://tourapi.example/200.jpg"
        )

        results = []
        for candidates in ([higher_id, lower_id], [lower_id, higher_id]):
            current = {**selected, "raw": dict(selected["raw"])}
            with patch("spontaneous.places.search_place_image", return_value=None):
                enrich_course_place_images(
                    [current], image_cache={}, related_places=candidates
                )
            results.append(current["_detailImageUrl"])

        self.assertEqual(results, ["https://tourapi.example/200.jpg"] * 2)

    def test_detail_lookup_limit_prevents_candidate_n_plus_one(self):
        selected = course_place(tourapi_place("100", "테스트산 전망대"))
        related = [
            tourapi_place(str(content_id), "테스트산")
            for content_id in (104, 102, 101, 103)
        ]
        unrelated = [
            tourapi_place(str(content_id), f"다른장소{content_id}")
            for content_id in range(200, 220)
        ]

        with patch(
            "spontaneous.places.search_place_image", return_value=None
        ) as lookup:
            enrich_course_place_images(
                [selected],
                image_cache={},
                related_places=unrelated + related,
            )

        self.assertEqual(lookup.call_count, MAX_DETAIL_IMAGE_LOOKUPS_PER_PLACE)
        self.assertEqual(
            [call.args[0] for call in lookup.call_args_list],
            ["100", "101", "102"],
        )

    def test_original_detail_image_is_preferred_over_thumbnail(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", return_value=image_response([
                {"smallimageurl": "https://tourapi.example/first-thumb.jpg"},
                {
                    "originimgurl": (
                        "http://tong.visitkorea.or.kr/cms/resource/original.jpg"
                    ),
                    "smallimageurl": "https://tourapi.example/second-thumb.jpg",
                },
            ])) as tour_api_http,
        ):
            result = search_place_image("123", image_cache={})

        self.assertEqual(
            result,
            "https://tong.visitkorea.or.kr/cms/resource/original.jpg",
        )
        params = parse_qs(urlparse(tour_api_http.call_args.args[0]).query)
        self.assertEqual(params["contentId"], ["123"])
        self.assertEqual(params["imageYN"], ["Y"])
        self.assertNotIn("subImageYN", params)
        self.assertEqual(tour_api_http.call_args.kwargs["timeout"], 10)

    def test_thumbnail_is_used_when_original_is_absent(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", return_value=image_response([
                {
                    "originimgurl": "",
                    "smallimageurl": (
                        "http://tong.visitkorea.or.kr/cms/resource/thumb.jpg"
                    ),
                }
            ])),
        ):
            result = search_place_image("123", image_cache={})

        self.assertEqual(
            result,
            "https://tong.visitkorea.or.kr/cms/resource/thumb.jpg",
        )

    def test_https_url_is_unchanged(self):
        value = "https://tong.visitkorea.or.kr/cms/resource/image.jpg?size=large"

        self.assertEqual(normalize_tourapi_image_url(value), value)

    def test_path_and_query_string_are_preserved_when_upgrading(self):
        value = (
            "http://tong.visitkorea.or.kr/cms/resource/a%20b/image.jpg"
            "?name=a%2Fb&token=x%3Dy#preview"
        )

        self.assertEqual(
            normalize_tourapi_image_url(value),
            (
                "https://tong.visitkorea.or.kr/cms/resource/a%20b/image.jpg"
                "?name=a%2Fb&token=x%3Dy#preview"
            ),
        )

    def test_empty_and_invalid_urls_are_none(self):
        for value in (
            None,
            "",
            "   ",
            "not-a-url",
            "ftp://tong.visitkorea.or.kr/image.jpg",
            "http://example.test/image.jpg",
            "https:///missing-host.jpg",
            "https://user:password@example.test/image.jpg",
            "https://example.test:invalid/image.jpg",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_tourapi_image_url(value))

    def test_invalid_original_falls_back_to_valid_thumbnail(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", return_value=image_response([{
                "originimgurl": "javascript:alert(1)",
                "smallimageurl": "https://tourapi.example/thumb.jpg",
            }])),
        ):
            result = search_place_image("123", image_cache={})

        self.assertEqual(result, "https://tourapi.example/thumb.jpg")

    def test_empty_image_result_is_none_and_cached(self):
        cache = {}
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch(
                "spontaneous.places.urlopen",
                return_value=image_response([]),
            ) as tour_api_http,
        ):
            first = search_place_image("123", image_cache=cache)
            second = search_place_image("123", image_cache=cache)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(cache, {"123": None})
        self.assertEqual(tour_api_http.call_count, 1)

    def test_duplicate_selected_content_id_is_looked_up_once(self):
        places = [selected_place("123"), selected_place("123")]
        cache = {}
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch(
                "spontaneous.places.urlopen",
                return_value=image_response([{
                    "originimgurl": "https://tourapi.example/original.jpg"
                }]),
            ) as tour_api_http,
        ):
            enrich_course_place_images(places, image_cache=cache)

        self.assertEqual(tour_api_http.call_count, 1)
        self.assertEqual(
            [place["_detailImageUrl"] for place in places],
            ["https://tourapi.example/original.jpg"] * 2,
        )

    def test_invalid_response_is_none(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch(
                "spontaneous.places.urlopen",
                return_value=BytesIO(b"not-json"),
            ),
        ):
            result = search_place_image("123", image_cache={})

        self.assertIsNone(result)

    def test_image_api_error_is_cached_and_does_not_stop_enrichment(self):
        places = [selected_place("123"), selected_place("123")]
        cache = {}
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch(
                "spontaneous.places.urlopen",
                side_effect=TimeoutError("timed out"),
            ) as tour_api_http,
        ):
            enrich_course_place_images(places, image_cache=cache)

        self.assertEqual(tour_api_http.call_count, 1)
        self.assertEqual(cache, {"123": None})
        self.assertEqual(
            [place["_detailImageUrl"] for place in places],
            [None, None],
        )


if __name__ == "__main__":
    main()
