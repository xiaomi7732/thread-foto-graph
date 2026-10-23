import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from src import reply_gallery


class ReplyGalleryTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "id": "root",
            "label": "Nature",
            "group": 0,
            "text": "Show your photos",
            "permalink": "https://www.threads.net/@owner/post/root",
        }
        self.photo = {
            "id": "photo",
            "username": "photographer",
            "media_type": "IMAGE",
            "media_url": "https://example.com/photo.jpg?a=1&b=2",
            "permalink": "https://www.threads.net/@photographer/post/photo",
            "replied_to": {"id": "root"},
        }

    def collect(self, replies):
        with patch.object(
            reply_gallery.threads_client, "get_conversation", return_value=replies
        ) as fetch:
            threads = reply_gallery.collect_gallery({"nodes": [self.node]})
        fetch.assert_called_once_with("root")
        return threads

    def test_all_visible_images_including_answered_nested_and_carousel(self):
        replies = [
            self.photo,
            {
                "id": "answer",
                "username": "owner",
                "replied_to": {"id": "photo"},
            },
            {
                "id": "album",
                "username": "another",
                "replied_to": {"id": "answer"},
                "media_type": "CAROUSEL_ALBUM",
                "children": {"data": [
                    {"id": "one", "media_type": "IMAGE", "media_url": "https://example.com/1"},
                    {"id": "video", "media_type": "VIDEO", "media_url": "https://example.com/v"},
                    {"id": "two", "media_type": "IMAGE", "media_url": "https://example.com/2"},
                ]},
            },
            {
                **self.photo,
                "id": "hidden",
                "hide_status": "HIDDEN",
            },
            {
                **self.photo,
                "id": "hidden-child",
                "replied_to": {"id": "hidden"},
            },
            {**self.photo, "id": "video-only", "media_type": "VIDEO"},
        ]
        threads = self.collect(replies)
        self.assertEqual(len(threads[0]["photos"]), 3)
        self.assertEqual([p["id"] for p in threads[0]["photos"]], ["photo", "one", "two"])
        self.assertEqual([p["number"] for p in threads[0]["photos"]], [1, 1, 2])
        self.assertEqual(threads[0]["photos"][1]["reply_url"], self.node["permalink"])

    def test_missing_media_keeps_link_and_logs_warning(self):
        with patch("builtins.print") as log:
            threads = self.collect([{**self.photo, "media_url": None}])
        self.assertEqual(threads[0]["photos"][0]["url"], "")
        self.assertIn("no media URL", log.call_args.args[0])
        html, markdown = reply_gallery.render_gallery(threads, "now")
        self.assertIn("Image unavailable", html)
        self.assertIn("(image unavailable)", markdown)

    def test_untrusted_content_is_escaped_and_links_are_copyable(self):
        self.node["text"] = '<script>alert("root")</script>'
        self.node["label"] = "<unsafe topic>"
        threads = self.collect([{
            **self.photo,
            "username": '"><script>alert(1)</script>',
            "text": '<img src=x onerror="alert(1)">',
        }])
        html, markdown = reply_gallery.render_gallery(threads, "now")
        self.assertNotIn('<script>alert', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn("&lt;unsafe topic&gt;", html)
        self.assertIn('width="160"', markdown)
        self.assertIn(self.photo["permalink"], markdown)
        self.assertIn("Copy all Markdown", html)
        self.assertIn("Copy all reply links", html)
        self.assertIn(self.node["permalink"], markdown)
        self.assertIn("a=1&amp;b=2", markdown)
        self.assertIn("Copy the selected text manually", html)

    def test_unsafe_url_fails_instead_of_publishing(self):
        with self.assertRaises(ValueError):
            self.collect([{**self.photo, "media_url": "javascript:alert(1)"}])
        with self.assertRaises(ValueError):
            self.collect([{**self.photo, "permalink": "javascript:alert(1)"}])

    def test_empty_graph_does_not_fetch_and_renders_empty_state(self):
        with patch.object(reply_gallery.threads_client, "get_conversation") as fetch:
            threads = reply_gallery.collect_gallery({"nodes": []})
        fetch.assert_not_called()
        html, markdown = reply_gallery.render_gallery(threads, "now")
        self.assertIn("No current topics.", html)
        self.assertIn("No current topics.", markdown)

    def test_thread_without_photos_is_still_shown(self):
        html, markdown = reply_gallery.render_gallery(self.collect([]), "now")
        self.assertIn("No reply images.", html)
        self.assertIn("Show your photos", markdown)

    def test_favorites_controls_and_reply_urls_are_rendered_for_each_image(self):
        class GalleryParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.cards = []
                self.favorite_buttons = []
                self.elements = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "article":
                    self.cards.append(attrs)
                if tag == "button" and attrs.get("class") == "favorite-toggle":
                    self.favorite_buttons.append(attrs)
                if "id" in attrs:
                    self.elements[attrs["id"]] = attrs

        threads = self.collect([self.photo, {**self.photo, "id": "second"}])
        html, markdown = reply_gallery.render_gallery(threads, "now")
        parser = GalleryParser()
        parser.feed(html)
        self.assertEqual(len(parser.cards), 2)
        self.assertTrue(all(
            card["data-reply-url"] == self.photo["permalink"] for card in parser.cards
        ))
        self.assertEqual([card["data-photo-id"] for card in parser.cards], ["photo", "second"])
        self.assertEqual(len(parser.favorite_buttons), 2)
        self.assertTrue(all(
            button["aria-pressed"] == "false" for button in parser.favorite_buttons
        ))
        self.assertEqual(parser.elements["favorite-view"]["aria-pressed"], "false")
        self.assertIn("disabled", parser.elements["copy-favorites"])
        self.assertIn("disabled", parser.elements["reset-favorites"])
        self.assertNotIn("favorite-toggle", markdown)
        self.assertIn("window.localStorage", html)
        self.assertNotIn("sessionStorage", html)

    def test_photo_identity_survives_media_url_and_reply_order_changes(self):
        original = self.collect([self.photo])[0]["photos"][0]
        refreshed = self.collect([
            {**self.photo, "id": "new-reply"},
            {**self.photo, "media_url": "https://example.com/refreshed.jpg"},
        ])[0]["photos"][1]
        self.assertEqual(original["id"], refreshed["id"])
        self.assertNotEqual(original["url"], refreshed["url"])
        html, _ = reply_gallery.render_gallery(
            [{**self.node, "topic": "Nature", "url": self.node["permalink"], "photos": [refreshed]}],
            "now",
        )
        self.assertIn('data-photo-id="photo"', html)

    def test_photo_identity_is_escaped_in_generated_attribute(self):
        html, _ = reply_gallery.render_gallery(
            self.collect([{**self.photo, "id": 'id" onclick="alert(1)'}]), "now"
        )
        self.assertIn('data-photo-id="id&quot; onclick=&quot;alert(1)"', html)
        self.assertNotIn('data-photo-id="id" onclick=', html)

    def test_main_writes_all_outputs_without_touching_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            graph = json.dumps({"nodes": [self.node], "edges": []})
            (root / "docs" / "graph.json").write_text(graph, encoding="utf-8")
            with (
                patch.object(reply_gallery, "ROOT_DIR", root),
                patch.object(reply_gallery.config, "THREADS_ACCESS_TOKEN", "test-token"),
                patch.object(
                    reply_gallery.threads_client,
                    "get_conversation",
                    return_value=[self.photo],
                ),
            ):
                self.assertEqual(reply_gallery.main(), 0)
            self.assertTrue((root / "docs" / "reply-images.html").is_file())
            self.assertFalse((root / "docs" / "reply-images.md").exists())
            self.assertIn(
                'href="reply-images.txt" download="reply-images.md"',
                (root / "docs" / "reply-images.html").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (root / "docs" / "reply-images.txt").read_text(encoding="utf-8"),
                (root / "data" / "reply-images.md").read_text(encoding="utf-8"),
            )
            self.assertEqual((root / "docs" / "graph.json").read_text(encoding="utf-8"), graph)


if __name__ == "__main__":
    unittest.main()
