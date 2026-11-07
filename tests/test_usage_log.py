"""AI 用量記錄（usage_log）：python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usage_log  # noqa: E402

TODAY = date(2026, 11, 9)  # 週一


def at(day: str) -> datetime:
    return usage_log.TAIPEI_TZ.localize(datetime.fromisoformat(f"{day}T10:00:00"))


class UsageLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ai_usage.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_weekly_summary(self):
        usage_log.record("anthropic", "claude-haiku-4-5-20251001", "Claude 文字", 1_000_000, 100_000,
                         now=at("2026-11-03"), path=self.path)
        usage_log.record("gemini", "gemini-2.5-flash", "Gemini 看照片", 1_000_000, 0,
                         now=at("2026-11-04"), path=self.path)
        usage_log.record("gemini", "gemini-2.5-flash", "Gemini 看照片", 10, 0,
                         now=at("2026-10-20"), path=self.path)  # 不在上週
        text = usage_log.summary_text(today=TODAY, path=self.path)
        self.assertIn("11/02～11/08", text)
        self.assertIn("US$1.80", text)  # 1.00＋0.50（Claude）＋0.30（Gemini）
        self.assertIn("Claude 文字 1 次", text)
        self.assertIn("Gemini 看照片 1 次", text)

    def test_bad_line_skipped(self):
        usage_log.record("gemini", "gemini-2.5-flash", "Gemini 收據", 100, 10, now=at("2026-11-05"), path=self.path)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"time": "2026-11-0\n不是 JSON\n')
        rows, bad = usage_log.load(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(bad, 2)
        self.assertIn("Gemini 收據 1 次", usage_log.summary_text(today=TODAY, path=self.path))

    def test_unknown_model_listed(self):
        usage_log.record("anthropic", "mystery-model", "Claude 文字", 10, 10, now=at("2026-11-05"), path=self.path)
        text = usage_log.summary_text(today=TODAY, path=self.path)
        self.assertIn("單價未知", text)
        self.assertIn("mystery-model", text)

    def test_zero_usage(self):
        self.assertIn("上週沒有呼叫 AI", usage_log.summary_text(today=TODAY, path=self.path))

    def test_record_never_raises(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        bad_path = os.path.join(blocker, "ai_usage.jsonl")  # 上一層是檔案，寫不進去
        self.assertEqual(usage_log.record("gemini", "gemini-2.5-flash", "x", 1, 1, path=bad_path), {})

    def test_no_message_content_stored(self):
        usage_log.record("anthropic", "claude-haiku-4-5-20251001", "Claude 文字", 5, 5, now=at("2026-11-05"), path=self.path)
        with open(self.path, encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        self.assertEqual(set(row), {"time", "service", "model", "feature", "in", "out", "usd"})

    def test_token_helpers(self):
        msg = SimpleNamespace(usage=SimpleNamespace(input_tokens=12, output_tokens=3))
        self.assertEqual(usage_log.claude_tokens(msg), (12, 3))
        self.assertEqual(usage_log.claude_tokens(SimpleNamespace()), (0, 0))
        meta = SimpleNamespace(prompt_token_count=7, candidates_token_count=2, thoughts_token_count=None)
        self.assertEqual(usage_log.gemini_tokens(SimpleNamespace(usage_metadata=meta)), (7, 2))
        self.assertEqual(usage_log.gemini_tokens(SimpleNamespace()), (0, 0))

    def test_command(self):
        for t in ("AI用量", "ai用量", "AI 用量", " AI用量 "):
            self.assertTrue(usage_log.is_usage_command(t), t)
        for t in ("用量", "AI用量很高嗎", "記帳"):
            self.assertFalse(usage_log.is_usage_command(t), t)


if __name__ == "__main__":
    unittest.main()
