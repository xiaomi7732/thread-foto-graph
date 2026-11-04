import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import cache as media_cache, vision


class VisionPromptTests(unittest.TestCase):
    @patch.object(vision.requests, "post")
    @patch.object(vision.requests, "get")
    def test_request_includes_watch_outs_and_preserves_response(self, get, post):
        get.return_value = Mock(
            headers={"Content-Type": "image/jpeg"}, content=b"photo"
        )
        text = (
            "Description: A mountain at sunrise.\n"
            "Photograph read: Warm side light emphasizes the layered ridges "
            "and creates a quiet mood.\n"
            "Photo type: Artistic landscape.\n"
            "Watch-outs: Bright sky clipped to white."
        )
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": text}]}}]
        }

        description, attempts = vision.describe_image("https://example.com/photo.jpg")

        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Description:", prompt)
        self.assertIn("Photograph read:", prompt)
        self.assertIn("Photo type:", prompt)
        self.assertIn("Watch-outs:", prompt)
        self.assertIn("Empty is a valid and common value", prompt)
        self.assertIn("one or two short clauses of observable facts only", prompt)
        self.assertIn("no scores, grades, overall verdicts, praise, blame", prompt)
        self.assertIn("intent cannot be determined from the image", prompt)
        self.assertIn("snapshot, artistic, documentary, or storytelling", prompt)
        self.assertIn("composition, light, and mood", prompt)
        self.assertEqual(description, text)
        self.assertEqual(attempts, 1)
        post.assert_called_once()

    def test_split_watch_outs_preserves_legacy_empty_and_pending_descriptions(self):
        cases = [
            (None, None, ""),
            ("A mountain.", "A mountain.", ""),
            ("Description: A mountain.\nWatch-outs:", "Description: A mountain.", ""),
            (
                "Description: A mountain.\r\n\r\nWatch-outs: Sky clipped;\r\n"
                "flare streaks.\r\n\r\nPhoto type: Landscape.",
                "Description: A mountain.\r\n\r\nPhoto type: Landscape.",
                "Sky clipped; flare streaks.",
            ),
        ]
        for raw, description, notes in cases:
            with self.subTest(raw=raw):
                self.assertEqual(vision.split_watch_outs(raw), (description, notes))

    @patch.object(vision.time, "sleep")
    @patch.object(vision, "describe_image")
    def test_watch_outs_survive_cache_round_trip_and_keep_carousel_alignment(self, describe_image, sleep):
        raw = "Description: Sun over water.\nWatch-outs: Sun disc clipped; flare streaks."
        describe_image.return_value = (raw, 1)
        legacy = "Description: A mountain."
        item = {
            "id": "album",
            "media_type": "CAROUSEL_ALBUM",
            "children": {"data": [
                {"id": media_id, "media_type": "IMAGE", "media_url": "https://example.com/photo.jpg"}
                for media_id in ("legacy", "sun", "empty", "pending")
            ]},
        }
        cache = {"legacy": legacy, "empty": "Description: A lake.\nWatch-outs:"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(media_cache, "CACHE_PATH", Path(directory) / "cache.json"):
                result = vision.describe_images(
                    [item], cache, 1, on_progress=lambda: media_cache.save_cache(cache)
                )
                restored = media_cache.load_cache()
                cached_item = {key: value for key, value in item.items() if key != "watch_outs"}
                cached_result = vision.describe_images([cached_item], restored, 0)

        self.assertEqual(result[0], 1)
        self.assertEqual(cached_result[0], 0)
        self.assertEqual(restored["sun"], raw)
        self.assertEqual(item["photo_descriptions"], [legacy, "Description: Sun over water.", "Description: A lake.", None])
        self.assertEqual(item["watch_outs"], ["", "Sun disc clipped; flare streaks.", "", ""])
        self.assertEqual(cached_item, item)
        describe_image.assert_called_once()

    @patch.object(vision, "describe_image")
    def test_legacy_and_pending_photos_do_not_gain_watch_outs(self, describe_image):
        for cache in ({}, {"photo": "A mountain."}):
            with self.subTest(cache=cache):
                item = {
                    "id": "photo", "media_type": "IMAGE",
                    "media_url": "https://example.com/photo.jpg",
                    "watch_outs": ["Stale note."],
                }
                vision.describe_images([item], cache, 0)
                self.assertNotIn("watch_outs", item)
        describe_image.assert_not_called()

    @patch.object(vision, "describe_image")
    def test_previously_attempted_photo_is_left_pending(self, describe_image):
        item = {"id": "photo-1", "media_type": "IMAGE", "media_url": "https://example.com/photo.jpg"}

        result = vision.describe_images(
            [item],
            {},
            10,
            attempted_media_ids={"photo-1": None},
        )

        self.assertEqual(result[0], 0)
        self.assertEqual(item["photo_descriptions"], [None])
        describe_image.assert_not_called()

    def test_failed_description_is_preserved_without_retrying(self):
        item = {"id": "photo-1", "media_type": "IMAGE", "media_url": "https://example.com/photo.jpg"}
        attempted_media_ids = {}
        with patch.object(
            vision,
            "describe_image",
            side_effect=vision.requests.RequestException("request failed"),
        ) as describe_image, patch.object(vision.time, "sleep"):
            vision.describe_images([item], {}, 10, attempted_media_ids=attempted_media_ids)
            first_description = item["photo_descriptions"][0]

            vision.describe_images([item], {}, 10, attempted_media_ids=attempted_media_ids)

        self.assertIn(vision.DESCRIPTION_UNAVAILABLE_PREFIX, first_description)
        self.assertEqual(item["photo_descriptions"], [first_description])
        describe_image.assert_called_once()


if __name__ == "__main__":
    unittest.main()
