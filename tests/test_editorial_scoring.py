# -*- coding: utf-8 -*-
"""企画の採点(editorial_planning.judge / editorial_selection.score)の入力検証と、fallback の通知のテスト。
2026-10-09 Run 37874933391 で「採点に失敗。従来の横断候補経路で続行: ValueError」となり、
候補1件の不正(または id の欠落)で全候補の採点が捨てられて従来経路へ fallback した事例の再発防止。
APIは呼ばない。実行: python -m unittest tests/test_editorial_scoring.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import editorial_planning as ep  # noqa: E402
import editorial_selection as es  # noqa: E402
import generate_articles as ga  # noqa: E402
import report_gate_rejections as rgr  # noqa: E402

GOOD = {"demand": 18, "timing": 15, "usefulness": 15, "originality": 12}


def cands(n=4):
    # 本番と同じく、X / Places 由来の長い id を含める
    ids = ["x-2108333868327776493", "places-ChIJN1t_tDeuEmsRUsoyG83frY4", "news-00", "t0153"][:n]
    ids += [f"news-{i:02d}" for i in range(1, n - 3)] if n > 4 else []
    return [{"id": cid, "articleType": "news", "query": f"q{i}", "titleIdea": f"候補{i}",
             "area": "藤沢", "cat": "g", "searchIntent": "x"} for i, cid in enumerate(ids)]


class Block:
    type = "tool_use"
    name = "submit_editorial_scores"

    def __init__(self, data):
        self.input = data


class Response:
    def __init__(self, data, stop_reason="tool_use"):
        self.content = [Block(data)]
        self.stop_reason = stop_reason


class FakeClient:
    """messages.create が呼ばれるたびに responses を順に返す。prompt から候補の別名(c01…)を記録する。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response(item)


def scores(*entries):
    return {"scores": [{**GOOD, "id": f"c{i + 1:02d}", **e} if isinstance(e, dict) else e
                       for i, e in enumerate(entries)]}


class RootCauseReproductionTest(unittest.TestCase):
    """旧実装で ValueError になり、全候補が fallback した3つの経路。新実装では不正な候補だけを外す。"""

    def test_old_paths_raised_value_error_for_whole_batch(self):
        # 旧 score() は 1候補の範囲外/文字列で ValueError、旧 judge() は 1件欠落で ValueError を送出していた
        with self.assertRaises(ValueError):
            es.score({"id": "a"}, dict(GOOD, demand=30), {}, [], "2026-10-09")
        with self.assertRaises(ValueError):
            es.score({"id": "a"}, dict(GOOD, timing=None), {}, [], "2026-10-09")

    def test_single_out_of_range_candidate_is_excluded_not_whole_batch(self):
        c = cands()
        bad = dict(GOOD, demand=30)
        client = FakeClient(scores({}, bad, {}, {}), scores(dict(bad, id="c01")))  # 再採点でも範囲外
        diag = {}
        result = ep.judge(client, c, [], diagnostics=diag)
        self.assertEqual(set(result), {c[0]["id"], c[2]["id"], c[3]["id"]})
        self.assertIn(c[1]["id"], diag["invalid"])
        self.assertIn("demand=30 が範囲外(0〜25)", diag["invalid"][c[1]["id"]]["problems"][0])
        self.assertTrue(diag["retried"])
        self.assertEqual(diag["valid"], 3)

    def test_missing_candidate_is_rescored_once(self):
        c = cands()
        client = FakeClient(scores({}, {}, {}), scores(dict(GOOD, id="c04", timing=9)))
        result = ep.judge(client, c, [])
        self.assertEqual(len(result), 4)
        self.assertEqual(result[c[3]["id"]]["timing"], 9)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn('"id": "c04"', client.prompts[1])
        self.assertNotIn('"id": "c01"', client.prompts[1])  # 再採点は不足分だけ

    def test_long_ids_are_replaced_by_short_aliases(self):
        c = cands()
        client = FakeClient(scores({}, {}, {}, {}))
        result = ep.judge(client, c, [{"id": 182, "title": "既存"}])
        self.assertEqual(set(result), {x["id"] for x in c})
        self.assertNotIn("x-2108333868327776493", client.prompts[0])
        self.assertNotIn('"id": 182', client.prompts[0])  # 既存記事の数値idは渡さない


