import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser-script tests")
class GalleryFavoritesTests(unittest.TestCase):
    def test_favorites_script_concurrency_and_storage_errors(self):
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("gallery_favorites.test.cjs"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
