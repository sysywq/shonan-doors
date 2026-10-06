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
    def test_targeted_stock_topic_is_not_hidden_by_prompt_slice(self):
        topics = [{"id": f"s{i}", "status": "candidate", "query": f"q{i}",
                   "titleIdea": f"t{i}", "area": "大磯", "category": "l",
                   "searchIntent": "guide"} for i in range(10)]
        with mock.patch.object(ga, "call_claude_stock_selection", return_value=[
                {"topicId": "s9", "decision": "skip", "skipReason": "test"}]) as choose:
            entries, updated = ga.run_stock_generation([], topics, "2026-09-28", [],
                                                       only_topic_id="s9", refill=False, limit=1)
        self.assertEqual(entries, [])
        self.assertEqual(choose.call_args.args[0][0]["id"], "s9")
        self.assertEqual(updated[9]["status"], "skipped")

    def test_municipality_name_does_not_block_unrelated_topic(self):
        existing = {"id": 78, "title": "鎌倉市のスーパーが開店", "dek": "梶原の買い物",
                    "subjectNames": [], "link": "https://example.com/store", "area": "鎌倉",
                    "cat": "l", "body": "スーパーの買い物"}
        proposed = {"subjectNames": ["鎌倉市"], "link": "https://example.com/life",
                    "area": "鎌倉", "cat": "l", "body": "移住後の保育や交通"}
        self.assertIsNone(ga.find_same_subject(proposed, [existing]))

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


