"""Browser integration smoke tests. Set FOLIO_BROWSER_TEST=1 to enable."""
import os
import tempfile
import unittest
from pathlib import Path

from folio_eval.__main__ import main
from folio_eval.dataset import generate_suite
from folio_eval.runner import _safe_url
from folio_eval.schema import BenchmarkSuite


@unittest.skipUnless(os.getenv("FOLIO_BROWSER_TEST") == "1", "requires Playwright Chromium")
class BrowserSmokeTests(unittest.TestCase):
    def test_all_five_task_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = main(["run", "--count", "5", "--output", str(Path(tmp) / "run")])
            self.assertEqual(result, 0)

    def test_resume_existing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = str(Path(tmp) / "run")
            self.assertEqual(main(["run", "--count", "5", "--output", folder]), 0)
            self.assertEqual(main(["run", "--count", "5", "--output", folder, "--resume"]), 0)

    def test_deny_cross_origin(self):
        with self.assertRaises(ValueError):
            _safe_url("https://example.com/", "http://127.0.0.1:3000")


if __name__ == "__main__":
    unittest.main()
