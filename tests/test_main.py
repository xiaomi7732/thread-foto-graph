import unittest
from unittest.mock import patch

from src import main


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
        self.describe.side_effect = [(1, False, 0, 0, 0)] * 4

        self.assertEqual(main.main(), 0)

        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(self.describe.call_count, 4)

    def test_stops_after_first_pass_when_no_new_recognition_is_needed(self):
        self.load_cache.return_value = {"thread-1": "cached description"}
        self.describe.side_effect = [(0, False, 0, 0, 0), (0, False, 0, 0, 0)]

        self.assertEqual(main.main(), 0)

        self.collect.assert_called_once()
        self.assertEqual(self.describe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
