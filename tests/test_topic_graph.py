import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import threads_client, topic_graph


class PhotoDescriptionTests(unittest.TestCase):
    def test_multiline_photograph_read_stays_in_its_reply_bullet(self):
        expected = (
            "Description: A mountain at sunrise. "
            "Photograph read: Warm light emphasizes the ridges."
        )
        for separator in ("\n", "\n\n", "\r\n\r\n"):
            with self.subTest(separator=separator):
                description = separator.join([
                    "Description: A mountain at sunrise.",
                    "Photograph read: Warm light emphasizes the ridges.",
                ])
                reply = {
                    "username": "photographer",
                    "photo_descriptions": [description, None],
                }

                lines = topic_graph._render_reply_lines(reply, 1)

                self.assertEqual(lines[1], f"      - Photo: {expected}")
                self.assertEqual(
                    lines[2], f"      - Photo: {topic_graph.PENDING_PHOTO_TEXT}"
                )
                self.assertTrue(all("\n" not in line and "\r" not in line for line in lines))
                self.assertEqual(reply["photo_descriptions"], [description, None])


class NeedsLikeTests(unittest.TestCase):
    def setUp(self):
        self.thread = {"id": "thread-1", "text": "Root post"}

    def test_already_liked_text_reply_is_not_actionable(self):
        reply = {
            "id": "reply-1",
            "username": "commenter",
            "text": "Nice shot!",
            "is_liked": True,
        }

        reply_items, like_items = topic_graph._split_action_items(
            [{**self.thread, "replies": [reply]}]
        )

        self.assertEqual(reply_items, [])
        self.assertEqual(like_items, [])
        self.assertEqual(topic_graph._prune_for_action([reply]), ([], 1))
        self.assertFalse(topic_graph._reply_node(reply)["needs_response"])

    def test_liked_reply_is_context_only_for_actionable_descendant(self):
        parent = {
            "username": "commenter",
            "text": "Nice shot!",
            "is_liked": True,
            "replies": [{"username": "another", "text": "Agreed!"}],
        }

        pruned, _ = topic_graph._prune_for_action([parent])
        lines = topic_graph._render_reply_lines(pruned[0], 0, show_action_marker=True)

        self.assertNotIn("**[NEEDS LIKE]**", lines[0])
        self.assertIn("**[NEEDS LIKE]**", lines[1])

    def test_unliked_text_reply_still_needs_like(self):
        reply = {
            "id": "reply-1",
            "username": "commenter",
            "text": "Nice shot!",
            "is_liked": False,
        }

        reply_items, like_items = topic_graph._split_action_items(
            [{**self.thread, "replies": [reply]}]
        )

        self.assertEqual(reply_items, [])
        self.assertEqual([entry[1] for entry in like_items], [reply])

    def test_liked_photo_reply_still_needs_a_written_reply(self):
        reply = {
            "id": "reply-1",
            "username": "commenter",
            "text": "Nice shot!",
            "is_liked": True,
            "photo_descriptions": ["A mountain at sunrise."],
        }

        reply_items, like_items = topic_graph._split_action_items(
            [{**self.thread, "replies": [reply]}]
        )

        self.assertEqual([entry[1] for entry in reply_items], [reply])
        self.assertEqual(like_items, [])

    def test_text_classification_uses_intent_before_word_count(self):
        cases = [
            (None, "like"),
            ("", "like"),
            (" \t\n ", "like"),
            ("\U0001f44d \U0001f44d \U0001f44d", "like"),
            ("Nice shot _", "like"),
            ("1\ufe0f\u20e3 2\ufe0f\u20e3 3\ufe0f\u20e3", "like"),
            ("1\u20e3 2\u20e3 3\u20e3", "like"),
            ("#\ufe0f\u20e3 *\ufe0f\u20e3", "like"),
            ("nai\u0308ve photo", "like"),
            ("\u0915\u093f photo", "like"),
            ("\u0905\u091a\u094d\u200d\u091b\u0940 \u092b\u094b\u091f\u094b", "like"),
            ("\u0639\u06a9\u0633\u200c\u0647\u0627 \u0632\u06cc\u0628\u0627\u0633\u062a", "like"),
            ("\u2139\ufe0f \u2139\ufe0f \u2139\ufe0f", "like"),
            ("Nice shot \u2139\ufe0f", "like"),
            ("nai\u0308ve warm photo", "reply"),
            ("1 2 3", "reply"),
            ("Thank you!", "like"),
            ("Thank you !!! \U0001f44d", "like"),
            ("THANK YOU SO MUCH!!!", "like"),
            ("Thanks for sharing.", "like"),
            ("I love this!", "like"),
            ("What a beautiful shot.", "like"),
            ("What camera?", "reply"),
            ("Why\uff1f", "reply"),
            ("How\u061f", "reply"),
            ("Thank you so much?", "reply"),
            ("I tried this yesterday", "reply"),
            ("Nice\twarm\nlight", "reply"),
            ("Don't stop!", "like"),
            ("Don\u2019t stop!", "like"),
            ("Don't stop shooting!", "reply"),
            ("caf\u00e9 tr\u00e8s joli", "reply"),
            ("Thank you so much, but where was this taken", "reply"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(topic_graph._action_category({"text": text}), expected)

    def test_unicode_word_tokenization_preserves_words_and_contractions(self):
        cases = [
            ("nai\u0308ve photo", ["na\u00efve", "photo"]),
            ("\u0915\u093f photo", ["\u0915\u093f", "photo"]),
            ("\u0905\u091a\u094d\u200d\u091b\u0940 photo", ["\u0905\u091a\u094d\u200d\u091b\u0940", "photo"]),
            ("\u0639\u06a9\u0633\u200c\u0647\u0627 photo", ["\u0639\u06a9\u0633\u200c\u0647\u0627", "photo"]),
            ("\u2139\ufe0f \u2139\ufe0f", []),
            ("Nice shot \u2139\ufe0f", ["Nice", "shot"]),
            ("A\ufe0f photo", ["A\ufe0f", "photo"]),
            ("Don't stop", ["Don't", "stop"]),
            ("Don\u2019t stop", ["Don\u2019t", "stop"]),
            ("Nice_shot _", ["Nice", "shot"]),
            ("_\u0308 \U0001f44d", []),
            ("1\ufe0f\u20e3 2\u20e3 #\ufe0f\u20e3 *\u20e3", []),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(topic_graph._text_words(text), expected)

    def test_liked_substantive_text_still_needs_a_written_reply(self):
        for text in ("I tried this yesterday", "What camera?"):
            for liked in (False, True):
                with self.subTest(text=text, liked=liked):
                    reply = {
                        "id": "reply-1",
                        "username": "commenter",
                        "text": text,
                        "is_liked": liked,
                    }
                    reply_items, like_items = topic_graph._split_action_items(
                        [{**self.thread, "replies": [reply]}]
                    )
                    self.assertEqual([entry[1] for entry in reply_items], [reply])
                    self.assertEqual(like_items, [])
                    self.assertTrue(topic_graph._reply_node(reply)["needs_response"])
                    self.assertEqual(topic_graph._reply_node(reply)["action"], "reply")

    def test_liked_common_acknowledgement_is_resolved(self):
        for text in (
            "Thank you so much!",
            "Nice shot _",
            "1\ufe0f\u20e3 2\ufe0f\u20e3 3\ufe0f\u20e3",
            "nai\u0308ve photo",
            "\u0915\u093f photo",
            "\u0905\u091a\u094d\u200d\u091b\u0940 \u092b\u094b\u091f\u094b",
            "\u0639\u06a9\u0633\u200c\u0647\u0627 \u0632\u06cc\u0628\u0627\u0633\u062a",
            "\u2139\ufe0f \u2139\ufe0f \u2139\ufe0f",
            "Nice shot \u2139\ufe0f",
        ):
            with self.subTest(text=text):
                reply = {
                    "username": "commenter",
                    "text": text,
                    "is_liked": True,
                }
                self.assertFalse(topic_graph._needs_action(reply))
                self.assertEqual(topic_graph._prune_for_action([reply]), ([], 1))
                self.assertIsNone(topic_graph._reply_node(reply)["action"])

    def test_pending_photo_overrides_acknowledgement(self):
        reply = {
            "username": "commenter",
            "text": "Thank you so much!",
            "photo_descriptions": [None],
            "is_liked": True,
        }
        self.assertEqual(topic_graph._action_category(reply), "reply")
        self.assertTrue(topic_graph._needs_action(reply))

    @patch.object(topic_graph.config, "THREADS_USERNAME", "owner")
    def test_substantive_text_respects_existing_resolution_rules(self):
        cases = [
            {"username": "owner"},
            {"username": "commenter", "replies": [{"username": "owner"}]},
            {
                "username": "commenter",
                "hide_status": "HIDDEN",
                "replies": [{"username": "another", "text": "What camera?"}],
            },
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                reply = {"text": "I tried this yesterday", **fields}
                self.assertFalse(topic_graph._needs_action(reply))
                self.assertEqual(list(topic_graph._flatten_actionable([reply])), [])
                self.assertEqual(topic_graph._prune_for_action([reply])[0], [])
                self.assertFalse(topic_graph._reply_node(reply)["needs_response"])

    def test_generated_outputs_agree_on_text_reply_actions(self):
        thread = {
            **self.thread,
            "replies": [
                {"username": "short", "text": "Thank you"},
                {"username": "thanks", "text": "Thank you so much!"},
                {
                    "username": "parent",
                    "text": "Nice shot!",
                    "is_liked": True,
                    "replies": [{
                        "username": "longer",
                        "text": "I tried this yesterday",
                        "is_liked": True,
                    }],
                },
                {"username": "questioner", "text": "What camera?", "is_liked": True},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "ACTIONS_MD_PATH": root / "actions.md",
                "NEEDS_REPLY_MD_PATH": root / "needs-reply.md",
                "NEEDS_LIKE_MD_PATH": root / "needs-like.md",
                "TOPICS_MD_PATH": root / "topics.md",
                "THREADS_DIR": root / "threads",
                "HISTORY_DIR": root / "history",
                "GRAPH_JSON_PATH": root / "graph.json",
            }
            with patch.multiple(topic_graph, **paths):
                graph = topic_graph.build_topic_graph([thread])

            needs_reply = paths["NEEDS_REPLY_MD_PATH"].read_text(encoding="utf-8")
            needs_like = paths["NEEDS_LIKE_MD_PATH"].read_text(encoding="utf-8")
            topics = paths["TOPICS_MD_PATH"].read_text(encoding="utf-8")
            actions = paths["ACTIONS_MD_PATH"].read_text(encoding="utf-8")
            self.assertIn("## Needs Reply (2)", needs_reply)
            self.assertIn("**longer**: I tried this yesterday", needs_reply)
            self.assertIn("_Replying to: Nice shot!_", needs_reply)
            self.assertIn("**questioner**: What camera?", needs_reply)
            self.assertNotIn("**thanks**:", needs_reply)
            self.assertIn("## Needs Like (2)", needs_like)
            self.assertIn("- thanks: Thank you so much!", needs_like)
            self.assertNotIn("- longer:", needs_like)
            self.assertNotIn("- questioner:", needs_like)
            self.assertIn("**[NEEDS REPLY]** Reply by longer:", topics)
            self.assertIn("**[NEEDS REPLY]** Reply by questioner:", topics)
            self.assertIn("**[NEEDS LIKE]** Reply by thanks:", topics)
            self.assertNotIn("**[NEEDS LIKE]** Reply by parent:", topics)
            self.assertIn("**longer**: I tried this yesterday", actions)
            self.assertIn("- thanks: Thank you so much!", actions)
            archived = (paths["THREADS_DIR"] / "thread-1.md").read_text(encoding="utf-8")
            self.assertIn("Reply by parent: Nice shot!", archived)
            self.assertIn("Reply by longer: I tried this yesterday", archived)
            history = sorted(paths["HISTORY_DIR"].glob("*.md"))
            self.assertEqual(len(history), 2)
            self.assertEqual(
                {file.read_text(encoding="utf-8") for file in history},
                {needs_reply, needs_like},
            )
            self.assertEqual(
                json.loads(paths["GRAPH_JSON_PATH"].read_text(encoding="utf-8")), graph
            )
            nodes = graph["nodes"][0]["replies"]
            self.assertEqual(nodes[0]["action"], "like")
            self.assertEqual(nodes[1]["action"], "like")
            self.assertIsNone(nodes[2]["action"])
            self.assertEqual(nodes[2]["replies"][0]["action"], "reply")
            self.assertEqual(nodes[3]["action"], "reply")

    def test_conversation_request_includes_liked_status(self):
        self.assertIn("is_liked", threads_client.REPLY_FIELDS)


if __name__ == "__main__":
    unittest.main()
