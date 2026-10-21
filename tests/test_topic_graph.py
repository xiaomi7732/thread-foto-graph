import unittest

from src import threads_client, topic_graph


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

    def test_conversation_request_includes_liked_status(self):
        self.assertIn("is_liked", threads_client.REPLY_FIELDS)


if __name__ == "__main__":
    unittest.main()