class InputValidationTest(unittest.TestCase):
    def test_string_number_is_read_and_logged(self):
        c = cands()
        diag = {}
        result = ep.judge(FakeClient(scores({"demand": "18"}, {}, {}, {})), c, [], diagnostics=diag)
        self.assertEqual(result[c[0]["id"]]["demand"], 18)
        self.assertTrue(any("文字列数値" in n for n in diag["notes"]))

    def test_null_bool_nan_and_text_are_invalid_not_zero(self):
        for value in (None, True, float("nan"), "高い", [], -1):
            judgment, problems, _ = es.validate_judgment(dict(GOOD, usefulness=value))
            self.assertIsNone(judgment, value)
            self.assertTrue(problems, value)
        judgment, problems, _ = es.validate_judgment({"demand": 10})
        self.assertIsNone(judgment)
        self.assertEqual(len(problems), 3)  # 欠落を 0 で埋めない

    def test_scores_returned_as_json_string_are_recovered(self):
        c = cands()
        payload = {"scores": json.dumps(scores({}, {}, {}, {})["scores"])}
        diag = {}
        self.assertEqual(len(ep.judge(FakeClient(payload), c, [], diagnostics=diag)), 4)
        self.assertTrue(any("JSON文字列" in n for n in diag["notes"]))

    def test_conflicting_duplicate_ids_are_invalid(self):
        c = cands()
        data = scores({}, {}, {}, {})
        data["scores"].append(dict(GOOD, id="c02", demand=3))
        diag = {}
        client = FakeClient(data, scores(dict(GOOD, id="c02", demand=4)))
        result = ep.judge(client, c, [], diagnostics=diag)
        self.assertEqual(result[c[1]["id"]]["demand"], 4)  # どちらの値も採らず、再採点の値を使う
        self.assertTrue(diag["retried"])
        self.assertIn('"id": "c02"', client.prompts[1])

    def test_conflicting_duplicate_ids_stay_excluded_if_retry_fails(self):
        c = cands()
        data = scores({}, {}, {}, {})
        data["scores"].append(dict(GOOD, id="c02", demand=3))
        diag = {}
        result = ep.judge(FakeClient(data, {"scores": []}), c, [], diagnostics=diag)
        self.assertNotIn(c[1]["id"], result)
        self.assertIn("食い違う", diag["invalid"][c[1]["id"]]["problems"][0])

    def test_identical_duplicates_are_merged(self):
        c = cands()
        data = scores({}, {}, {}, {})
        data["scores"].append(dict(GOOD, id="c02"))
        self.assertEqual(len(ep.judge(FakeClient(data), c, [])), 4)

    def test_unknown_ids_are_ignored_with_diagnostics(self):
        c = cands()
        data = scores({}, {}, {}, {})
        data["scores"].append(dict(GOOD, id="c99"))
        diag = {}
        ep.judge(FakeClient(data), c, [], diagnostics=diag)
        self.assertEqual(len(diag["unknown"]), 1)

    def test_broken_structure_raises_editorial_score_error_with_diagnostics(self):
        for payload in ({"scores": 5}, {"result": []}, {"scores": "not json"}):
            with self.assertRaises(ep.EditorialScoreError):
                ep.judge(FakeClient(payload), cands(), [])

    def test_no_valid_score_raises(self):
        c = cands()
        bad = {k: None for k in GOOD}
        with self.assertRaises(ep.EditorialScoreError) as cm:
            ep.judge(FakeClient(scores(bad, bad, bad, bad), scores(bad, bad, bad, bad)), c, [])
        self.assertEqual(len(cm.exception.diagnostics["invalid"]), 4)

    def test_duplicate_candidate_ids_are_rejected(self):
        c = cands()
        c[1]["id"] = c[0]["id"]
        with self.assertRaises(ep.EditorialScoreError):
            ep.judge(FakeClient(), c, [])

    def test_api_error_is_not_a_score_error(self):
        class RateLimitError(Exception):
            pass
        with self.assertRaises(RateLimitError) as cm:
            ep.judge(FakeClient(RateLimitError("429")), cands(), [])
        self.assertNotIsInstance(cm.exception, ValueError)

    def test_plan_ranks_only_judged_candidates(self):
        c = cands()
        ranked = es.plan(c, {c[0]["id"]: GOOD, c[2]["id"]: GOOD}, {}, [], "2026-10-09")
        self.assertEqual({x["id"] for x, _ in ranked}, {c[0]["id"], c[2]["id"]})

    def test_legacy_select_still_works_without_ai_scores(self):
        a = {"area": "藤沢", "cat": "e", "articleType": "news"}
        self.assertEqual(len(es.select([a, dict(a, area="鎌倉")], [], {}, 2, "2026-10-09")), 2)


