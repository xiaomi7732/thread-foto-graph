import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import requests

from src import cache as media_cache, vision_review


class VisionReviewTests(unittest.TestCase):
    def setUp(self):
        self.cache = {
            "qwen": media_cache.make_entry("Description: A mountain.", "ollama", "qwen3-vl:4b"),
            "gemini": media_cache.make_entry("A lake.", "gemini", "test-model", "ollama_timeout"),
            "legacy": "A forest.",
            "blocked": media_cache.make_entry(
                "(description unavailable: blocked by Ollama)", "ollama", "qwen3-vl:4b"
            ),
        }

    def test_report_attributes_results_without_guessing_legacy_provenance(self):
        html = vision_review.render_report(self.cache, {"pending": {}})
        for expected in (
            "ollama", "qwen3-vl:4b", "gemini", "test-model", "ollama_timeout",
            "legacy/unknown", "moderated", "pending (no successful cached result)",
        ):
            self.assertIn(expected, html)
        self.assertNotIn("<img", html)

    def test_report_escapes_text_and_does_not_persist_signed_urls(self):
        cache = {
            "unsafe": media_cache.make_entry(
                '<script>alert(1)</script> https://example.com/image?signature=secret',
                "ollama", '<img src=x onerror="alert(1)">',
            ),
        }
        refs = {
            "unsafe": {
                "permalink": "javascript:alert(1)",
                "media_url": "https://example.com/photo?signature=private",
            },
        }
        html = vision_review.render_report(cache, refs, previews=True)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("signature=", html)
        self.assertNotIn("https://example.com/photo", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('src="/image/unsafe"', html)

    def test_references_preserve_carousel_ids_and_strip_permalink_queries(self):
        refs = vision_review.collect_references([{
            "id": "root", "media_type": "CAROUSEL_ALBUM",
            "permalink": "https://www.threads.net/@example/post/abc?tracking=private",
            "children": {"data": [
                {"id": "first", "media_type": "IMAGE", "media_url": "https://example.com/a?sig=private"},
                {"id": "second", "media_type": "IMAGE"},
            ]},
            "replies": [{
                "id": "hidden", "media_type": "IMAGE", "hide_status": "HIDDEN",
            }],
        }])
        self.assertEqual(set(refs), {"first", "second"})
        self.assertEqual(refs["first"]["permalink"], "https://www.threads.net/@example/post/abc")
        self.assertEqual(refs["second"]["media_url"], "")

    def test_offline_generation_needs_no_threads_or_vision_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media_cache.json"
            with patch.object(media_cache, "CACHE_PATH", path), \
                 patch.object(media_cache, "load_cache", return_value=self.cache), \
                 patch.object(vision_review.threads_client, "collect_threads_with_replies") as collect, \
                 patch.object(vision_review.vision.requests, "post") as infer:
                self.assertEqual(vision_review.main([]), 0)
                self.assertTrue(path.with_name("vision-review.html").exists())
                collect.assert_not_called()
                infer.assert_not_called()

    def test_loopback_server_serves_report_but_not_cache_files_or_foreign_hosts(self):
        with vision_review.review_server(self.cache, {}, port=0) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with urlopen(url, timeout=5) as response:
                    self.assertIn(b"qwen3-vl:4b", response.read())
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                for request, status in (
                    (url + "/media_cache.json", 404),
                    (Request(url, headers={"Host": "untrusted.example"}), 403),
                    (url + "/image/unknown", 404),
                ):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, status)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_preview_proxy_returns_image_bytes_without_exposing_source_url(self):
        source = "https://example.com/image.jpg?signature=private"
        image = requests.Response()
        image.status_code = 200
        image.headers["Content-Type"] = "image/jpeg"
        image.raw = io.BytesIO(b"preview bytes")
        with patch.object(vision_review.requests, "get", return_value=image) as fetch, \
             vision_review.review_server(
                 self.cache, {"qwen": {"media_url": source}}, port=0,
             ) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                root = f"http://127.0.0.1:{server.server_port}"
                with urlopen(root, timeout=5) as response:
                    self.assertNotIn(b"signature=private", response.read())
                with urlopen(root + "/image/qwen", timeout=5) as response:
                    self.assertEqual(response.read(), b"preview bytes")
                    self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                fetch.assert_called_once_with(source, timeout=30, stream=True)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
import io
