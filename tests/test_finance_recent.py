"""記帳進行中判斷（finance_service.recent_expense）：python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RecentExpenseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ.pop("USE_FIRESTORE", None)
        import finance_service
        cls.fs = finance_service
        cls.now = finance_service.TAIPEI_TZ.localize(datetime(2026, 10, 26, 12, 0))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _write(self, created_at: str | None):
        data = {"projects": {}, "expenses": {}}
        if created_at is not None:
            data["expenses"]["e1"] = {"project": "日本旅行", "amount": 850, "created_at": created_at, "photo_noted": False}
        with open(self.fs.FINANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_no_expense_returns_none(self):
        self._write(None)
        self.assertIsNone(self.fs.recent_expense("C1", now=self.now))

    def test_expense_within_three_days_is_recent(self):
        self._write("2026-10-24 12:30")
        got = self.fs.recent_expense("C1", now=self.now)
        self.assertIsNotNone(got)
        self.assertEqual(got[0]["project"], "日本旅行")
        self.assertEqual(got[1], "e1")

    def test_old_expense_is_not_recent(self):
        self._write("2026-10-20 09:00")
        self.assertIsNone(self.fs.recent_expense("C1", now=self.now))

    def test_bad_date_is_not_recent(self):
        self._write("昨天")
        self.assertIsNone(self.fs.recent_expense("C1", now=self.now))


if __name__ == "__main__":
    unittest.main()
