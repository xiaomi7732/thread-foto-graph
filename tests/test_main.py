import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import main, continuous, topic_graph
from src.vision_retry import VisionRetry


class MainIterationTests(unittest.TestCase):
    def setUp(self):
        self.threads = [
            {"id": "thread-1", "media_type": "IMAGE", "media_url": "url-1", "replies": []}
        ]
        self.patches = [
            patch.object(main.config, "require"),
            patch.object(main.config, "LOOKBACK_DAYS", 4),
            patch.object(main.config, "MAX_ROOT_THREADS", None),
            patch.object(main.config, "MAX_VISION_REQUESTS_PER_RUN", 10),
            patch.object(main.config, "MAX_ITERATIONS", 3),
            patch.object(main.threads_client, "collect_threads_with_replies", return_value=self.threads),
            patch.object(main, "load_cache", return_value={}),
            patch.object(main, "save_cache"),
            patch.object(main.topic_graph, "build_topic_graph", return_value={"nodes": [], "edges": []}),
            patch.object(main.vision, "describe_images"),
        ]
        self.mocks = [patcher.start() for patcher in self.patches]
        self.addCleanup(self.stop_patches)
        (
            self.require,
            _,
            _,
            _,
            _,
            self.collect,
            self.load_cache,
            _,
            self.build_graph,
            self.describe,
        ) = self.mocks
        self.describe.return_value = (0, False, 0, 0, 0)

    def stop_patches(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_refetches_and_builds_graph_from_latest_tree(self):
        latest_threads = self.threads + [
            {"id": "thread-2", "media_type": "IMAGE", "media_url": "url-2", "replies": []}
        ]
        self.collect.side_effect = [self.threads, latest_threads, latest_threads]
        self.describe.side_effect = [(1, False, 0, 0, 0)] + [(0, False, 0, 0, 0)] * 9

        self.assertEqual(main.main(), 0)

        self.assertEqual(self.collect.call_count, 3)
        self.assertEqual(self.describe.call_count, 10)
        self.build_graph.assert_called_once_with(latest_threads)

    def test_honors_iteration_cap(self):
        main.config.MAX_ITERATIONS = 2
        second_threads = [
            {"id": "thread-2", "media_type": "IMAGE", "media_url": "url-2", "replies": []}
        ]
        self.collect.side_effect = [self.threads, second_threads]
        self.describe.side_effect = [(1, False, 0, 0, 0)] * 4

        self.assertEqual(main.main(), 0)

        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(self.describe.call_count, 4)

    def test_does_not_repeat_for_a_failed_photo_seen_again(self):
        self.collect.side_effect = [self.threads, self.threads]

        def fail_only_once_per_photo(
            items,
            cache,
            _,
            __,
            stop_calls,
            failures,
            image_failures,
            non_retryable,
            *,
            attempted_media_ids,
            state,
        ):
            photo_ids = {
                photo["id"]
                for item in items
                for photo in main.threads_client.photo_items(item)
            }
            new_photo_ids = photo_ids - attempted_media_ids.keys()
            attempted_media_ids.update({photo_id: None for photo_id in new_photo_ids})
            return (len(new_photo_ids), stop_calls, failures, image_failures, non_retryable)

        self.describe.side_effect = fail_only_once_per_photo

        self.assertEqual(main.main(), 0)

        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(self.describe.call_count, 4)
        self.assertEqual(self.describe.call_args.kwargs["attempted_media_ids"], {"thread-1": None})

    def test_stops_after_first_pass_when_no_new_recognition_is_needed(self):
        self.load_cache.return_value = {"thread-1": "cached description"}
        self.describe.side_effect = [(0, False, 0, 0, 0), (0, False, 0, 0, 0)]

        self.assertEqual(main.main(), 0)

        self.collect.assert_called_once()
        self.assertEqual(self.describe.call_count, 2)

    def test_local_only_main_does_not_require_a_gemini_key_and_shares_run_state(self):
        with patch.object(main.config, "VISION_PROVIDER", "ollama"), \
             patch.object(main.config, "GEMINI_API_KEY", ""):
            self.load_cache.return_value = {"thread-1": "cached description"}
            self.assertEqual(main.main(), 0)
        self.assertNotIn("GEMINI_API_KEY", [call.args[0] for call in self.require.call_args_list])
        states = [call.kwargs["state"] for call in self.describe.call_args_list]
        self.assertTrue(all(state is states[0] for state in states))


class ContinuousMainTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.status = self.root / "cache" / "continuous-cycle.json"
        self.enterContext(patch.dict(os.environ, {"CONTINUOUS_CYCLE_ID": "current"}))
        self.enterContext(patch.multiple(
            main.config, THREADS_ACCESS_TOKEN="private", THREADS_USERNAME="owner",
            VISION_PROVIDER="ollama", MAX_ITERATIONS=3,
        ))
        self.enterContext(patch.object(continuous, "STATUS_PATH", self.status))
        hashes = continuous.live_hashes
        self.enterContext(patch.object(continuous, "live_hashes", side_effect=lambda: hashes(self.root)))
        self.enterContext(patch.object(main, "VisionRetry", side_effect=lambda: VisionRetry(self.root / "cache" / "retry.json")))
        self.enterContext(patch.object(main, "load_cache", return_value={}))
        self.enterContext(patch.object(main, "save_cache"))
        paths = {
            "NEEDS_REPLY_MD_PATH": self.root / "data" / "needs-reply.md",
            "NEEDS_LIKE_MD_PATH": self.root / "data" / "needs-like.md",
            "ACTIONS_MD_PATH": self.root / "data" / "actions.md",
            "TOPICS_MD_PATH": self.root / "data" / "topics.md",
            "GRAPH_JSON_PATH": self.root / "docs" / "graph.json",
            "THREADS_DIR": self.root / "data" / "threads",
            "HISTORY_DIR": self.root / "data" / "history",
        }
        self.enterContext(patch.multiple(topic_graph, **paths))
        self.collect = self.enterContext(patch.object(main.threads_client, "collect_threads_with_replies"))
        self.collect.return_value = []

    def record(self, code=0):
        return continuous.read_status(code, "current", self.status)

    def test_one_pass_and_history_only_changes_are_quiet(self):
        self.assertEqual(main.main(), 0)
        self.assertTrue(self.record()["live_changed"])
        self.collect.assert_called_once()
        self.collect.reset_mock()
        self.assertEqual(main.main(), 0)
        self.assertFalse(self.record()["live_changed"])
        (self.root / "data" / "history" / "extra-day.md").write_text("snapshot")
        self.assertEqual(main.main(), 0)
        self.assertFalse(self.record()["live_changed"])

    def test_canonical_shuffled_inputs_produce_identical_live_bytes(self):
        threads = [
            {"id": "2", "timestamp": "2026-11-12T00:00:00Z", "text": "Mountain",
             "replies": [
                 {"id": "r2", "timestamp": "2026-11-12T02:00:00Z", "text": "What camera?", "username": "other"},
                 {"id": "r1", "timestamp": "bad", "text": "Thanks", "username": "other"},
             ]},
            {"id": "1", "timestamp": "2026-11-12T00:00:00Z", "text": "Landscape", "replies": []},
        ]
        self.collect.return_value = copy.deepcopy(threads)
        self.assertEqual(main.main(), 0)
        shuffled = copy.deepcopy(threads[::-1])
        shuffled[1]["replies"].reverse()
        self.collect.return_value = shuffled
        self.assertEqual(main.main(), 0)
        self.assertFalse(self.record()["live_changed"])

    def test_auth_and_transient_errors_do_not_render_or_prune(self):
        for code in (75, 78):
            self.collect.side_effect = continuous.CycleFailure(code, "test_error", 1234)
            with patch.object(main.topic_graph, "build_topic_graph") as render:
                self.assertEqual(main.main(), code)
                render.assert_not_called()
                self.assertEqual(self.record(code)["retry_at"], 1234)
            self.assertFalse((self.root / "cache" / "retry.json").exists())

    def test_invalid_config_and_corrupt_state_fail_before_rendering(self):
        with patch.object(main.config, "validate_vision", side_effect=ValueError):
            self.assertEqual(main.main(), 78)
        self.status.unlink()
        path = self.root / "cache" / "retry.json"
        path.write_text("{bad")
        self.assertEqual(main.main(), 1)
        self.assertFalse(self.status.exists())


if __name__ == "__main__":
    unittest.main()
