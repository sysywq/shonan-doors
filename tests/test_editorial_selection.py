import unittest
from unittest import mock

from editorial_selection import WEIGHTS, observed_points, period, plan, score, select
from editorial_planning import candidate_pool


class EditorialSelectionTest(unittest.TestCase):
    def candidates(self):
        return [{"id": str(i), "area": ["藤沢", "鎌倉", "茅ヶ崎", "大磯", "二宮"][i % 5],
                 "cat": ["e", "g", "l", "t", "b"][i % 5],
                 "articleType": "news" if i % 2 else "stock", "query": f"q{i}"}
                for i in range(10)]

    def test_exploration_can_select_stock_ahead_of_repeated_news(self):
        existing = [{"area": "藤沢", "cat": "e", "articleType": "news", "date": "2026-09-26"}] * 10
        news = {"area": "藤沢", "cat": "e", "articleType": "news", "title": "news"}
        stock = {"area": "大磯", "cat": "l", "articleType": "stock", "title": "stock"}
        self.assertEqual(select([news, stock], existing, {}, 1, "2026-09-28"), [stock])

    def test_signal_is_bounded_and_missing_signal_works(self):
        a = {"area": "藤沢", "cat": "e", "articleType": "news"}
        b = {"area": "鎌倉", "cat": "t", "articleType": "stock"}
        signal = {"editorial_segments": [{"area": "鎌倉", "cat": "t", "gsc_impressions": 100,
                                          "ga4_views": 100}]}
        self.assertEqual(select([a, b], [], signal, 2, "2026-09-28")[0], b)
        self.assertEqual(len(select([a, b], [], {}, 2, "2026-09-28")), 2)
        self.assertLessEqual(observed_points(b, signal, "2026-09-28"), 4)
        self.assertLessEqual(observed_points(b, signal, "2026-11-15"), 6)

    def test_october_uses_two_exploration_slots_and_november_one(self):
        candidates = self.candidates()
        existing = [{"area": "藤沢", "cat": "e", "articleType": "news", "date": "2026-09-20"}] * 20
        judgments = {c["id"]: {"demand": 20, "timing": 16, "usefulness": 16, "originality": 12}
                     for c in candidates}
        for c in candidates[:3]:
            judgments[c["id"]] = {"demand": 25, "timing": 20, "usefulness": 20, "originality": 15}
        oct_plan = plan(candidates, judgments, {}, existing, "2026-10-15")[:5]
        nov_plan = plan(candidates, judgments, {}, existing, "2026-11-15")[:5]
        oct_ids = [c["id"] for c, _ in oct_plan]
        nov_ids = [c["id"] for c, _ in nov_plan]
        self.assertEqual(len(set(oct_ids)), 5)
        self.assertEqual(len(set(nov_ids)), 5)
        self.assertEqual(sum(result["slot"] == "exploration" for _, result in oct_plan), 2)
        self.assertEqual(sum(result["slot"] == "exploration" for _, result in nov_plan), 1)
        self.assertEqual(period("2026-10-15"), "broad_exploration")
        self.assertEqual(period("2026-11-15"), "light_optimization")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(period("2026-12-01"), "light_optimization")
        with mock.patch.dict("os.environ", {"EDITORIAL_NOV_REVIEW_APPROVED": "1"}):
            self.assertEqual(period("2026-12-01"), "post_review")
        self.assertEqual(sum(WEIGHTS.values()), 100)
        self.assertLessEqual(score(candidates[0], judgments["0"], {}, existing, "2026-10-15")["total"], 100)

    def test_pool_deduplicates_existing_subject_and_query(self):
        discovered = [{"id": "n1", "articleType": "news", "query": "茅ヶ崎 新店", "subject": "既存店",
                       "area": "茅ヶ崎", "cat": "g"},
                      {"id": "n2", "articleType": "news", "query": "鎌倉 子育て", "subject": "新規",
                       "area": "鎌倉", "cat": "l"}]
        stock = [{"id": "s1", "articleType": "stock", "query": "鎌倉　子育て", "subject": "",
                  "area": "鎌倉", "cat": "l"}]
        pool = candidate_pool(discovered, stock, [{"title": "既存店"}])
        self.assertEqual([c["id"] for c in pool], ["n2"])


if __name__ == "__main__":
    unittest.main()
