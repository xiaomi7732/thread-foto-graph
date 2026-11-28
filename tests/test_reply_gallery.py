import json
import tempfile
import unittest
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from src import reply_gallery, photo_styles


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
        self.assertIn('data-image-url=""', html)
        self.assertIn(
            f'data-copy="{self.photo["permalink"]}" data-kind="">Copy reply link',
            html,
        )
        self.assertIn(f'href="{self.photo["permalink"]}"', markdown)

    def test_cached_styles_support_legacy_and_structured_entries_without_inference(self):
        for entry in (
            "Description: A flower. Photograph read: Soft light. Photo type: Macro photography.",
            {"description": "Description: A flower.\nPhotograph read: Soft light.\n"
             "Photo type:\nMacro photography\nWatch-outs: None observed."},
        ):
            with self.subTest(entry=entry), patch.object(
                reply_gallery.threads_client, "get_conversation", return_value=[self.photo],
            ):
                threads = reply_gallery.collect_gallery({"nodes": [self.node]}, {"photo": entry})
                self.assertEqual(threads[0]["photos"][0]["style"], "Macro photography")
                html, _ = reply_gallery.render_gallery(threads, "now")
                self.assertIn('data-photo-style="macro"', html)
                self.assertIn('value="macro"', html)
                self.assertIn('Macro (1)', html)
                self.assertIn('<p class="photo-style">Macro photography</p>', html)
                self.assertNotIn("{{STYLE_FILTERS}}", html)
        self.assertEqual(reply_gallery.photo_style(None), "Unknown")
        self.assertEqual(reply_gallery.photo_style({"description": "No classification"}), "Unknown")

    def test_style_variants_share_one_filter_without_losing_original_labels(self):
        threads = self.collect([self.photo])
        labels = ["Macro photography", "Close-up", "Macro flower photography",
                  "Botanical photography", "Unusual custom style", "Unknown"]
        threads[0]["photos"] = [
            {**threads[0]["photos"][0], "id": str(index), "style": label}
            for index, label in enumerate(labels)
        ]
        html, _ = reply_gallery.render_gallery(threads, "now")
        self.assertEqual(html.count('class="style-filter"'), 4)
        self.assertIn("Macro (3)", html)
        self.assertIn("Plants &amp; flowers (1)", html)
        self.assertIn("Other (1)", html)
        self.assertIn("Unknown (1)", html)
        self.assertEqual(html.count('data-photo-style="macro"'), 3)
        for label in labels:
            self.assertIn(f'<p class="photo-style">{label}</p>', html)

    def test_style_categories_are_bounded_and_match_whole_words(self):
        examples = {
            "Bird photography": "Animals",
            "Birds and animals": "Animals",
            "Flowers": "Plants & flowers",
            "Environmental portrait": "Portrait",
            "Urban architecture": "Architecture",
            "Candid street photography": "Street & documentary",
            "Astrophotography": "Landscape",
            "Still-life photography": "Food & still life",
            "Intentional camera movement": "Abstract & experimental",
            "Delicate lighting": "Other",
            "Unknown.": "Unknown",
        }
        for label, expected in examples.items():
            with self.subTest(label=label):
                self.assertEqual(reply_gallery._style_category(label), expected)
        threads = self.collect([self.photo])
        threads[0]["photos"] = [
            {**threads[0]["photos"][0], "id": str(index), "style": f"Custom style {index}"}
            for index in range(100)
        ]
        html, _ = reply_gallery.render_gallery(threads, "now")
        self.assertEqual(html.count('class="style-filter"'), 1)
        self.assertIn("Other (100)", html)

    def test_carousel_styles_use_individual_photo_cache_ids_and_escape_labels(self):
        with patch.object(reply_gallery.threads_client, "get_conversation", return_value=[{
            **self.photo, "media_type": "CAROUSEL_ALBUM",
            "children": {"data": [
                {"id": "one", "media_type": "IMAGE", "media_url": "https://example.com/1"},
                {"id": "two", "media_type": "IMAGE", "media_url": "https://example.com/2"},
            ]},
        }]):
            threads = reply_gallery.collect_gallery(
                {"nodes": [self.node]},
                {"one": 'Photo type: Portrait"><script>bad</script>'},
            )
        self.assertEqual(threads[0]["photos"][1]["style"], "Unknown")
        html, _ = reply_gallery.render_gallery(threads, "now")
        self.assertIn('data-photo-style="unknown"', html)
        self.assertIn('Unknown (1)', html)
        self.assertNotIn("<script>bad</script>", html)
        self.assertIn("Portrait&quot;&gt;&lt;script&gt;bad&lt;/script&gt;", html)

    def test_published_style_takes_precedence_over_a_different_workflow_cache(self):
        with patch.object(reply_gallery.threads_client, "get_conversation", return_value=[
            self.photo, {**self.photo, "id": "new-photo"},
        ]):
            threads = reply_gallery.collect_gallery(
                {"nodes": [self.node]},
                {"photo": "Photo type: Portrait", "new-photo": "Photo type: Macro"},
                {"photo": "Landscape"},
            )
        self.assertEqual([photo["style"] for photo in threads[0]["photos"]], ["Landscape", "Macro"])

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
        self.assertIn(self.photo["permalink"], html)
        self.assertIn("Copy all Markdown", html)
        self.assertIn("Copy all image links", html)
        self.assertIn(self.node["permalink"], markdown)
        self.assertIn("a=1&amp;b=2", markdown)
        self.assertIn("Copy the selected text manually", html)

    def test_carousel_copies_specific_images_without_repeated_thread_links(self):
        threads = self.collect([{
            **self.photo,
            "media_type": "CAROUSEL_ALBUM",
            "children": {"data": [
                {"id": "one", "media_type": "IMAGE", "media_url": "https://example.com/1"},
                {"id": "two", "media_type": "IMAGE", "media_url": "https://example.com/2"},
            ]},
        }])
        html, markdown = reply_gallery.render_gallery(threads, "now")
        self.assertIn(
            'data-copy="https://example.com/1\nhttps://example.com/2" '
            'data-kind="all-links">Copy all image links', html,
        )
        cards = html.split("<article ")[1:]
        self.assertEqual(len(cards), 2)
        for index, card in enumerate(cards, start=1):
            card = card.split("</article>", 1)[0]
            image_url = f"https://example.com/{index}"
            self.assertIn(f'data-image-url="{image_url}"', card)
            self.assertIn(f'class="thumbnail" href="{image_url}"', card)
            self.assertIn(f'data-copy="{image_url}" data-kind="">Copy image link', card)
            self.assertIn(f'href="{self.photo["permalink"]}">Reply</a>', card)
            self.assertNotIn(self.node["permalink"], card)
            self.assertIn(f'<a href="{image_url}"><img src="{image_url}"', markdown)
        self.assertEqual(html.count("Copy thread link"), 1)
        self.assertNotIn(">Thread</a>", html)

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

    def test_topic_selector_distinguishes_threads_and_escapes_labels_and_ids(self):
        threads = self.collect([self.photo])
        threads[0]["topic"] = 'Nature & "<trees>"'
        threads.extend([
            {**threads[0], "id": 'same-"topic'},
            {**threads[0], "id": "different-topic", "group": 7, "photos": []},
        ])
        html, _ = reply_gallery.render_gallery(threads, "now")
        self.assertIn('<label for="topic-filter">Topic</label>', html)
        self.assertIn('<option value="">All topics</option>', html)
        self.assertEqual(html.count('<optgroup label="Nature &amp; &quot;&lt;trees&gt;&quot;">'), 2)
        self.assertIn('<option value="root" title="Show your photos">Show your photos</option>', html)
        self.assertIn('<option value="same-&quot;topic"', html)
        self.assertIn('data-thread-id="same-&quot;topic"', html)
        self.assertIn('<option value="different-topic"', html)
        self.assertEqual(html.count('<div class="gallery-thread" data-thread-id='), 3)
        self.assertNotIn("{{TOPIC_OPTIONS}}", html)

    def test_long_topic_prompt_has_a_short_label_and_full_title(self):
        threads = self.collect([])
        threads[0]["text"] = "A <prompt> " * 20
        html, _ = reply_gallery.render_gallery(threads, "now")
        text = " ".join(threads[0]["text"].split())
        self.assertIn(
            f'<option value="root" title="{escape(text)}">{escape(text[:97])}...</option>',
            html,
        )

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
            (root / "data").mkdir()
            (root / "data" / "photo-styles.json").write_text(
                '{"version":1,"styles":{"photo":"Published landscape"}}', encoding="utf-8",
            )
            with (
                patch.object(reply_gallery, "ROOT_DIR", root),
                patch.object(reply_gallery.config, "THREADS_ACCESS_TOKEN", "test-token"),
                patch.object(reply_gallery, "load_cache", return_value={"photo": "Photo type: Landscape"}),
                patch.object(
                    reply_gallery.threads_client,
                    "get_conversation",
                    return_value=[self.photo],
                ),
            ):
                self.assertEqual(reply_gallery.main(), 0)
            self.assertTrue((root / "docs" / "reply-images.html").is_file())
            self.assertIn('data-photo-style="landscape"', (
                root / "docs" / "reply-images.html"
            ).read_text(encoding="utf-8"))
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


class PhotoStyleIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "photo-styles.json"
        self.enterContext(patch.object(photo_styles, "STYLE_PATH", self.path))

    def test_export_excludes_roots_hidden_branches_and_private_cache_metadata(self):
        image = lambda media_id: {"id": media_id, "media_type": "IMAGE"}
        threads = [{**image("root"), "replies": [
            {**image("one"), "replies": [image("two")]},
            {**image("hidden"), "hide_status": "HIDDEN", "replies": [image("hidden-child")]},
        ]}]
        cache = {
            media_id: {"description": f"Photo type: {style}\nWatch-outs: private notes",
                       "model": "private model", "provider": "private provider"}
            for media_id, style in (("root", "Root style"), ("one", "Macro"),
                                    ("hidden", "Hidden style"), ("hidden-child", "Hidden style"))
        }
        photo_styles.write_styles(threads, cache, {"one": "Old", "two": "Portrait", "expired": "Old"})
        self.assertEqual(photo_styles.load_styles(), {"one": "Macro", "two": "Portrait"})
        text = self.path.read_text()
        self.assertNotIn("private", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("root", text)

    def test_missing_or_unknown_cache_preserves_known_styles_and_stable_bytes(self):
        threads = [{"id": "root", "replies": [{"id": "photo", "media_type": "IMAGE"}]}]
        photo_styles.write_styles(threads, {}, {"photo": "Landscape"})
        before = self.path.read_bytes()
        photo_styles.write_styles(threads, {"photo": "Photo type: Unknown"}, photo_styles.load_styles())
        self.assertEqual(self.path.read_bytes(), before)
        photo_styles.write_styles([], {}, photo_styles.load_styles())
        self.assertEqual(photo_styles.load_styles(), {})

    def test_absent_index_is_supported_but_invalid_schema_fails_closed(self):
        self.assertEqual(photo_styles.load_styles(), {})
        for data in ([], {"version": True, "styles": {}}, {"version": 2, "styles": {}},
                     {"version": 1, "styles": {"photo": None}}, {"version": 1, "styles": []}):
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    photo_styles.load_styles()


if __name__ == "__main__":
    unittest.main()
