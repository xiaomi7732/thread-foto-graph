import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import outward


NOW = dt.datetime(2026, 11, 2, 7, tzinfo=dt.timezone.utc)


def comment(media_id="our-comment", **extra):
    return {
        "id": media_id,
        "username": "owner",
        "text": "What caught your eye?",
        "timestamp": "2026-11-01T10:00:00Z",
        "permalink": f"https://www.threads.com/@owner/post/{media_id}",
        **extra,
    }


def answer(media_id="answer", **extra):
    return {
        "id": media_id,
        "username": "photographer",
        "text": "The light was amazing.",
        "timestamp": "2026-11-01T11:00:00Z",
        "permalink": f"https://www.threads.com/@photographer/post/{media_id}",
        "has_replies": False,
        "replied_to": {"id": "our-comment"},
        "replies": [],
        **extra,
    }


def registry_entry(media_id="our-comment", **extra):
    return {
        "reply_id": media_id,
        "target_author": "photographer",
        "target_post_url": None,
        "target_post_id": None,
        "style": "question",
        "posted_on": "2026-11-01",
        "source": "daily-batch",
        "status": "posted",
        **extra,
    }


class RegistryTests(unittest.TestCase):
    def test_load_registry_validates_and_preserves_metadata(self):
        entry = registry_entry("123", style="closer")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps({
                "schema_version": 1, "account": "owner", "entries": [entry],
            }), encoding="utf-8")
            self.assertEqual(outward.load_registry(path, "@OWNER"), [entry])
            with self.assertRaisesRegex(outward.RunAborted, "INVALID_REGISTRY"):
                outward.load_registry(path, "someone_else")

    def test_invalid_registry_never_silently_falls_back(self):
        entry = registry_entry("123")
        invalid_entries = [
            None,
            {},
            {**entry, "reply_id": None},
            {**entry, "reply_id": 123},
            {**entry, "reply_id": "123/insights"},
            {**entry, "target_author": []},
            {**entry, "target_author": "@"},
            {**entry, "posted_on": "2026-02-31"},
            {**entry, "posted_on": "20261101"},
            {**entry, "posted_on": None},
            {**entry, "style": []},
            {**entry, "style": "unknown"},
            {**entry, "status": None},
            {**entry, "target_post_url": {}},
        ]
        documents = [
            None, [], {}, {"entries": []},
            {"schema_version": 2, "account": "owner", "entries": []},
            {"schema_version": 1, "account": None, "entries": []},
            {"schema_version": 1, "account": "owner", "entries": {}},
            {"schema_version": 1, "account": "owner", "entries": [entry, entry]},
        ] + [
            {"schema_version": 1, "account": "owner", "entries": [bad]}
            for bad in invalid_entries
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            for document in documents:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(outward.RunAborted, "INVALID_REGISTRY"):
                        outward.load_registry(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(outward.RunAborted, "INVALID_REGISTRY"):
                outward.load_registry(path)

    def test_registry_window_uses_noon_utc_and_only_posted_entries(self):
        cutoff = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)
        candidates = outward._registry_candidates([
            registry_entry("old", posted_on="2026-10-31"),
            registry_entry("boundary"),
            registry_entry("newer", posted_on="2026-11-02"),
            registry_entry("failed", status="failed"),
        ], cutoff)
        self.assertEqual([row["id"] for row in candidates], ["newer", "boundary"])
        self.assertEqual(candidates[1]["timestamp"], cutoff.isoformat())
        self.assertIsNone(candidates[1]["permalink"])
        self.assertEqual(candidates[1]["root_post"]["username"], "photographer")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = outward.Client("test-token")
        self.addCleanup(self.client.session.close)

    def test_individual_expansion_paginates_without_following_next_url(self):
        first = answer("one")
        second = answer("two")
        for item in (first, second):
            item.pop("replies")
        self.client.get = Mock(side_effect=[
            {
                **comment(),
                "has_replies": True,
                "replies": {
                    "data": [first],
                    "paging": {"next": "https://untrusted.invalid/?access_token=secret", "cursors": {"after": "cursor"}},
                },
            },
            {"id": "our-comment", "has_replies": True, "replies": {"data": [second]}},
        ])
        _, answers = self.client.answers("our-comment")
        self.assertEqual([a["id"] for a in answers], ["one", "two"])
        self.assertEqual(answers[0]["replies"], [])
        self.assertEqual(self.client.get.call_args_list[0].args[0], "our-comment")
        self.assertIn("replies.limit(100)", self.client.get.call_args_list[0].args[1]["fields"])
        self.assertEqual(self.client.get.call_args_list[1].args[0], "our-comment")
        self.assertIn("replies.after(cursor)", self.client.get.call_args_list[1].args[1]["fields"])

    def test_unavailable_or_inconsistent_edges_never_mean_no_answer(self):
        for node in ({}, {"has_replies": True}, {"has_replies": True, "replies": {"data": []}}):
            with self.subTest(node=node), self.assertRaises(outward.ObservationError):
                self.client.edge("id", node, "id")
        self.assertEqual(self.client.edge("id", {"has_replies": False}, "id"), [])

    def test_child_parent_is_taken_from_edge_when_field_omitted(self):
        direct = answer(has_replies=True, replies={
            "data": [{"id": "child", "username": "owner", "has_replies": False}],
        })
        self.client.get = Mock(return_value={
            **comment(), "has_replies": True, "replies": {"data": [direct]},
        })
        _, replies = self.client.answers("our-comment")
        self.assertEqual(replies[0]["replies"][0]["username"], "owner")

    def test_incomplete_child_authors_fail_before_deriving_actions(self):
        for metadata in ({}, {"username": None}, {"username": ""}, {"username": " "}, {"username": 7}):
            with self.subTest(metadata=metadata):
                direct = answer(has_replies=True, replies={
                    "data": [{"id": "child", **metadata}],
                })
                self.client.get = Mock(return_value={
                    **comment(), "has_replies": True, "replies": {"data": [direct]},
                })
                with self.assertRaisesRegex(outward.ObservationError, "CHILD_AUTHOR_UNAVAILABLE"):
                    self.client.answers("our-comment")

    def test_parent_mismatch_and_pagination_loops_fail(self):
        self.client.get = Mock(return_value={
            **comment(), "has_replies": True,
            "replies": {"data": [answer(replied_to={"id": "wrong"})]},
        })
        with self.assertRaisesRegex(outward.ObservationError, "PARENT"):
            self.client.answers("our-comment")
        node = {"has_replies": True, "replies": {
            "data": [answer()],
            "paging": {"next": "next", "cursors": {"after": "repeated"}},
        }}
        self.client.get.return_value = node
        with self.assertRaisesRegex(outward.ObservationError, "PAGINATION"):
            self.client.edge("our-comment", node, "id")

    @patch.object(outward.time, "sleep")
    @patch.object(outward.random, "uniform", return_value=0.75)
    def test_request_uses_short_politeness_delay(self, uniform, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {"id": "our-comment"}
        with patch.object(self.client.session, "get", return_value=response):
            self.assertEqual(self.client.get("me", {}), {"id": "our-comment"})
        uniform.assert_called_once_with(0.5, 1)
        sleep.assert_called_once_with(0.75)

    @patch.object(outward.time, "sleep")
    def test_rate_limit_auth_and_access_errors_abort_without_retry(self, sleep):
        for status, code, message in (
            (429, 4, "RATE_LIMITED"),
            (400, 190, "SESSION_EXPIRED"),
            (400, 10, "INSUFFICIENT_ACCESS"),
        ):
            response = Mock(status_code=status)
            response.json.return_value = {"error": {"code": code, "message": "secret-value"}}
            with self.subTest(code=code), patch.object(
                self.client.session, "get", return_value=response
            ) as get, self.assertRaisesRegex(outward.RunAborted, message):
                self.client.get("me", {})
            get.assert_called_once()

    @patch.object(outward.time, "sleep")
    def test_transient_errors_have_bounded_retries(self, sleep):
        response = Mock(status_code=503)
        response.json.return_value = {"error": {"code": 2}}
        with patch.object(self.client.session, "get", return_value=response) as get:
            with self.assertRaisesRegex(outward.ObservationError, "HTTP_503"):
                self.client.get("id", {})
        self.assertEqual(get.call_count, 4)

    def test_discovery_pages_and_excludes_own_roots_even_when_root_is_old(self):
        cutoff = NOW - dt.timedelta(days=2)
        self.client.get = Mock(side_effect=[
            {"data": [
                comment("outward"),
                comment("own", root_post={"id": "old-root"}),
            ], "paging": {"next": "next", "cursors": {"after": "after-one"}}},
            {"id": "old-root", "username": "Owner"},
            {"data": [
                comment("too-old", timestamp="2026-10-01T10:00:00Z"),
                comment("second"),
            ]},
        ])
        discovered = self.client.discover("owner", cutoff, NOW)
        self.assertEqual([r["id"] for r in discovered], ["outward", "second"])
        self.assertEqual(self.client.get.call_args_list[2].args[1]["after"], "after-one")

    def test_failed_root_lookup_keeps_candidates_and_is_cached(self):
        self.client.get = Mock(side_effect=[
            {"data": [
                comment("first", root_post={"id": "unavailable-root"}),
                comment("second", root_post={"id": "unavailable-root"}),
            ]},
            outward.ObservationError("API_ERROR_HTTP_400_CODE_100"),
        ])
        rows = self.client.discover("owner", NOW - dt.timedelta(days=2), NOW)
        self.assertEqual([row["id"] for row in rows], ["first", "second"])
        self.assertEqual(rows[0]["root_post"]["lookup_error"], "API_ERROR_HTTP_400_CODE_100")
        self.assertEqual(self.client.get.call_count, 2)

    def test_root_lookup_still_propagates_global_abort(self):
        self.client.get = Mock(side_effect=[
            {"data": [comment(root_post={"id": "root"})]},
            outward.RunAborted("RATE_LIMITED"),
        ])
        with self.assertRaisesRegex(outward.RunAborted, "RATE_LIMITED"):
            self.client.discover("owner", NOW - dt.timedelta(days=2), NOW)


class OutwardTestCase(unittest.TestCase):
    def client(self, answers=None, candidates=None, likes=3):
        client = Mock()
        client.get.side_effect = lambda path, params: (
            {"id": "account", "username": "owner"} if path == "me"
            else {"data": [{"name": "likes", "values": [{"value": likes}]}]}
        )
        client.discover.return_value = candidates if candidates is not None else [comment()]
        client.answers.return_value = (comment(), answers or [])
        return client

    def collect(self, client, previous=None, limit=40):
        return outward.collect(client, "owner", previous or {}, NOW, 14, limit)


class CollectionTests(OutwardTestCase):
    def test_registry_mode_tracks_registry_and_uses_discovery_only_for_audit(self):
        client = self.client([answer()], candidates=[comment("stray")])
        client.answers.return_value = (comment(root_post={
            "username": "not-the-target",
            "permalink": "https://www.threads.com/@photographer/post/root",
        }), [answer()])
        snapshot = outward.collect(
            client, "owner", {}, NOW, 14, 40, registry=[registry_entry()],
        )
        self.assertEqual(snapshot["mode"], "registry")
        self.assertEqual(snapshot["registry_count"], 1)
        self.assertEqual([r["reply_id"] for r in snapshot["replies"]], ["our-comment"])
        client.answers.assert_called_once_with("our-comment")
        item = snapshot["replies"][0]
        self.assertTrue(item["from_registry"])
        self.assertEqual(item["style"], "question")
        self.assertEqual(item["source"], "daily-batch")
        self.assertEqual(item["target_author"], "photographer")
        self.assertEqual(item["state"], "answered_by_author")
        self.assertEqual(item["posted_at"], comment()["timestamp"])
        self.assertEqual(item["reply_permalink"], comment()["permalink"])
        self.assertEqual(item["target_post_url"], "https://www.threads.com/@photographer/post/root")
        self.assertEqual(snapshot["audit"], {
            "discovered_not_registered": ["stray"],
            "registered_missing_from_discovery": ["our-comment"],
        })
        markdown = outward.render(snapshot)
        self.assertIn("1 of 1 registered replies checked", markdown)
        self.assertIn("discovery is audit-only", markdown)
        self.assertIn("Target: @photographer; style: question", markdown)

    def test_registry_metadata_wins_over_node_metadata(self):
        client = self.client([answer()])
        client.answers.return_value = (comment(root_post={
            "username": "wrong", "permalink": "https://www.threads.com/@wrong/post/wrong",
        }), [answer()])
        entry = registry_entry(
            style="closer", target_post_url="https://www.threads.com/@photographer/post/canonical",
        )
        first = outward.collect(client, "owner", {}, NOW, 14, 40, registry=[entry])
        item = first["replies"][0]
        self.assertEqual(item["target_post_url"], entry["target_post_url"])
        self.assertEqual(item["style"], "closer")
        self.assertTrue(item["answers"][0]["is_target_author"])
        second = outward.collect(client, "owner", first, NOW, 14, 40, registry=[entry])
        self.assertFalse(second["replies"][0]["answers"][0]["is_new"])

    def test_registry_cap_and_audit_use_whole_registered_window(self):
        client = self.client(candidates=[comment("one")])
        snapshot = outward.collect(
            client, "owner", {}, NOW, 14, 2,
            registry=[registry_entry("one"), registry_entry("two"), registry_entry("three")],
        )
        self.assertTrue(snapshot["capped"])
        self.assertEqual(snapshot["registry_count"], 3)
        self.assertEqual(len(snapshot["replies"]), 2)
        self.assertEqual(snapshot["audit"]["registered_missing_from_discovery"], ["three", "two"])
        client = self.client(candidates=[comment("one"), comment("two"), comment("three")])
        snapshot = outward.collect(client, "owner", {}, NOW, 14, 2, registry=[registry_entry("one")])
        self.assertFalse(snapshot["capped"])
        self.assertEqual(snapshot["registry_count"], 1)

    def test_absent_registry_preserves_discovery_output_shape(self):
        snapshot = self.collect(self.client([answer()]))
        self.assertEqual(set(snapshot), {
            "schema_version", "generated_at", "run_id", "account", "status",
            "lookback_days", "max_replies", "discovered_count", "capped",
            "seen_answer_ids", "replies",
        })
        self.assertFalse({"style", "source", "from_registry"} & snapshot["replies"][0].keys())

    def test_empty_registry_is_authoritative_not_discovery_fallback(self):
        client = self.client(candidates=[comment()])
        snapshot = outward.collect(client, "owner", {}, NOW, 14, 40, registry=[])
        self.assertEqual(snapshot["mode"], "registry")
        self.assertEqual(snapshot["registry_count"], 0)
        self.assertEqual(snapshot["replies"], [])
        self.assertFalse(snapshot["capped"])
        client.answers.assert_not_called()

    def test_registry_failed_read_stays_unreachable_with_author_metadata(self):
        client = self.client()
        client.answers.side_effect = [
            outward.ObservationError("API_ERROR_HTTP_400_CODE_100"),
            (comment("healthy"), []),
        ]
        snapshot = outward.collect(
            client, "owner", {}, NOW, 14, 40,
            registry=[registry_entry(), registry_entry("healthy")],
        )
        failed = snapshot["replies"][0]
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(failed["state"], "unreachable")
        self.assertIsNone(failed["answer_count"])
        self.assertEqual(failed["target_author"], "photographer")
        self.assertTrue(failed["from_registry"])

    def test_new_answers_repeat_quietly_and_like_deltas_are_observed(self):
        client = self.client([answer()])
        first = self.collect(client)
        self.assertTrue(first["replies"][0]["answers"][0]["is_new"])
        self.assertIsNone(first["replies"][0]["like_delta"])
        second = self.collect(client, first)
        item = second["replies"][0]
        self.assertFalse(item["answers"][0]["is_new"])
        self.assertEqual(item["like_delta"], 0)
        self.assertFalse(item["likes_is_new"])
        third = self.collect(self.client([answer()], likes=5), second)
        self.assertEqual(third["replies"][0]["like_delta"], 2)
        self.assertTrue(third["replies"][0]["likes_is_new"])
        self.assertIsNone(item["answers"][0]["is_target_author"])
        self.assertEqual(item["state"], "answered")

    def test_actions_match_needs_reply_and_resolve_on_our_direct_response(self):
        answers = [
            answer("question", text="Where?"),
            answer("thanks", text="Thank you so much!"),
            answer("liked", text="Thank you", is_liked=True),
            answer("responded", replies=[{"id": "child", "username": "OWNER"}]),
            answer("photo", text="", media_type="IMAGE"),
            answer("hidden", hide_status="HIDDEN"),
            answer("ours", username="owner"),
        ]
        result = self.collect(self.client(answers))["replies"][0]
        self.assertEqual(
            [a["action"] for a in result["answers"]],
            ["reply", "like", None, None, "reply", None, None],
        )
        self.assertEqual(result["answer_count"], 6)
        self.assertFalse(result["answers"][-1]["is_new"])

    def test_target_author_precedence_and_scope_cap(self):
        candidates = [
            comment(root_post={"username": "photographer", "permalink": "https://www.threads.com/@photographer/post/root"}),
            comment("older"),
        ]
        snapshot = self.collect(self.client([answer()], candidates), limit=1)
        self.assertTrue(snapshot["capped"])
        self.assertEqual(snapshot["discovered_count"], 2)
        self.assertEqual(snapshot["replies"][0]["state"], "answered_by_author")
        self.assertTrue(snapshot["replies"][0]["answers"][0]["is_target_author"])

    def test_unreachable_is_not_no_answer_and_recovery_does_not_repeat_new(self):
        previous = self.collect(self.client([answer()]))
        client = self.client(candidates=[comment(), comment("healthy")])
        client.answers.side_effect = [
            outward.ObservationError("API_ERROR_HTTP_400_CODE_100"),
            (comment("healthy"), []),
        ]
        partial = self.collect(client, previous)
        failed = partial["replies"][0]
        self.assertEqual(failed["state"], "unreachable")
        self.assertIsNone(failed["answer_count"])
        self.assertEqual(failed["last_successful"]["like_count"], 3)
        self.assertEqual(partial["status"], "partial")
        recovered = self.collect(self.client([answer()]), partial)
        self.assertFalse(recovered["replies"][0]["answers"][0]["is_new"])
        self.assertEqual(recovered["replies"][0]["like_delta"], 0)

    def test_unknown_likes_do_not_turn_into_zero(self):
        client = self.client([answer()])
        client.get.side_effect = [
            {"username": "owner"},
            outward.ObservationError("INVALID_INSIGHTS"),
        ]
        snapshot = self.collect(client)
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["replies"][0]["answer_count"], 1)
        self.assertIsNone(snapshot["replies"][0]["like_count"])

    def test_failed_root_metadata_is_partial_without_losing_answers(self):
        client = self.client(
            [answer()],
            [comment(root_post={"id": "root", "lookup_error": "API_ERROR_HTTP_400_CODE_100"})],
        )
        snapshot = self.collect(client)
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["replies"][0]["state"], "answered")
        self.assertEqual(snapshot["replies"][0]["answer_count"], 1)
        self.assertIsNone(snapshot["replies"][0]["target_author"])
        self.assertIn("Target metadata unavailable", outward.render(snapshot))

    def test_account_mismatch_and_all_unreachable_abort(self):
        client = self.client()
        client.get.side_effect = None
        client.get.return_value = {"username": "someone_else"}
        with self.assertRaisesRegex(outward.RunAborted, "ACCOUNT_MISMATCH"):
            self.collect(client)
        client = self.client()
        client.answers.side_effect = outward.ObservationError("UNAVAILABLE")
        with self.assertRaisesRegex(outward.RunAborted, "ALL_ITEMS_UNREACHABLE"):
            self.collect(client)

    def test_rate_limit_during_item_does_not_return_partial_snapshot(self):
        client = self.client()
        client.answers.side_effect = outward.RunAborted("RATE_LIMITED")
        with self.assertRaisesRegex(outward.RunAborted, "RATE_LIMITED"):
            self.collect(client)


