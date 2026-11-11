import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
if not BASH and os.name == "nt" and shutil.which("git"):
    candidate = Path(shutil.which("git")).resolve().parents[1] / "bin" / "bash.exe"
    if candidate.exists():
        BASH = str(candidate)


@unittest.skipUnless(BASH and shutil.which("git"), "Bash and Git are required")
class LinuxRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.repo = self.root / "work"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self.git("init", "--initial-branch=main")
        self.identity(self.repo)
        self.git("init", "--bare", str(self.remote))
        self.write("run_daily.sh", (ROOT / "run_daily.sh").read_text(encoding="utf-8"))
        self.write(".gitignore", "cache/\n.venv/\ntest-bin/\n")
        self.write(".gitattributes", "*.sh text eol=lf\n")
        for path in (
            "data/needs-reply.md", "data/needs-like.md", "data/actions.md",
            "data/topics.md", "data/threads/.gitkeep", "data/history/.gitkeep",
            "docs/graph.json",
        ):
            self.write(path, "")
        self.git("add", ".")
        self.git("commit", "-m", "Fixture baseline")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", "main")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("test-bin/flock", '#!/usr/bin/env bash\nexit "${TEST_LOCK_FAILURE:-0}"\n')
        self.write("test-bin/timeout", '#!/usr/bin/env bash\nshift 3\nexec "$@"\n')
        self.write("test-bin/sleep", "#!/usr/bin/env bash\nexit 0\n")
        self.write(".venv/bin/python", """#!/usr/bin/env bash
printf 'retained' > cache/media_cache.json
if [[ -n "${TEST_PIPELINE_FAIL:-}" ]]; then exit 7; fi
if [[ -n "${TEST_OTHER:-}" ]]; then
  printf 'remote change' > "$TEST_OTHER/data/needs-reply.md"
  git -C "$TEST_OTHER" add data/needs-reply.md
  git -C "$TEST_OTHER" commit -m 'Concurrent output'
  git -C "$TEST_OTHER" push origin main
fi
if [[ -n "${TEST_CHANGE:-}" ]]; then
  printf '%s' "$TEST_CHANGE" > data/needs-reply.md
fi
""")

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)
        return path

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, check=True,
        )

    def identity(self, repo):
        self.git("config", "user.name", "Runner Test", cwd=repo)
        self.git("config", "user.email", "runner@example.invalid", cwd=repo)
        self.git("config", "commit.gpgsign", "false", cwd=repo)

    def run_script(self, *args, **env):
        return subprocess.run(
            [BASH, "-c", 'export PATH="$PWD/test-bin:$PATH"; exec bash ./run_daily.sh "$@"', "--", *args],
            cwd=self.repo, env={**os.environ, **env},
            capture_output=True, text=True, timeout=30,
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_local_run_never_commits_or_pushes(self):
        result = self.run_script(TEST_CHANGE="local output")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.baseline)
        self.assertEqual((self.repo / "data/needs-reply.md").read_text(), "local output")

    def test_pipeline_failure_preserves_cache_without_publishing(self):
        result = self.run_script("--publish", TEST_PIPELINE_FAIL="1")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.baseline)
        self.assertEqual((self.repo / "cache/media_cache.json").read_text(), "retained")

    def test_lock_contention_and_dirty_checkout_stop_before_pipeline(self):
        result = self.run_script("--publish", TEST_LOCK_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "cache/media_cache.json").exists())
        self.write("unreviewed.txt", "do not publish")
        result = self.run_script("--publish")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr)
        self.assertFalse((self.repo / "cache/media_cache.json").exists())

    def test_unchanged_output_creates_no_commit(self):
        result = self.run_script("--publish")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.baseline)

    def test_success_publishes_only_generated_output(self):
        result = self.run_script("--publish", TEST_CHANGE="new output")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines()
        self.assertEqual(changed, ["data/needs-reply.md"])
        self.assertEqual(self.git("rev-parse", "origin/main").stdout.strip(), self.head())
        self.assertTrue((self.repo / "cache/media_cache.json").exists())

    def test_rejected_push_retains_commit_and_refuses_another_pipeline_run(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        result = self.run_script("--publish", TEST_CHANGE="retained output")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.head(), self.baseline)
        self.assertEqual(self.git("rev-list", "--count", "origin/main..HEAD").stdout.strip(), "1")
        self.assertTrue((self.repo / "cache/media_cache.json").exists())
        again = self.run_script("--publish")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("Unpushed commits", again.stderr)

    def test_generated_file_conflict_aborts_rebase_and_retains_our_commit(self):
        other = self.root / "other"
        self.git("clone", "--branch", "main", str(self.remote), str(other))
        self.identity(other)
        result = self.run_script("--publish", TEST_CHANGE="our output", TEST_OTHER=str(other))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Conflicting remote changes", result.stderr)
        self.assertEqual((self.repo / "data/needs-reply.md").read_text(), "our output")
        self.assertFalse((self.repo / ".git/rebase-merge").exists())
        self.assertEqual(self.git("rev-list", "--count", "origin/main..HEAD").stdout.strip(), "1")

    def test_git_transport_timeouts_stop_preflight_or_preserve_unpushed_output(self):
        self.write("test-bin/timeout", '#!/usr/bin/env bash\nexec /usr/bin/timeout "$@"\n')
        self.write("test-bin/git", """#!/usr/bin/env bash
if [[ "$1" == "$TEST_STALLED_GIT_COMMAND" ]]; then
  printf 'started' > cache/git-timeout-test.log
  /usr/bin/sleep 2
fi
exec "$TEST_REAL_GIT" "$@"
""")
        for operation in ("fetch", "push"):
            with self.subTest(operation=operation):
                result = self.run_script(
                    "--publish", GIT_TIMEOUT_SECONDS="0.5",
                    TEST_STALLED_GIT_COMMAND=operation, TEST_REAL_GIT=shutil.which("git"),
                    TEST_CHANGE="bounded transport output",
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((self.repo / "cache/git-timeout-test.log").exists())
                if operation == "fetch":
                    self.assertEqual(result.returncode, 124)
                    self.assertEqual(self.head(), self.baseline)
                    self.assertFalse((self.repo / "cache/media_cache.json").exists())
                else:
                    self.assertNotEqual(self.head(), self.baseline)
                    self.assertEqual((self.repo / "cache/media_cache.json").read_text(), "retained")
                    self.assertEqual(
                        self.git("rev-list", "--count", "origin/main..HEAD").stdout.strip(), "1",
                    )

    def test_failed_staged_output_check_does_not_commit_or_push(self):
        self.write("test-bin/git", """#!/usr/bin/env bash
if [[ "$1" == diff && "$2" == --cached ]]; then exit 124; fi
exec "$TEST_REAL_GIT" "$@"
""")
        result = self.run_script(
            "--publish", TEST_REAL_GIT=shutil.which("git"), TEST_CHANGE="unpublished output",
        )
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.baseline)
        self.assertEqual((self.repo / "cache/media_cache.json").read_text(), "retained")


if __name__ == "__main__":
    unittest.main()
