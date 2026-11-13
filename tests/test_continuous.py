import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src import continuous, threads_auth, threads_client


def response(status, code=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = json.dumps({"error": {"code": code}} if code is not None else {"data": []}).encode()
    return result


class CycleContractTests(unittest.TestCase):
    def test_meta_codes_auth_precedence_and_unknown_fail_closed(self):
        for status, code, expected in (
            (400, 4, 75), (403, 17, 75), (400, 613, 75),
            (500, 190, 78), (400, 102, 78), (401, None, 78),
            (403, None, 78), (503, None, 75), (429, None, 75),
            (400, 32, 1), (400, 999, 1),
        ):
            with self.subTest(status=status, code=code):
                with self.assertRaises(continuous.CycleFailure) as raised:
                    continuous.check_threads_response(response(status, code))
                self.assertEqual(raised.exception.code, expected)

    def test_retry_after_seconds_dates_and_invalid_values(self):
        with patch.object(continuous.time, "time", return_value=1000):
            self.assertEqual(continuous.retry_after({"Retry-After": "120"}), 1120)
            self.assertEqual(continuous.retry_after({"Retry-After": "Thu, 01 Jan 1970 00:20:00 GMT"}), 1200)
            for value in ("nan", "inf", "-10", "bad", None):
                self.assertIsNone(continuous.retry_after({"Retry-After": value}))

    def test_status_is_cycle_bound_and_requires_real_booleans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            record = {
                "version": 1, "cycle_id": "current", "code": 0,
                "live_changed": False, "eligible_unattempted": False,
            }
            continuous.atomic_json(path, record)
            self.assertFalse(continuous.read_status(0, "current", path)["live_changed"])
            for changes in (
                {"cycle_id": "stale"}, {"code": 75}, {"live_changed": 0},
                {"eligible_unattempted": None},
            ):
                continuous.atomic_json(path, {**record, **changes})
                with self.assertRaises(ValueError):
                    continuous.read_status(0, "current", path)
            path.write_text('{"version":1,"cycle_id":"current","code":75,"retry_at":NaN}')
            with self.assertRaises(ValueError):
                continuous.read_status(75, "current", path)

    def test_live_hashes_ignore_history_and_distinguish_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = continuous.live_hashes(root)
            (root / "data" / "history").mkdir(parents=True)
            (root / "data" / "history" / "snapshot.md").write_text("new date")
            self.assertEqual(continuous.live_hashes(root), baseline)
            (root / "data" / "needs-like.md").write_bytes(b"")
            self.assertNotEqual(continuous.live_hashes(root), baseline)
            with patch.object(Path, "read_bytes", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    continuous.live_hashes(root)

    @patch.dict(os.environ, {"CONTINUOUS_CYCLE_ID": "test"})
    def test_collection_and_refresh_classification_and_schema_validation(self):
        with patch.object(threads_auth, "get_valid_access_token", return_value="private"):
            with patch.object(requests, "get", side_effect=requests.Timeout):
                with self.assertRaises(continuous.CycleFailure) as raised:
                    threads_client._get("me/threads", {})
                self.assertEqual(raised.exception.code, 75)
            invalid = response(200)
            invalid._content = b"{}"
            with patch.object(requests, "get", return_value=invalid):
                with self.assertRaises(continuous.CycleFailure):
                    threads_client._get("me/threads", {})
        for result, expected in ((response(400, 190), 78), (response(503), 75), (response(200), 1)):
            with patch.object(requests, "get", return_value=result):
                with self.assertRaises(continuous.CycleFailure) as raised:
                    threads_auth._refresh("private")
                self.assertEqual(raised.exception.code, expected)
        with patch.object(requests, "get", side_effect=requests.ConnectionError):
            with self.assertRaises(continuous.CycleFailure) as raised:
                threads_auth._refresh("private")
            self.assertEqual(raised.exception.code, 75)

    def test_service_bound_rejects_invalid_or_excessive_overrides(self):
        with patch.dict(os.environ, {"GIT_TIMEOUT_SECONDS": "120", "PIPELINE_TIMEOUT_SECONDS": "3000"}):
            self.assertEqual(continuous.validate_limits(), 5685)
        for value in ("nan", "inf", "0", "-1", "10000"):
            with patch.dict(os.environ, {"GIT_TIMEOUT_SECONDS": value}):
                with self.assertRaises(ValueError):
                    continuous.validate_limits()


class AdaptiveTests(unittest.TestCase):
    def test_promotions_changes_and_eligible_backlog(self):
        cadence = continuous.Cadence()
        quiet = {"live_changed": False, "eligible_unattempted": False}
        self.assertEqual([cadence.succeeded(quiet) for _ in range(5)], [300, 900, 900, 1800, 1800])
        self.assertEqual(cadence.succeeded({**quiet, "eligible_unattempted": True}), 300)
        self.assertEqual(cadence.quiet, 0)
        cadence.succeeded(quiet)
        self.assertEqual(cadence.succeeded({**quiet, "live_changed": True}), 300)
        self.assertEqual(cadence.quiet, 0)

    def test_deadlines_skip_slots_without_catch_up(self):
        for start, interval, now, expected in (
            (0, 300, 120, 300), (0, 300, 420, 600),
            (0, 300, 300, 300), (0, 300, 901, 1200),
            (420, 900, 500, 1320),
        ):
            self.assertEqual(continuous.next_due(start, interval, now), expected)

    def run_cycles(self, codes, *, durations=None, hints=None, changed=False):
        self.clock = 0
        waits = []
        remaining = list(codes)
        durations = iter(durations or [0] * len(codes))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def launch(_, **kwargs):
                code = remaining.pop(0)
                self.clock += next(durations)
                record = {
                    "version": 1, "cycle_id": kwargs["env"]["CONTINUOUS_CYCLE_ID"],
                    "code": code, "live_changed": changed, "eligible_unattempted": False,
                    "retry_at": hints,
                }
                continuous.atomic_json(root / "cache" / "continuous-cycle.json", record)
                return Mock(wait=Mock(return_value=code))

            def wait(delay):
                waits.append(delay)
                self.clock += delay
                return not remaining

            with patch.object(continuous.time, "time", return_value=1000):
                code = continuous.supervise(root, clock=lambda: self.clock, waiter=wait, launch=launch)
        return code, waits

    def test_retries_freeze_quiet_counter_and_resume_from_actual_start(self):
        code, waits = self.run_cycles([0, 75, 0, 0])
        self.assertEqual(code, 0)
        self.assertEqual(waits, [300, 30, 900, 900])
        code, waits = self.run_cycles([75] * 6)
        self.assertEqual(code, 75)
        self.assertEqual(waits, [30, 60, 120, 240, 480])

    def test_overruns_and_wall_clock_changes_do_not_affect_cadence(self):
        code, waits = self.run_cycles([0], durations=[420], changed=True)
        self.assertEqual((code, waits), (0, [180]))
        code, waits = self.run_cycles([75], hints=1200)
        self.assertEqual((code, waits), (0, [200]))
        code, waits = self.run_cycles([75], hints=5000)
        self.assertEqual((code, waits), (75, []))

    def test_fatal_codes_do_not_retry(self):
        for code in (70, 73, 78, 124, 1):
            self.assertEqual(self.run_cycles([code]), (code, []))

    def test_graceful_handler_waits_for_real_child_and_launches_no_next_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "finished"
            children = []

            def launch(_, **kwargs):
                child = subprocess.Popen([
                    sys.executable, "-c",
                    "import pathlib,time,sys; time.sleep(.2); pathlib.Path(sys.argv[1]).write_text('done')",
                    str(output),
                ], close_fds=True)
                children.append(child)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return child

            self.assertEqual(continuous.supervise(root, launch=launch), 0)
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].poll(), 0)
            self.assertEqual(output.read_text(), "done")


if __name__ == "__main__":
    unittest.main()
