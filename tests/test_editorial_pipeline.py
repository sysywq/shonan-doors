import json
from contextlib import ExitStack
import os
import tempfile
import types
import unittest
from unittest import mock
import sys

sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import generate_articles as ga
import editorial_planning as ep


class EditorialPipelineTest(unittest.TestCase):
    def test_plans_before_drafting_and_reserves_only_selected_ids(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {name: os.path.join(d, name + ".json") for name in
                     ("articles", "counter", "stock", "series", "report")}
            fixtures = {"articles": [], "counter": {"next_id": 40},
                        "stock": [{"id": "s1", "status": "candidate", "query": "大磯 暮らし",
                                   "titleIdea": "大磯の暮らし", "area": "大磯", "category": "l",
                                   "searchIntent": "暮らしを知る"}], "series": []}
            for name, data in fixtures.items():
                with open(paths[name], "w", encoding="utf-8") as f:
                    json.dump(data, f)
            areas = ["藤沢", "鎌倉", "茅ヶ崎", "平塚", "二宮", "逗子", "葉山", "藤沢", "鎌倉"]
            cats = ["e", "g", "l", "t", "b", "c", "p", "g", "t"]
            discovered = [{"id": f"n{i}", "articleType": "news", "query": f"topic{i}",
                           "titleIdea": f"Topic {i}", "area": areas[i], "cat": cats[i],
                           "searchIntent": "local", "sourceUrl": "https://example.org/official",
                           "subject": f"subject{i}"} for i in range(9)]
            drafted = []
            def news(*args, candidate=None, **kwargs):
                drafted.append(candidate["id"])
                return ([{"id": "pending", "articleType": "news", "area": candidate["area"],
                          "cat": candidate["cat"], "title": candidate["titleIdea"], "tags": [],
                          "subjectNames": [candidate["subject"]], "date": "2026-09-28"}], [], [])
            def stock(*args, only_topic_id=None, **kwargs):
                drafted.append(only_topic_id)
                return ([{"id": "pending", "articleType": "stock", "area": "大磯", "cat": "l",
                          "title": "大磯の暮らし", "tags": [], "subjectNames": ["大磯の暮らし"],
                          "date": "2026-09-28", "_topicId": "s1"}], args[1])
            judgments = {c["id"]: {"demand": 18, "timing": 15, "usefulness": 15,
                                    "originality": 12} for c in discovered}
            judgments["s1"] = {"demand": 25, "timing": 20, "usefulness": 20, "originality": 15}
            patches = [mock.patch.object(ga, "ARTICLES_JSON_PATH", paths["articles"]),
                       mock.patch.object(ga, "ID_COUNTER_PATH", paths["counter"]),
                       mock.patch.object(ga, "STOCK_TOPICS_PATH", paths["stock"]),
                       mock.patch.object(ga, "EVENT_SERIES_PATH", paths["series"]),
                       mock.patch.object(ga, "RUN_REPORT_PATH", paths["report"]),
                       mock.patch.object(ga.anthropic, "Anthropic", return_value=object(), create=True),
                       mock.patch.object(ga, "refill_stock_topics_if_needed", side_effect=lambda a,b,c: (b,0)),
                       mock.patch.object(ep, "discover", return_value=discovered),
                       mock.patch.object(ep, "judge", return_value=judgments),
                       mock.patch.object(ga, "growth_signal", return_value={}),
                       mock.patch.object(ga, "growth_hints", return_value=[]),
                       mock.patch.object(ga, "run_news_generation", side_effect=news),
                       mock.patch.object(ga, "run_stock_generation", side_effect=stock),
                       mock.patch.object(ga, "find_same_subject", return_value=None),
                       mock.patch.object(ga, "is_duplicate", return_value=None),
                       mock.patch.object(ga, "gate_budget_exhausted", return_value=False),
                       mock.patch.object(ga, "register_event_series_if_new", side_effect=lambda a,b: (b,False)),
                       mock.patch.object(ga, "write_gate_summary"),
                       mock.patch.object(ga, "write_shortfall_summary")]
            with ExitStack() as stack:
                stack.enter_context(mock.patch.multiple(ga, DAILY_TARGET_ARTICLES=5, DAILY_MIN_ARTICLES=3))
                stack.enter_context(mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}))
                for patch in patches:
                    stack.enter_context(patch)
                ga.run_editorial_plan([], "2026-09-28", [], [], [], [])
            with open(paths["report"], encoding="utf-8") as f:
                report = json.load(f)
            with open(paths["stock"], encoding="utf-8") as f:
                topics = json.load(f)
            self.assertEqual(report["planning"]["candidate_count"], 10)
            self.assertEqual(report["accepted_ids"], [40, 41, 42, 43, 44])
            self.assertIn("s1", drafted)
            self.assertEqual(topics[0]["status"], "generated")
            self.assertEqual(topics[0]["generatedArticleId"], report["stock_ids"][0])


if __name__ == "__main__":
    unittest.main()