class PlannedFallbackTopupTest(unittest.TestCase):
    """企画候補が全滅したときの補充探索(DAILY_TOPUP_MAX_ATTEMPTS 回まで)。"""

    def run_plan(self, fallback_batches, max_attempts=3, avoid=None):
        """企画候補(news)は全件が公開前監査で不合格。fallback は batches を1回ずつ返す。"""
        self.drafted = []
        with tempfile.TemporaryDirectory() as d:
            paths = {name: os.path.join(d, name + ".json") for name in
                     ("articles", "counter", "stock", "series", "report")}
            for name, data in {"articles": [], "counter": {"next_id": 40}, "stock": [], "series": []}.items():
                with open(paths[name], "w", encoding="utf-8") as f:
                    json.dump(data, f)
            areas = ["藤沢", "鎌倉", "茅ヶ崎", "平塚", "二宮", "逗子", "葉山", "大磯", "寒川", "藤沢"]
            discovered = [{"id": f"n{i}", "articleType": "news", "query": f"topic{i}",
                           "titleIdea": f"Topic {i}", "area": areas[i], "cat": "e",
                           "searchIntent": "local", "sourceUrl": "https://example.org/official",
                           "subject": f"subject{i}"} for i in range(10)]
            judgments = {c["id"]: {"demand": 18, "timing": 15, "usefulness": 15, "originality": 12}
                         for c in discovered}
            fallback_calls = []
            batches = list(fallback_batches)

            def news(existing, *args, candidate=None, gate_rejections=None, label="", **kwargs):
                if candidate is not None:
                    self.drafted.append(candidate["id"])
                    # 企画候補は公開前監査で不合格(品質基準は緩めない)
                    gate_rejections.append({"title": f"NG {candidate['id']}", "area": candidate["area"],
                                            "link": f"https://example.org/{candidate['id']}",
                                            "draft": {"title": f"NG {candidate['id']}",
                                                      "area": candidate["area"], "tags": []}})
                    return [], [], []
                fallback_calls.append({"label": label, "count": kwargs.get("count"),
                                       "max_refills": kwargs.get("max_refills"),
                                       "avoid": [a.get("id") for a in existing]})
                return ([dict(e) for e in batches.pop(0)] if batches else []), [], []

            patches = [mock.patch.object(ga, "ARTICLES_JSON_PATH", paths["articles"]),
                       mock.patch.object(ga, "ID_COUNTER_PATH", paths["counter"]),
                       mock.patch.object(ga, "STOCK_TOPICS_PATH", paths["stock"]),
                       mock.patch.object(ga, "EVENT_SERIES_PATH", paths["series"]),
                       mock.patch.object(ga, "RUN_REPORT_PATH", paths["report"]),
                       mock.patch.object(ga.anthropic, "Anthropic", return_value=object(), create=True),
                       mock.patch.object(ga, "refill_stock_topics_if_needed", side_effect=lambda a, b, c: (b, 0)),
                       mock.patch.object(ep, "discover", return_value=discovered),
                       mock.patch.object(ep, "judge", return_value=judgments),
                       mock.patch.object(ga, "growth_signal", return_value={}),
                       mock.patch.object(ga, "growth_hints", return_value=[]),
                       mock.patch.object(ga, "run_news_generation", side_effect=news),
                       mock.patch.object(ga, "find_same_subject", return_value=None),
                       mock.patch.object(ga, "gate_budget_exhausted", return_value=False),
                       mock.patch.object(ga, "register_event_series_if_new", side_effect=lambda a, b: (b, False)),
                       mock.patch.object(ga, "write_gate_summary"),
                       mock.patch.object(ga, "write_shortfall_summary")]
            with ExitStack() as stack:
                stack.enter_context(mock.patch.multiple(ga, DAILY_TARGET_ARTICLES=5, DAILY_MIN_ARTICLES=3,
                                                        DAILY_TOPUP_MAX_ATTEMPTS=max_attempts,
                                                        MAX_EDITORIAL_DRAFT_ATTEMPTS=8))
                stack.enter_context(mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}))
                for patch in patches:
                    stack.enter_context(patch)
                ga.run_editorial_plan([], "2026-10-06", [], [], [], [], avoid=avoid)
            with open(paths["articles"], encoding="utf-8") as f:
                self.saved_articles = json.load(f)
            with open(paths["report"], encoding="utf-8") as f:
                return json.load(f), fallback_calls

    @staticmethod
    def entry(n):
        return {"id": "pending", "articleType": "news", "area": "藤沢", "cat": "e", "title": f"補充{n}",
                "tags": [f"tag{n}"], "subjectNames": [f"subject-fb{n}"], "date": "2026-10-06"}

    def test_fallback_retries_up_to_topup_max_attempts_until_minimum(self):
        # 2026-10-06 の事例: 企画候補0件・fallback 2件で打ち切られていた → 3件に届くまで繰り返す
        report, calls = self.run_plan([[self.entry(1)], [self.entry(2)], [self.entry(3)], [self.entry(4)]])
        self.assertEqual(len(calls), 3)  # 3件に届いたので4回目は呼ばない
        self.assertEqual(report["accepted_ids"], [40, 41, 42])
        self.assertIsNone(report["shortfall"])
        # 1回ごとの補充は refill せず、回数は DAILY_TOPUP_MAX_ATTEMPTS で制御する
        self.assertTrue(all(c["max_refills"] == 0 for c in calls))
        self.assertEqual([c["count"] for c in calls], [5, 4, 3])

    def test_fallback_is_bounded_by_topup_max_attempts(self):
        report, calls = self.run_plan([[], [self.entry(1)], [], [self.entry(2)], [self.entry(3)]],
                                      max_attempts=3)
        self.assertEqual(len(calls), 3)  # 上限で打ち切る(無制限に再試行しない)
        self.assertEqual(report["accepted_ids"], [40])
        self.assertEqual(report["shortfall"]["total"], 1)

    def test_fallback_avoids_drafts_rejected_in_same_run(self):
        _report, calls = self.run_plan([[], [], []])
        # 同一run内で公開前監査に不合格だった企画候補のドラフトを、重複チェックの対象として毎回渡す
        for c in calls:
            self.assertGreater(c["avoid"].count("gate-rejected"), 0)

    def test_fallback_skips_candidate_duplicating_rejected_draft(self):
        dup = dict(self.entry(1), title="NG n0")  # 不合格ドラフトと同じタイトル
        report, calls = self.run_plan([[dup], [self.entry(2)], [self.entry(3)], [self.entry(4)]])
        self.assertEqual(len(calls), 3)
        titles = [a["title"] for a in self.saved_articles]
        self.assertNotIn("NG n0", titles)  # 同一run内で不合格になった対象は採用しない
        self.assertEqual(titles, ["補充2", "補充3"])
        self.assertEqual(report["shortfall"]["total"], 2)

    def test_targets_rejected_earlier_the_same_day_are_not_redrafted(self):
        # 補充 run(round 1 以降): 前の round で見送った企画・ドラフトは、企画候補にも補充探索にも使わない
        import daily_state as ds
        state = {"rejected": [ds.compact({"titleIdea": "Topic 0", "subject": "subject0", "area": "藤沢"},
                                         "企画で試したが採用されなかった", 0),
                              ds.compact(dict(self.entry(1), title="補充1"), "公開前監査で不合格", 0)]}
        report, calls = self.run_plan([[self.entry(1)], [self.entry(2)], [self.entry(3)], [self.entry(4)]],
                                      avoid=ds.avoid_articles(state))
        self.assertNotIn("n0", self.drafted)  # 見送り済みの企画はドラフトを作らない
        self.assertIn("n1", self.drafted)
        titles = [a["title"] for a in self.saved_articles]
        self.assertNotIn("補充1", titles)  # 見送り済みと同じ対象の補充候補は採らない
        self.assertEqual(titles, ["補充2", "補充3"])
        self.assertEqual(report["shortfall"]["total"], 2)
        self.assertTrue(all(a.get("id") != "rejected-today" for a in self.saved_articles))  # 台帳には書かない
        self.assertTrue(all(c["avoid"].count("rejected-today") == 2 for c in calls))


if __name__ == "__main__":
    unittest.main()
