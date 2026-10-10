"""Regression tests for read-only Claude cost analysis."""
import json
import tempfile
import unittest
from pathlib import Path
from scripts.report_claude_costs import report, usage_rows


class ClaudeCostReportTests(unittest.TestCase):
    def test_dedup_and_jst_day(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "usage.jsonl"
            row = {"call_id": "one", "timestamp_utc": "2026-10-10T16:10:00+00:00",
                   "stage": "news_article_generation", "model": "claude-sonnet-4-6",
                   "article_id": 301, "estimated_usd": 0.2,
                   "tokens": {"input": 10, "output": 20}}
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
            result = report(usage_rows(path), [{"id": 301, "date": "2026-10-11",
                                                  "title": "Test", "slug": "test-0301"}], date="2026-10-11")
            self.assertEqual(result["estimated_total_usd"], 0.2)
            self.assertEqual(result["daily_publication_cohorts"][0]["articles_in_main"], 1)
            self.assertEqual(result["daily_publication_cohorts"][0]["estimated_usd_per_article_in_main"], 0.2)
            self.assertEqual(result["cost_by_day_stage_model"][0]["calls"], 1)

    def test_unknown_price_and_zero_published(self):
        rows = [{"_day": "2026-10-11", "stage": "fact_audit", "model": "unknown",
                 "estimated_usd": None, "tokens": {}},
                {"_day": "2026-10-11", "stage": "fact_audit", "model": "known",
                 "estimated_usd": 0.4, "tokens": {}}]
        result = report(rows, [], date="2026-10-11")
        self.assertEqual(result["unknown_price_calls"], 1)
        self.assertIsNone(result["daily_publication_cohorts"][0]["estimated_usd_per_article_in_main"])
        self.assertEqual(result["unattributed_estimated_usd"], 0.4)


if __name__ == "__main__":
    unittest.main()