class PlanningFallbackTest(unittest.TestCase):
    """run_editorial_plan: 一部不正なら fallback しない / 使えない時は原因をレポートと Issue に残す。"""

    def run_plan(self, judge):
        self.cross = []
        with tempfile.TemporaryDirectory() as d:
            paths = {n: os.path.join(d, n + ".json") for n in ("articles", "counter", "stock", "series", "report")}
            for n, data in {"articles": [], "counter": {"next_id": 40}, "stock": [], "series": []}.items():
                with open(paths[n], "w", encoding="utf-8") as f:
                    json.dump(data, f)
            discovered = [dict(x, sourceUrl="https://example.org", subject=x["titleIdea"],
                               area=["藤沢", "鎌倉", "茅ヶ崎", "平塚"][i])
                          for i, x in enumerate(cands())]

            def news(*args, candidate=None, **kw):
                if candidate is None:
                    return [], [], []
                return ([{"id": "pending", "articleType": "news", "area": candidate["area"], "cat": "g",
                          "title": candidate["titleIdea"], "tags": [], "subjectNames": [candidate["subject"]],
                          "freshness": {"kind": "news", "announcementDate": "2026-10-08"}}], [], [])

            def cross(*args, fallback=None, **kw):
                self.cross.append(fallback)
                ga.write_run_report(status="ok", planning_fallback=fallback, date="2026-10-09")

            with ExitStack() as st:
                for p in [mock.patch.object(ga, "ARTICLES_JSON_PATH", paths["articles"]),
                          mock.patch.object(ga, "ID_COUNTER_PATH", paths["counter"]),
                          mock.patch.object(ga, "STOCK_TOPICS_PATH", paths["stock"]),
                          mock.patch.object(ga, "EVENT_SERIES_PATH", paths["series"]),
                          mock.patch.object(ga, "RUN_REPORT_PATH", paths["report"]),
                          mock.patch.object(ga.anthropic, "Anthropic", return_value=object(), create=True),
                          mock.patch.object(ga, "refill_stock_topics_if_needed", side_effect=lambda a, b, c: (b, 0)),
                          mock.patch.object(ep, "discover", return_value=discovered),
                          mock.patch.object(ep, "judge", side_effect=judge),
                          mock.patch.object(ga, "growth_signal", return_value={}),
                          mock.patch.object(ga, "growth_hints", return_value=[]),
                          mock.patch.object(ga, "run_news_generation", side_effect=news),
                          mock.patch.object(ga, "run_cross_selection", side_effect=cross),
                          mock.patch.object(ga, "gate_budget_exhausted", return_value=False),
                          mock.patch.object(ga, "find_same_subject", return_value=None),
                          mock.patch.object(ga, "is_duplicate", return_value=None),
                          mock.patch.object(ga, "register_event_series_if_new", side_effect=lambda a, b: (b, False)),
                          mock.patch.object(ga, "write_gate_summary"),
                          mock.patch.object(ga, "write_shortfall_summary"),
                          mock.patch.multiple(ga, DAILY_TARGET_ARTICLES=3, DAILY_MIN_ARTICLES=3),
                          mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "GITHUB_STEP_SUMMARY": ""})]:
                    st.enter_context(p)
                ga._freshness_rejections.clear()
                lines = []
                ga.run_editorial_plan([], "2026-10-09", [], lines, [], [])
            with open(paths["report"], encoding="utf-8") as f:
                return json.load(f), lines

    def test_partial_invalid_scores_do_not_fall_back(self):
        def judge(client, candidates, existing, diagnostics=None):
            diagnostics.update(invalid={candidates[1]["id"]: {"problems": ["demand=30 が範囲外(0〜25)"],
                                                              "raw": {"demand": "30"}}},
                               missing=[], notes=[], valid=3)
            return {c["id"]: GOOD for i, c in enumerate(candidates) if i != 1}

        report, lines = self.run_plan(judge)
        self.assertEqual(self.cross, [])  # 従来経路へ fallback しない
        self.assertEqual(report["planning"]["judged_count"], 3)
        self.assertEqual(report["planning"]["excluded_by_scoring"], [cands()[1]["id"]])
        self.assertTrue(any(line.startswith("除外(企画採点)") and "範囲外" in line for line in lines))
        self.assertEqual(len(report["accepted_ids"]), 3)

    def test_unusable_scores_fall_back_with_cause_and_diagnostics(self):
        def judge(client, candidates, existing, diagnostics=None):
            raise ep.EditorialScoreError("有効な採点が0件", {"candidates": 4, "valid": 0,
                                                          "invalid": {"x": {"problems": ["demandが欠落/null"]}},
                                                          "missing": [], "notes": []})

        report, lines = self.run_plan(judge)
        fb = self.cross[0]
        self.assertEqual(fb["cause"], "score_response_invalid")
        self.assertEqual(fb["stage"], "採点")
        self.assertIn("EditorialScoreError: 有効な採点が0件", fb["exception"])
        self.assertEqual(fb["candidate_count"], 4)
        self.assertEqual(fb["diagnostics"]["valid"], 0)
        self.assertEqual(report["planning_fallback"]["cause"], "score_response_invalid")
        self.assertTrue(any("採点応答の検証に失敗" in line for line in lines))

    def test_api_error_falls_back_as_separate_cause(self):
        class RateLimitError(Exception):
            pass

        def judge(*a, **kw):
            raise RateLimitError("rate_limit_error sk-ant-api03-SECRETSECRET")

        with mock.patch.object(ga.daily_state, "note_system_error", return_value=True):
            self.run_plan(judge)
        self.assertEqual(self.cross[0]["cause"], "api_error")
        self.assertNotIn("SECRETSECRET", self.cross[0]["exception"])  # 認証情報らしき文字列は出さない

    def test_fallback_creates_issue(self):
        info = {"stage": "採点", "cause": "score_response_invalid", "exception": "EditorialScoreError: x",
                "candidate_count": 4, "candidates": [], "diagnostics": {"valid": 0, "candidates": 4}}
        posts = []

        def fake_request(method, path, token, payload=None):
            if method == "GET":
                return []
            posts.append(payload)
            return {"html_url": "u"}

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": "2026-10-09", "gate_rejected": [], "planning_fallback": info}, f)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}):
                self.assertEqual(rgr.main(["--report", path], request=fake_request), 0)
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["title"].startswith(rgr.FALLBACK_TITLE_PREFIX))
        self.assertIn("採点応答の検証に失敗", posts[0]["body"])


if __name__ == "__main__":
    unittest.main()
