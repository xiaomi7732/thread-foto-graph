import unittest
from unittest.mock import Mock, patch

from src import vision


class VisionPromptTests(unittest.TestCase):
    @patch.object(vision.requests, "post")
    @patch.object(vision.requests, "get")
    def test_request_includes_photograph_read_and_preserves_response(self, get, post):
        get.return_value = Mock(
            headers={"Content-Type": "image/jpeg"}, content=b"photo"
        )
        text = (
            "Description: A mountain at sunrise.\n"
            "Photograph read: Warm side light emphasizes the layered ridges "
            "and creates a quiet mood.\n"
            "Photo type: Artistic landscape."
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
        self.assertIn("snapshot, artistic, documentary, or storytelling", prompt)
        self.assertIn("composition, light, and mood", prompt)
        self.assertEqual(description, text)
        self.assertEqual(attempts, 1)
        post.assert_called_once()

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