class OutputTests(OutwardTestCase):
    def test_main_loads_registry_next_to_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "outward-registry.json").write_text(json.dumps({
                "schema_version": 1, "account": "owner", "entries": [registry_entry("123")],
            }), encoding="utf-8")
            client = self.client(candidates=[comment("stray")])
            client.answers.return_value = (comment("123"), [])
            with patch.object(outward.config, "THREADS_USERNAME", "owner"), patch.object(
                outward.threads_auth, "get_valid_access_token", return_value="token"
            ), patch.object(outward, "Client", return_value=client):
                self.assertEqual(outward.main(["--output-dir", str(path)]), 0)
            snapshot = json.loads((path / "needs-outward.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["mode"], "registry")
            self.assertEqual([r["reply_id"] for r in snapshot["replies"]], ["123"])

    def test_invalid_registry_preserves_feed_before_any_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            previous = self.collect(self.client([answer()]))
            outward.write_outputs(previous, path)
            (path / "outward-registry.json").write_text("{", encoding="utf-8")
            before = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
            with patch.object(outward.config, "THREADS_USERNAME", "owner"), patch.object(
                outward, "Client"
            ) as client:
                self.assertEqual(outward.main(["--output-dir", str(path)]), 1)
            client.assert_not_called()
            self.assertEqual({p: p.read_bytes() for p in path.rglob("*") if p.is_file()}, before)

    def test_markdown_context_action_sections_and_timestamped_history(self):
        snapshot = self.collect(self.client([answer(text="<script>Not trusted</script> [link](bad)")]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            outward.write_outputs(snapshot, path)
            markdown = (path / "needs-outward.md").read_text(encoding="utf-8")
            self.assertIn("## New Answers (1)", markdown)
            self.assertIn("## Needs Reply (1)", markdown)
            self.assertIn("Our comment: What caught your eye?", markdown)
            self.assertNotIn("<script>", markdown)
            self.assertIn("&lt;script&gt;", markdown)
            self.assertIn("\\[link\\]", markdown)
            self.assertEqual(json.loads((path / "needs-outward.json").read_text()), snapshot)
            archives = list((path / "history").glob("needs-outward-*.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text()), snapshot)
            self.assertFalse(list(path.rglob("*.tmp")))

    def test_failed_api_run_leaves_previous_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            previous = self.collect(self.client([answer()]))
            outward.write_outputs(previous, path)
            before = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
            client = self.client()
            client.get.side_effect = outward.RunAborted("SESSION_EXPIRED")
            with patch.object(outward.config, "THREADS_USERNAME", "owner"), patch.object(
                outward.threads_auth, "get_valid_access_token", return_value="token"
            ), patch.object(outward, "Client", return_value=client):
                self.assertEqual(outward.main(["--output-dir", str(path)]), 1)
            self.assertEqual({p: p.read_bytes() for p in path.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
