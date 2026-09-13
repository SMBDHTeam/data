import json
from io import BytesIO
from unittest import TestCase, main
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from spontaneous.places import enrich_course_place_images, search_place_image


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


class SpontaneousPlaceImageTest(TestCase):
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

    def test_original_detail_image_is_preferred_over_thumbnail(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", return_value=image_response([
                {"smallimageurl": "https://tourapi.example/first-thumb.jpg"},
                {
                    "originimgurl": "https://tourapi.example/original.jpg",
                    "smallimageurl": "https://tourapi.example/second-thumb.jpg",
                },
            ])) as tour_api_http,
        ):
            result = search_place_image("123", image_cache={})

        self.assertEqual(result, "https://tourapi.example/original.jpg")
        params = parse_qs(urlparse(tour_api_http.call_args.args[0]).query)
        self.assertEqual(params["contentId"], ["123"])
        self.assertEqual(params["imageYN"], ["Y"])
        self.assertNotIn("subImageYN", params)
        self.assertEqual(tour_api_http.call_args.kwargs["timeout"], 10)

    def test_thumbnail_is_used_when_original_is_absent(self):
        with (
            patch.dict("os.environ", {"TOUR_API_KEY": "test-key"}),
            patch("spontaneous.places.urlopen", return_value=image_response([
                {"originimgurl": "", "smallimageurl": "https://tourapi.example/thumb.jpg"}
            ])),
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


if __name__ == "__main__":
    main()
