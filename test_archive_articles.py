import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "jobs" / "archive_articles.py"
SPEC = importlib.util.spec_from_file_location("archive_articles_module", MODULE_PATH)
archive_articles = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(archive_articles)


class ArchiveWindowTest(unittest.TestCase):
    def test_no_state_uses_default_window(self):
        now = 1_700_000_000
        self.assertEqual(archive_articles.calculate_since_ts(None, window_days=7, now_ts=now), now - 7 * 86400)

    def test_recent_success_still_uses_window(self):
        now = 1_700_000_000
        last_success = now - 3 * 86400
        self.assertEqual(archive_articles.calculate_since_ts(last_success, window_days=7, now_ts=now), now - 7 * 86400)

    def test_old_success_backfills_from_success(self):
        now = 1_700_000_000
        last_success = now - 10 * 86400
        self.assertEqual(archive_articles.calculate_since_ts(last_success, window_days=7, now_ts=now), last_success)


if __name__ == "__main__":
    unittest.main()
