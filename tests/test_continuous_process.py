"""Bounded Linux-only checks of actual signals, descendants, and flock."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "posix" and shutil.which("flock"), "Linux/POSIX flock is required")
class ContinuousProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        (self.root / "src").mkdir()
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").symlink_to(sys.executable)
        for relative in ("src/continuous.py", "run_continuous.sh"):
            (self.root / relative).write_bytes((ROOT / relative).read_bytes())
        (self.root / "run_daily.sh").write_text(
            "#!/usr/bin/env bash\nset -e\nexec 9>cache/pipeline.lock\n"
            "flock -n 9 || exit 73\nexec .venv/bin/python worker.py\n"
        )
        (self.root / "worker.py").write_text(
            "import json,os,pathlib,subprocess,sys,time\n"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.3)'])\n"
            "pathlib.Path('working').write_text(json.dumps([os.getpid(),p.pid]))\n"
            "p.wait()\n"
            "pathlib.Path('finished').write_text('published')\n"
            "pathlib.Path('cache/continuous-cycle.json').write_text(json.dumps({"
            "'version':1,'cycle_id':os.environ['CONTINUOUS_CYCLE_ID'],'code':0,"
            "'live_changed':False,'eligible_unattempted':False}))\n"
        )
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)

    def launch(self):
        child = subprocess.Popen(
            ["bash", "run_continuous.sh"], cwd=self.root,
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        deadline = time.monotonic() + 5
        while not (self.root / "working").exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                self.fail("Supervisor did not launch its bounded cycle")
            time.sleep(.01)
        return child

    def test_sigterm_finishes_child_and_releases_both_locks(self):
        child = self.launch()
        pids = json.loads((self.root / "working").read_text())
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual((self.root / "finished").read_text(), "published")
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        for filename in ("continuous.lock", "pipeline.lock"):
            self.assertEqual(subprocess.run(
                ["flock", "-n", str(self.root / "cache" / filename), "true"],
                check=False,
            ).returncode, 0)

    def test_duplicate_supervisor_stops_without_launching_another_cycle(self):
        child = self.launch()
        other = subprocess.run(
            ["bash", "run_continuous.sh"], cwd=self.root,
            capture_output=True, timeout=5,
        )
        self.assertEqual(other.returncode, 73)
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), 0)

    def test_stop_during_adaptive_wait_is_prompt_and_does_not_launch_again(self):
        child = self.launch()
        deadline = time.monotonic() + 5
        while not (self.root / "finished").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        time.sleep(.05)
        status = (self.root / "cache" / "continuous-cycle.json").read_bytes()
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual((self.root / "cache" / "continuous-cycle.json").read_bytes(), status)
