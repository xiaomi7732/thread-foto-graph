import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import cache as media_cache, vision


PHOTO_DESCRIPTION = (
    "Description: A mountain at sunrise.\n"
    "Photograph read: Warm side light emphasizes the ridges.\n"
    "Photo type: Artistic landscape.\n"
    "Watch-outs:"
)


def response(status=200, data=None, headers=None):
    result = vision.requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.url = "https://example.com/api"
    result._content = b"photo"
    if data is not None:
        import json
        result._content = json.dumps(data).encode()
    return result


def local_response(text=PHOTO_DESCRIPTION, **kwargs):
    return response(data={"message": {"content": text}, "done": True, **kwargs})


def gemini_response(text=PHOTO_DESCRIPTION):
    return response(data={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def photo(media_id="photo"):
    return {"id": media_id, "media_type": "IMAGE", "media_url": "https://example.com/photo.jpg"}


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
        self.assertEqual(media_cache.description_text(restored["sun"]), raw)
        self.assertEqual(restored["sun"]["provider"], "gemini")
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


class LocalVisionTests(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(vision.config, "GEMINI_API_KEY", "test-key"),
            patch.object(vision.config, "OLLAMA_MODEL", "qwen3-vl:4b"),
            patch.object(vision.time, "sleep"),
            patch.object(vision.requests, "get", return_value=response(headers={"Content-Type": "image/jpeg"})),
            patch.object(vision.requests, "post"),
        ]
        self.mocks = [self.enterContext(patcher) for patcher in patches]
        _, _, self.sleep, self.get, self.post = self.mocks
        self.enterContext(patch.object(
            vision.requests.Session, "post",
            side_effect=lambda *args, **kwargs: self.post(*args, **kwargs),
        ))
        self.cache = {}
        self.attempted = {}
        self.state = vision.VisionRun(provider="auto", gemini_budget=10, local_budget=10)

    def describe(self, media_id="photo"):
        item = photo(media_id)
        result = vision.describe_images(
            [item], self.cache, 10, state=self.state,
            attempted_media_ids=self.attempted,
        )
        return item, result

    def test_local_success_records_provenance_and_reuses_cache_without_cloud_key(self):
        self.post.return_value = local_response()
        with patch.object(vision.config, "GEMINI_API_KEY", ""):
            item, result = self.describe()
            entry = dict(self.cache["photo"])
            self.describe()
        self.assertEqual(result[0], 0)
        self.assertEqual(item["photo_descriptions"], [PHOTO_DESCRIPTION.rsplit("\nWatch-outs:", 1)[0]])
        self.assertEqual(entry["provider"], "ollama")
        self.assertEqual(entry["model"], "qwen3-vl:4b")
        self.assertTrue(entry["described_at"].endswith("Z"))
        self.assertIsNone(entry["fallback_reason"])
        self.assertEqual(self.cache["photo"], entry)
        self.assertEqual(self.state.local_requests, 1)
        self.assertEqual(self.state.gemini_requests, 0)
        self.get.assert_called_once()
        self.post.assert_called_once()
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["messages"][0]["images"], ["cGhvdG8="])
        self.assertEqual(body["messages"][0]["content"], vision.PROMPT)
        self.assertFalse(body["stream"])
        self.assertIs(body["think"], False)
        self.sleep.assert_not_called()

    def test_local_failures_fall_back_using_one_download_and_actual_provider(self):
        cases = [
            (vision.requests.Timeout("private exception text"), "ollama_timeout"),
            (vision.requests.ConnectionError("private exception text"), "ollama_unavailable"),
            (response(404), "ollama_unavailable"),
            (local_response(""), "ollama_invalid_output"),
            (local_response(done_reason="length"), "ollama_invalid_output"),
            (response(data={"message": None}), "ollama_invalid_output"),
        ]
        for failure, reason in cases:
            with self.subTest(reason=reason, failure=failure):
                self.state = vision.VisionRun(provider="auto")
                self.cache, self.attempted = {}, {}
                self.get.reset_mock()
                self.post.reset_mock()
                self.post.side_effect = [failure, gemini_response()]
                _, result = self.describe()
                entry = self.cache["photo"]
                self.assertEqual(entry["provider"], "gemini")
                self.assertEqual(entry["model"], vision.config.GEMINI_MODEL)
                self.assertEqual(entry["fallback_reason"], reason)
                self.assertNotIn("private exception", str(entry))
                self.assertEqual(result[0], 1)
                self.get.assert_called_once()
                calls = self.post.call_args_list
                self.assertEqual(
                    calls[0].kwargs["json"]["messages"][0]["images"][0],
                    calls[1].kwargs["json"]["contents"][0]["parts"][1]["inline_data"]["data"],
                )

    def test_unavailable_local_endpoint_is_not_retried_in_later_batches(self):
        self.post.side_effect = [
            vision.requests.ConnectionError(), gemini_response(), gemini_response(),
        ]
        self.describe("first")
        self.describe("second")
        self.assertEqual(self.state.local_requests, 1)
        self.assertEqual(self.state.gemini_requests, 2)
        self.assertEqual(self.cache["second"]["fallback_reason"], "ollama_unavailable")

    def test_gemini_quota_does_not_stop_later_local_success(self):
        self.post.side_effect = [local_response("invalid"), response(429), local_response()]
        first, result = self.describe("first")
        self.assertFalse(result[1])
        self.assertEqual(first["photo_descriptions"], [None])
        self.describe("second")
        self.assertTrue(self.state.gemini_disabled)
        self.assertEqual(self.state.gemini_requests, 1)
        self.assertEqual(self.cache["second"]["provider"], "ollama")
        self.assertNotIn("first", self.cache)

    def test_separate_budgets_allow_the_other_provider_to_continue(self):
        self.state.gemini_budget = 0
        self.post.return_value = local_response()
        self.describe("local")
        self.assertEqual(self.cache["local"]["provider"], "ollama")
        self.state.local_budget = self.state.local_requests
        self.state.gemini_budget = 1
        self.post.return_value = gemini_response()
        self.describe("cloud")
        self.assertEqual(self.cache["cloud"]["fallback_reason"], "ollama_budget_exhausted")
        self.assertEqual((self.state.local_requests, self.state.gemini_requests), (1, 1))

    def test_local_success_does_not_reset_gemini_failure_streak(self):
        self.state.local_budget = 20
        error = vision.requests.HTTPError(response=response(403))
        with patch.object(vision, "describe_local") as local, patch.object(vision, "describe_image") as gemini:
            local.side_effect = [
                outcome for _ in range(5)
                for outcome in (vision.OllamaResponseError(), PHOTO_DESCRIPTION)
            ]
            gemini.side_effect = error
            for index in range(10):
                self.describe(str(index))
        self.assertEqual(self.state.non_retryable_failures, 5)
        self.assertTrue(self.state.gemini_disabled)
        self.assertFalse(self.state.stopped())
        self.assertEqual(self.cache["9"]["provider"], "ollama")

    def test_image_download_failure_calls_neither_provider_and_is_retryable(self):
        self.get.side_effect = vision.requests.ConnectionError("url?access_token=secret")
        item, _ = self.describe()
        self.assertIn(vision.DESCRIPTION_UNAVAILABLE_PREFIX, item["photo_descriptions"][0])
        self.assertEqual(self.cache, {})
        self.assertEqual((self.state.local_requests, self.state.gemini_requests), (0, 0))
        self.describe()
        self.get.assert_called_once()
        self.post.assert_not_called()

    def test_source_host_outage_breaker_is_shared_across_batches(self):
        self.get.side_effect = vision.requests.ConnectionError()
        for index in range(7):
            self.describe(str(index))
        self.assertEqual(self.get.call_count, vision.MAX_CONSECUTIVE_IMAGE_FETCH_FAILURES)
        self.assertTrue(self.state.stopped())
        self.post.assert_not_called()

    def test_local_only_failure_never_calls_cloud_or_caches_error(self):
        self.state.provider = "ollama"
        self.post.side_effect = vision.requests.Timeout()
        item, result = self.describe()
        self.assertEqual(item["photo_descriptions"], [None])
        self.assertTrue(result[1])
        self.assertEqual(self.cache, {})
        self.post.assert_called_once()

    def test_explicit_local_refusal_is_not_bypassed_and_is_attributed(self):
        self.post.return_value = local_response("I cannot describe this image.")
        item, _ = self.describe()
        self.assertIn("blocked by Ollama", item["photo_descriptions"][0])
        self.assertEqual(self.cache["photo"]["provider"], "ollama")
        self.assertEqual(self.state.gemini_requests, 0)
        self.post.assert_called_once()

    def test_valid_photograph_read_disclaimer_is_not_a_refusal(self):
        text = PHOTO_DESCRIPTION.replace(
            "Warm side light emphasizes the ridges.",
            "I cannot provide exact camera settings, but warm side light emphasizes the ridges.",
        )
        self.post.return_value = local_response(text)
        item, _ = self.describe()
        self.assertEqual(self.cache["photo"]["description"], text)
        self.assertNotIn("blocked by", item["photo_descriptions"][0])
        self.assertEqual(self.state.gemini_requests, 0)

    def test_quoted_refusal_like_text_in_image_description_is_not_a_refusal(self):
        text = PHOTO_DESCRIPTION.replace(
            "A mountain at sunrise.",
            'A screenshot of a post whose caption reads "I can\'t help laughing at my cat."',
        )
        self.post.return_value = local_response(text)
        item, _ = self.describe()
        self.assertEqual(self.cache["photo"]["description"], text)
        self.assertNotIn("blocked by", item["photo_descriptions"][0])
        self.assertEqual(self.state.gemini_requests, 0)

    def test_policy_refusal_variants_never_fall_back_to_gemini(self):
        texts = (
            "Sorry, I cannot comply with this request due to content policy.",
            "I can\u2019t describe this image due to safety restrictions.",
            "I cannot safely describe this image due to content policy.",
            "Sorry, but I cannot ethically assist with this request.",
            "Description: I cannot describe this image due to content policy.",
            "Description: I cannot describe this image due to content policy.\n"
            "Photograph read:\nPhoto type:\nWatch-outs:",
            "I'm unable to help with this request.",
            PHOTO_DESCRIPTION.replace(
                "A mountain at sunrise.", "I cannot comply with this request due to content policy.",
            ),
        )
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                self.post.reset_mock()
                self.post.return_value = local_response(text)
                item, _ = self.describe(str(index))
                self.assertIn("blocked by Ollama", item["photo_descriptions"][0])
                self.assertEqual(self.state.gemini_requests, 0)
                self.post.assert_called_once()

    def test_explicit_refusal_field_is_respected_even_with_valid_sections(self):
        for index, content in enumerate((PHOTO_DESCRIPTION, None)):
            with self.subTest(content=content):
                self.post.return_value = response(data={
                    "message": {"content": content, "refusal": "Content policy"},
                    "done": True,
                })
                item, _ = self.describe(str(index))
                self.assertIn("blocked by Ollama", item["photo_descriptions"][0])
                self.assertEqual(self.state.gemini_requests, 0)

    def test_recovered_local_error_does_not_mislabel_budget_fallback(self):
        self.state.local_budget = 2
        self.post.side_effect = [
            local_response("invalid"), gemini_response(),
            local_response(), gemini_response(),
        ]
        self.describe("failed-local")
        self.describe("successful-local")
        self.describe("budget-fallback")
        self.assertEqual(self.cache["failed-local"]["fallback_reason"], "ollama_invalid_output")
        self.assertIsNone(self.cache["successful-local"]["fallback_reason"])
        self.assertEqual(self.cache["budget-fallback"]["fallback_reason"], "ollama_budget_exhausted")

    def test_deadline_leaves_uncached_photos_pending_but_reads_cached_records(self):
        self.state.deadline = time.monotonic() - 1
        self.cache["cached"] = media_cache.make_entry(PHOTO_DESCRIPTION, "ollama", "qwen3-vl:4b")
        item, result = self.describe("uncached")
        self.describe("cached")
        self.assertTrue(result[1])
        self.assertEqual(item["photo_descriptions"], [None])
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_gemini_retries_count_against_budget_without_redownloading(self):
        self.state.provider = "gemini"
        self.state.gemini_budget = 2
        self.post.side_effect = [response(429, headers={"Retry-After": "0"}), gemini_response()]
        _, result = self.describe()
        self.assertEqual(result[0], 2)
        self.assertEqual(self.state.gemini_requests, 2)
        self.get.assert_called_once()
        self.assertIsNone(self.cache["photo"]["fallback_reason"])

    def test_gemini_retry_wait_cannot_exceed_run_deadline(self):
        self.post.return_value = response(503)
        with patch.object(vision.time, "monotonic", return_value=90):
            with self.assertRaises(vision.VisionDeadlineExceeded) as caught:
                vision.describe_image("https://example.com/photo.jpg", deadline=100)
        self.assertEqual(caught.exception.gemini_request_attempts, 1)
        self.sleep.assert_not_called()

    def test_mixed_cache_preserves_legacy_and_heals_invalid_structured_entries(self):
        self.cache.update({
            "legacy": "A mountain.",
            "old-bug": {"description": vision.NO_DESCRIPTION_TEXT, "provider": "gemini"},
        })
        self.post.return_value = local_response()
        self.describe("legacy")
        self.describe("old-bug")
        self.assertEqual(self.cache["legacy"], "A mountain.")
        self.assertEqual(self.cache["old-bug"]["provider"], "ollama")
        self.post.assert_called_once()

    def test_description_is_saved_before_gemini_throttle(self):
        self.state.provider = "gemini"
        self.post.return_value = gemini_response()
        progress = Mock()
        self.sleep.side_effect = lambda _: self.assertTrue(progress.called)
        vision.describe_images(
            [photo()], self.cache, 10, state=self.state, on_progress=progress,
        )
        self.assertEqual(self.cache["photo"]["provider"], "gemini")
        progress.assert_called_once()

    def test_gemini_malformed_shape_is_uncached_and_explicit_block_is_attributed(self):
        self.state.provider = "gemini"
        self.post.side_effect = [
            response(data={"candidates": None}),
            response(data={"promptFeedback": {"blockReason": "SAFETY"}}),
        ]
        item, _ = self.describe("malformed")
        self.assertNotIn("malformed", self.cache)
        self.assertIn(vision.DESCRIPTION_UNAVAILABLE_PREFIX, item["photo_descriptions"][0])
        self.describe("blocked")
        self.assertEqual(self.cache["blocked"]["provider"], "gemini")
        self.assertIn("blocked by Gemini", self.cache["blocked"]["description"])

    def test_both_provider_breakers_leave_remaining_photos_retryable(self):
        self.post.side_effect = lambda url, **_: (
            local_response("invalid") if url.endswith("/api/chat") else response(403)
        )
        for index in range(7):
            self.describe(str(index))
        self.assertEqual(self.state.local_requests, 3)
        self.assertEqual(self.state.gemini_requests, 5)
        self.assertTrue(self.state.stopped())
        self.assertEqual(self.cache, {})


class LocalTransportTests(unittest.TestCase):
    def test_local_transport_bypasses_proxies_and_disables_thinking_and_redirects(self):
        with patch.dict(os.environ, {
            "HTTP_PROXY": "http://proxy.example.invalid:8080",
            "HTTPS_PROXY": "http://proxy.example.invalid:8080",
            "ALL_PROXY": "http://proxy.example.invalid:8080",
            "NO_PROXY": "",
        }), patch.object(vision.requests.Session, "send", return_value=local_response()) as send:
            text = vision.describe_local(("image/jpeg", "cGhvdG8="), time.monotonic() + 10)
        self.assertEqual(text, PHOTO_DESCRIPTION)
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertFalse(send.call_args.kwargs["allow_redirects"])


class VisionConfigTests(unittest.TestCase):
    def test_provider_modes_enforce_only_required_keys(self):
        with patch.object(vision.config, "GEMINI_API_KEY", ""):
            for mode in ("ollama", "auto"):
                with patch.object(vision.config, "VISION_PROVIDER", mode):
                    vision.config.validate_vision()
            with patch.object(vision.config, "VISION_PROVIDER", "gemini"):
                with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                    vision.config.validate_vision()
        with patch.object(vision.config, "VISION_PROVIDER", "typo"):
            with self.assertRaises(ValueError):
                vision.config.validate_vision()


if __name__ == "__main__":
    unittest.main()
import os
