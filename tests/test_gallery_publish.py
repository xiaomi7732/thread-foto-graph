import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
GALLERY_FILES = (
    "docs/reply-images.html",
    "docs/reply-images.txt",
    "data/reply-images.md",
)


def _bash_path():
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            bundled = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if bundled.is_file():
                return str(bundled)
        return None
    return shutil.which("bash")


@unittest.skipUnless(shutil.which("git") and _bash_path(), "Git and Bash are required")
class GalleryPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.upstream = self.root / "upstream"
        self.runner = self.root / "runner"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.upstream))
        self.configure(self.upstream)
        self.write(self.upstream, "docs/graph.json", "old graph\n")
        for path in GALLERY_FILES:
            self.write(self.upstream, path, "previous gallery\n")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "Seed graph and gallery")
        self.git(self.upstream, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.runner))
        self.configure(self.runner)
        for path in GALLERY_FILES:
            self.write(self.runner, path, "old graph\n")
        self.write(self.runner, "src/__init__.py", "")
        self.write(self.runner, "src/reply_gallery.py", textwrap.dedent("""\
            import os
            from pathlib import Path
            if os.environ.get("GALLERY_TEST_FAIL"):
                raise SystemExit(2)
            graph = Path("docs/graph.json").read_text(encoding="utf-8")
            Path("calls.log").write_text(graph, encoding="utf-8")
            for path in ("docs/reply-images.html", "docs/reply-images.txt", "data/reply-images.md"):
                Path(path).write_text(graph, encoding="utf-8")
        """))
        workflow = (ROOT_DIR / ".github" / "workflows" / "reply-image-gallery.yml").read_text(
            encoding="utf-8"
        )
        publish = workflow.split("      - name: Publish gallery\n", 1)[1]
        publish = publish.split("\n      - name:", 1)[0]
        script = textwrap.dedent(publish.split("        run: |\n", 1)[1])
        self.script = self.root / "publish.sh"
        self.script.write_text(script, encoding="utf-8", newline="\n")

    def git(self, cwd, *args):
        command = ["git"]
        if cwd == self.remote:
            command.extend(["--git-dir", str(self.remote)])
        return subprocess.run(
            [*command, *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout

    def configure(self, repository):
        self.git(repository, "config", "user.name", "Gallery Test")
        self.git(repository, "config", "user.email", "test@example.invalid")
        self.git(repository, "config", "commit.gpgsign", "false")

    def write(self, root, path, content):
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def push_upstream(self, path, content):
        self.write(self.upstream, path, content)
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "Concurrent upstream update")
        self.git(self.upstream, "push", "origin", "main")

    def publish(self, fail_generation=False):
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        if fail_generation:
            env["GALLERY_TEST_FAIL"] = "1"
        return subprocess.run(
            [_bash_path(), "-e", str(self.script)],
            cwd=self.runner, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_changed_graph_is_regenerated_before_push(self):
        self.push_upstream("docs/graph.json", "new graph\n")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.runner / "calls.log").read_text(), "new graph\n")
        for path in GALLERY_FILES:
            self.assertEqual(self.git(self.remote, "show", f"main:{path}"), "new graph\n")

    def test_unrelated_upstream_change_reuses_gallery(self):
        self.push_upstream("README.md", "Unrelated documentation\n")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.runner / "calls.log").exists())
        self.assertEqual(
            self.git(self.remote, "show", "main:docs/reply-images.html"), "old graph\n"
        )

    def test_generation_failure_does_not_push_stale_gallery(self):
        self.push_upstream("docs/graph.json", "new graph\n")
        result = self.publish(fail_generation=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.git(self.remote, "show", "main:docs/reply-images.html"),
            "previous gallery\n",
        )


if __name__ == "__main__":
    unittest.main()
