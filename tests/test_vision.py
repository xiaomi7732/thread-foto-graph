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
            "and creates a quiet mood."
        )
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": text}]}}]
        }

        description, attempts = vision.describe_image("https://example.com/photo.jpg")

        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Description:", prompt)
        self.assertIn("Photograph read:", prompt)
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
            attempted_media_ids={"photo-1"},
        )

        self.assertEqual(result[0], 0)
        self.assertEqual(item["photo_descriptions"], [None])
        describe_image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
