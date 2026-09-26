# -*- coding: utf-8 -*-
"""公開前監査ゲート(publish_gate.py)と、Daily Articles への組み込みのテスト。
APIは呼ばない(Anthropicクライアント・記事生成・GitHub APIはすべてスタブに差し替える)。
実行: python -m unittest tests/test_publish_gate.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import generate_articles as g  # noqa: E402
import publish_gate as pg  # noqa: E402
import report_gate_rejections as rgr  # noqa: E402


def claim(text, status, role="detail", av="", pv="", url="https://www.city.example.lg.jp/event.html"):
    return {"claim": text, "role": role, "status": status, "basis": "primary_page",
            "articleValue": av, "primaryValue": pv, "logicallyCompatible": status != "contradicted",
            "primaryUrl": url, "note": ""}


def audit_response(claims, quoted=False, needs_human=False):
    return {"primarySources": ["https://www.city.example.lg.jp/event.html"], "claims": claims,
            "hasQuotedComment": quoted, "needsHuman": needs_human, "verdict": "confirmed", "summary": "テスト"}


PASS = audit_response([claim("10月3日開催", "confirmed", role="central"),
                       claim("駐車場なし", "not_found_in_primary")])
FAIL_CORE = audit_response([claim("10月3日開催", "contradicted", role="central", av="10月3日", pv="10月10日"),
                            claim("会場は海岸", "not_found_in_primary", role="dek")])
FAIL_DETAIL_TIME = audit_response([claim("10月3日開催", "confirmed", role="central"),
                                   claim("開始時刻", "contradicted", av="10時開始", pv="11時開始")])


class FakeBlock:
    def __init__(self, inp):
        self.type, self.name, self.input = "tool_use", "submit_audit", inp


class FakeResp:
    def __init__(self, inp):
        self.content, self.stop_reason = [FakeBlock(inp)], "tool_use"


class FakeClient:
    """記事タイトルごとに、監査の応答を順番に返すモック(同じ記事の再監査は次の応答)"""
    def __init__(self, by_title):
        self.by_title = {k: list(v) for k, v in by_title.items()}
        self.calls = []
        self.messages = self

    def create(self, **kw):
        art = json.loads(kw["messages"][0]["content"].split("\n\n", 1)[1])
        self.calls.append(art["title"])
        v = self.by_title[art["title"]].pop(0)
        if isinstance(v, Exception):
            raise v
        return FakeResp(v)


def draft(title, body="イベントは10時開始。会場は海岸。" * 20, **kw):
    d = {"id": "today-run-pending-id", "title": title, "dek": "リード", "area": "藤沢", "cat": "e",
         "link": "https://www.city.example.lg.jp/event.html",
         "sources": ["https://www.city.example.lg.jp/event.html"], "body": body}
    d.update(kw)
    return d


def normalized(resp):
    import fact_audit as fa
    return fa.normalize_result(resp, 1)[0]


class EvaluateTest(unittest.TestCase):
    def test_confirmed_passes_even_with_detail_unverified(self):
        passed, reasons, blocking = pg.evaluate(normalized(PASS))
        self.assertTrue(passed)
        self.assertEqual((reasons, blocking), ([], []))

    def test_contradicted_fails(self):
        passed, reasons, blocking = pg.evaluate(normalized(FAIL_DETAIL_TIME))
        self.assertFalse(passed)
        self.assertEqual(len(blocking), 1)
        self.assertIn("contradicted", reasons[0])

    def test_core_unverified_fails(self):
        r = normalized(audit_response([claim("10月3日開催", "confirmed", role="central"),
                                       claim("会場", "source_unavailable", role="title")]))
        passed, reasons, blocking = pg.evaluate(r)
        self.assertFalse(passed)
        self.assertEqual(blocking[0]["role"], "title")

    def test_quoted_comment_fails(self):
        passed, reasons, _ = pg.evaluate(normalized(dict(PASS, hasQuotedComment=True)))
        self.assertFalse(passed)
        self.assertTrue(any("人物の発言" in r for r in reasons))

    def test_needs_human_fails(self):
        passed, _, _ = pg.evaluate(normalized(dict(PASS, needsHuman=True)))
        self.assertFalse(passed)

    def test_anomaly_or_no_claims_fails(self):
        self.assertFalse(pg.evaluate(normalized("壊れた応答"))[0])
        self.assertFalse(pg.evaluate(normalized(audit_response([])))[0])


class AutofixTest(unittest.TestCase):
    def test_short_numeric_detail_is_fixed(self):
        fixed, fixes = pg.safe_autofix(draft("A", body="開始は10時開始の予定。"), normalized(FAIL_DETAIL_TIME))
        self.assertEqual(fixed["body"], "開始は11時開始の予定。")
        self.assertEqual(fixes[0]["from"], "10時開始")

    def test_core_claim_is_not_fixed(self):
        fixed, _ = pg.safe_autofix(draft("A", body="10月3日に開催。"), normalized(FAIL_CORE))
        self.assertIsNone(fixed)

    def test_ambiguous_or_secondary_media_is_not_fixed(self):
        self.assertIsNone(pg.safe_autofix(draft("A", body="10時開始。10時開始。"), normalized(FAIL_DETAIL_TIME))[0])
        other = audit_response([claim("開始時刻", "contradicted", av="10時開始", pv="11時開始",
                                      url="https://www.townnews.co.jp/x.html")])
        self.assertIsNone(pg.safe_autofix(draft("A", body="10時開始。"), normalized(other))[0])

    def test_text_claim_is_not_fixed(self):
        text = audit_response([claim("創業", "contradicted", av="明治期から続く", pv="昭和創業")])
        self.assertIsNone(pg.safe_autofix(draft("A", body="明治期から続く店。"), normalized(text))[0])

    # Issue #29 の型: 置き換えでは直せない細部の誤り → その1文だけを削除する
    FEE = audit_response([claim("料金", "contradicted",
                                av="映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）",
                                pv="映画鑑賞料（通常上映）一般1,300円・小中学生650円／展示観覧料（特別展）一般500円・小中学生250円（別立て）")])

    def test_unreplaceable_detail_sentence_is_removed(self):
        body = "特別展が開かれている。会期は12月13日まで。\n\n開館は9時から。映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）。月曜休館。"
        fixed, fixes = pg.safe_autofix(draft("A", body=body), normalized(self.FEE))
        self.assertEqual(fixed["body"], "特別展が開かれている。会期は12月13日まで。\n\n開館は9時から。月曜休館。")
        self.assertEqual(fixes[0]["action"], "remove")
        self.assertEqual(fixes[0]["to"], "")
        self.assertEqual(fixed["sources"], draft("A")["sources"])  # 削除では情報源を増やさない

    def test_sentence_not_removed_when_paragraph_would_vanish_or_in_dek(self):
        lone = "特別展が開かれている。\n\n映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）。"
        self.assertIsNone(pg.safe_autofix(draft("A", body=lone), normalized(self.FEE))[0])
        body = "開館は9時から。映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）。月曜休館。"
        in_dek = draft("A", body=body, dek="映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）")
        self.assertIsNone(pg.safe_autofix(in_dek, normalized(self.FEE))[0])

    def test_removal_limit_and_core_claims_are_respected(self):
        many = audit_response([claim(f"細部{i}", "contradicted", av=f"誤り{i}の記述", pv=f"正しい記述{i}ではない長い説明" * 3)
                               for i in range(3)])
        body = "導入。誤り0の記述がある。誤り1の記述がある。誤り2の記述がある。結び。"
        self.assertIsNone(pg.safe_autofix(draft("A", body=body), normalized(many))[0])  # 3文目の削除は上限超え
        mixed = audit_response([claim("料金", "contradicted", av="誤り0の記述", pv="別の長い説明" * 5),
                                claim("会場", "contradicted", role="central", av="海岸", pv="山")])
        self.assertIsNone(pg.safe_autofix(draft("A", body=body), normalized(mixed))[0])

    def test_removal_then_reaudit_pass(self):
        body = "特別展が開かれている。会期は12月13日まで。\n\n開館は9時から。映画観覧料は一般1,300円・小中学生650円（展示観覧料含む）。月曜休館。"
        client = FakeClient({"料金記事": [self.FEE, PASS]})
        gate = pg.check_draft(draft("料金記事", body=body), client=client)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["audits"], 2)
        self.assertNotIn("展示観覧料含む", gate["entry"]["body"])


class CheckDraftTest(unittest.TestCase):
    def test_pass(self):
        gate = pg.check_draft(draft("合格記事"), client=FakeClient({"合格記事": [PASS]}))
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["audits"], 1)

    def test_reject_records_reasons_and_claims(self):
        gate = pg.check_draft(draft("不合格記事"), client=FakeClient({"不合格記事": [FAIL_CORE]}))
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["reasons"])
        self.assertEqual({c["role"] for c in gate["claims"]}, {"central", "dek"})

    def test_autofix_then_reaudit_pass(self):
        client = FakeClient({"修正記事": [FAIL_DETAIL_TIME, PASS]})
        gate = pg.check_draft(draft("修正記事", body="開始は10時開始の予定。" * 30), client=client)
        self.assertFalse(gate["passed"])  # 10時開始が複数回出る → 置換箇所が一意でないので直さない
        client = FakeClient({"修正記事": [FAIL_DETAIL_TIME, PASS]})
        gate = pg.check_draft(draft("修正記事", body="開始は10時開始の予定。" + "海辺の祭り。" * 60), client=client)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["audits"], 2)
        self.assertIn("11時開始", gate["entry"]["body"])
        self.assertEqual(len(client.calls), 2)

    def test_autofix_then_reaudit_fail_is_rejected(self):
        client = FakeClient({"修正記事": [FAIL_DETAIL_TIME, FAIL_DETAIL_TIME]})
        gate = pg.check_draft(draft("修正記事", body="開始は10時開始の予定。"), client=client)
        self.assertFalse(gate["passed"])
        self.assertIn("自動修正後の再監査で不合格", gate["reasons"][0])
        self.assertEqual(gate["entry"]["body"], "開始は10時開始の予定。")  # 不合格なら元のドラフトのまま

    def test_audit_exception_is_rejected(self):
        gate = pg.check_draft(draft("例外記事"), client=FakeClient({"例外記事": [RuntimeError("boom")]}))
        self.assertFalse(gate["passed"])


def news_item(title, body_seed):
    return {
        "cat": "e", "area": "藤沢", "scene": "festival", "title": title, "dek": f"{title}のリード",
        "body": (body_seed * 80)[:1200], "tags": [title], "link": f"https://www.city.example.lg.jp/{abs(hash(title))}.html",
        "subjectNames": [title], "sources": [f"https://www.city.example.lg.jp/{abs(hash(title))}.html"],
        "eventStartDate": "2026-10-10", "eventEndDate": "", "eventSeriesKey": "",
        "address": "", "access": "", "hours": "", "closedDays": "",
        "instagram": "", "facebook": "", "x": "", "tiktok": "",
    }


class DailyIntegrationTest(unittest.TestCase):
    """generate_articles.main() を、生成・監査をスタブにして一時ディレクトリで動かす。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.paths = {k: os.path.join(d, f"{k}.json") for k in ("articles", "counter", "stock", "series", "report")}
        existing = [{"id": 1, "title": "既存記事", "dek": "既存", "area": "鎌倉", "cat": "t", "tags": ["既存"],
                     "date": "2026-01-01", "body": "既存記事の本文。" * 50, "link": "", "subjectNames": ["既存施設"],
                     "slug": "kamakura-tourism-0001"}]
        for k, v in (("articles", existing), ("counter", {"next_id": 10}), ("stock", []), ("series", [])):
            with open(self.paths[k], "w", encoding="utf-8") as f:
                json.dump(v, f, ensure_ascii=False)
        self.patches = [
            mock.patch.object(g, "ARTICLES_JSON_PATH", self.paths["articles"]),
            mock.patch.object(g, "ID_COUNTER_PATH", self.paths["counter"]),
            mock.patch.object(g, "STOCK_TOPICS_PATH", self.paths["stock"]),
            mock.patch.object(g, "EVENT_SERIES_PATH", self.paths["series"]),
            mock.patch.object(g, "RUN_REPORT_PATH", self.paths["report"]),
            mock.patch.object(g, "NEWS_ARTICLES_PER_DAY", 2),
            mock.patch.object(g, "NEWS_REFILL_MAX_ATTEMPTS", 1),
            # 既存テストは news 2件=目標・最低ラインとして動かす(top-up はTopUpTestで確認する)
            mock.patch.object(g, "DAILY_TARGET_ARTICLES", 2),
            mock.patch.object(g, "DAILY_MIN_ARTICLES", 1),
            mock.patch.object(g, "_gate_drafts_checked", 0),
            mock.patch.object(g, "_gate_client", object()),
            mock.patch.object(g, "run_stock_generation", lambda existing, topics, today, log, gate_rejections=None: ([], topics)),
            mock.patch.dict(os.environ, {"PUBLISH_GATE": "on"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def load(self, k):
        with open(self.paths[k], encoding="utf-8") as f:
            return json.load(f)

    def run_main(self, batches, verdicts):
        """batches: call_claude_news が順に返す記事リスト / verdicts: タイトル→合否"""
        calls = iter(batches)

        def fake_check(entry, client=None):
            ok = verdicts[entry["title"]]
            return {"passed": ok, "entry": entry, "verdict": "confirmed" if ok else "fix",
                    "reasons": [] if ok else ["一次情報と矛盾する記述(contradicted)が1件"],
                    "claims": [] if ok else [claim("開催日", "contradicted", role="central", av="10月3日", pv="10月10日")],
                    "summary": "", "primarySources": [], "autofix": [], "audits": 1}

        self.news_calls = []

        def fake_news(recent_titles, event_series=None, count=None):
            self.news_calls.append(count)
            return next(calls)

        with mock.patch.object(g, "call_claude_news", fake_news), \
                mock.patch.object(pg, "check_draft", fake_check):
            g.main()
        return self.load("report")

    def test_passing_articles_published_and_failing_recorded(self):
        report = self.run_main(
            [[news_item("秋の海辺まつり", "海辺のまつりが開かれる。"), news_item("誤りのある記事", "山の上で催しがある。")],
             [news_item("補充でも不合格", "川沿いで行事がある。")]],
            {"秋の海辺まつり": True, "誤りのある記事": False, "補充でも不合格": False},
        )
        titles = [a["title"] for a in self.load("articles")]
        self.assertIn("秋の海辺まつり", titles)
        self.assertNotIn("誤りのある記事", titles)
        self.assertNotIn("補充でも不合格", titles)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["accepted_ids"], [10])  # 公開した記事だけがIDを持つ(不合格分はIDを消費しない)
        self.assertEqual(self.load("counter")["next_id"], 11)
        self.assertEqual([r["title"] for r in report["gate_rejected"]], ["誤りのある記事", "補充でも不合格"])
        self.assertEqual(report["gate_rejected"][0]["claims"][0]["status"], "contradicted")

    def test_refill_replaces_rejected_draft(self):
        report = self.run_main(
            [[news_item("秋の海辺まつり", "海辺のまつりが開かれる。"), news_item("誤りのある記事", "山の上で催しがある。")],
             [news_item("補充で合格", "川沿いで行事がある。")]],
            {"秋の海辺まつり": True, "誤りのある記事": False, "補充で合格": True},
        )
        self.assertEqual(report["accepted_ids"], [10, 11])
        self.assertEqual(len(report["gate_rejected"]), 1)

    def test_all_rejected_publishes_nothing_without_failing(self):
        before = self.load("articles")
        report = self.run_main(
            [[news_item("誤りA", "海辺のまつりが開かれる。"), news_item("誤りB", "山の上で催しがある。")],
             [news_item("誤りC", "川沿いで行事がある。")]],
            {"誤りA": False, "誤りB": False, "誤りC": False},
        )
        self.assertEqual(self.load("articles"), before)
        self.assertEqual(report["status"], "no_articles_passed_gate")
        self.assertEqual(report["accepted_ids"], [])
        self.assertEqual(len(report["gate_rejected"]), 3)
        self.assertEqual(self.load("counter")["next_id"], 10)

    def test_non_gate_shortfall_still_fails(self):
        # 監査の不合格ではなく重複などで件数に届かない場合は、従来どおり失敗させる
        dup = news_item("既存記事", "既存記事の本文。")
        with self.assertRaises(RuntimeError):
            self.run_main([[news_item("秋の海辺まつり", "海辺のまつりが開かれる。"), dup], [dup]],
                          {"秋の海辺まつり": True, "既存記事": True})
        self.assertEqual(self.load("report")["status"], "error")

    # ---- 目標件数への補充(top-up)・最低ライン・上限 ----

    def test_topup_fills_to_target_with_other_candidates(self):
        # news 2件 + stock 0件 → 目標4件に2件不足 → 別候補を生成して監査(不合格分はさらに補充)
        with mock.patch.object(g, "DAILY_TARGET_ARTICLES", 4), mock.patch.object(g, "DAILY_TOPUP_MAX_ATTEMPTS", 2):
            report = self.run_main(
                [[news_item("秋の海辺まつり", "海辺のまつりが開かれる。"), news_item("山の音楽会", "山の上で催しがある。")],
                 [news_item("補充で不合格", "川沿いで行事がある。"), news_item("補充で合格", "駅前で市が立つ。")],
                 [news_item("再補充で合格", "寺で展示がある。")]],
                {"秋の海辺まつり": True, "山の音楽会": True, "補充で不合格": False, "補充で合格": True, "再補充で合格": True},
            )
        self.assertEqual(self.news_calls, [2, 2, 1])
        self.assertEqual(report["accepted_ids"], [10, 11, 12, 13])
        self.assertEqual(report["total_count"], 4)
        self.assertEqual([r["title"] for r in report["gate_rejected"]], ["補充で不合格"])
        self.assertIsNone(report["shortfall"])
        self.assertNotIn("補充で不合格", [a["title"] for a in self.load("articles")])

    def test_topup_is_bounded_and_below_minimum_is_reported(self):
        # 不合格が続いても top-up は上限回数で止まり、最低ライン未達は理由付きで記録される(不合格記事は公開しない)
        with mock.patch.object(g, "DAILY_TARGET_ARTICLES", 4), mock.patch.object(g, "DAILY_MIN_ARTICLES", 3), \
                mock.patch.object(g, "DAILY_TOPUP_MAX_ATTEMPTS", 2):
            report = self.run_main(
                [[news_item("秋の海辺まつり", "海辺のまつりが開かれる。"), news_item("誤りA", "山の上で催しがある。")],
                 [news_item("誤りB", "川沿いで行事がある。")],
                 [news_item("誤りC", "駅前で市が立つ。")],
                 [news_item("誤りD", "寺で展示がある。")],
                 [news_item("上限後の候補", "港で祭りがある。")]],
                {"秋の海辺まつり": True, "誤りA": False, "誤りB": False, "誤りC": False, "誤りD": False, "上限後の候補": True},
            )
        self.assertEqual(self.news_calls, [2, 1, 3, 3])  # 上限後は生成しない
        self.assertEqual(report["accepted_ids"], [10])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["shortfall"]["total"], 1)
        self.assertEqual(report["shortfall"]["min"], 3)
        self.assertTrue(any("不合格 4件" in r for r in report["shortfall"]["reasons"]))
        titles = [a["title"] for a in self.load("articles")]
        self.assertEqual([t for t in titles if t != "既存記事"], ["秋の海辺まつり"])

    def test_gate_budget_stops_generation(self):
        with mock.patch.object(g, "MAX_GATE_DRAFTS_PER_RUN", 3), mock.patch.object(g, "DAILY_TARGET_ARTICLES", 5), \
                mock.patch.object(g, "NEWS_REFILL_MAX_ATTEMPTS", 3):
            report = self.run_main(
                [[news_item("誤りA", "海辺のまつりが開かれる。"), news_item("誤りB", "山の上で催しがある。")],
                 [news_item("誤りC", "川沿いで行事がある。"), news_item("誤りD", "駅前で市が立つ。")],
                 [news_item("上限後の候補", "寺で展示がある。")]],
                {"誤りA": False, "誤りB": False, "誤りC": False, "誤りD": True, "上限後の候補": True},
            )
        self.assertEqual(self.news_calls, [2, 2])
        self.assertEqual(g._gate_drafts_checked, 3)
        # 上限を超えた「誤りD」は監査していないので公開もIssue化もしない
        self.assertEqual([r["title"] for r in report["gate_rejected"]], ["誤りA", "誤りB", "誤りC"])
        self.assertEqual(report["accepted_ids"], [])
        self.assertEqual(report["status"], "no_articles_passed_gate")


class StockGateTest(unittest.TestCase):
    def test_rejected_stock_topic_is_skipped_and_consumes_no_id(self):
        with tempfile.TemporaryDirectory() as d:
            counter = os.path.join(d, "counter.json")
            with open(counter, "w") as f:
                json.dump({"next_id": 50}, f)
            topics = [{"id": "t1", "query": "鎌倉 紫陽花", "status": "candidate"},
                      {"id": "t2", "query": "茅ヶ崎 海", "status": "candidate"}]
            art = lambda t, s: {"cat": "t", "area": "鎌倉" if t == "紫陽花の寺" else "茅ヶ崎", "scene": "garden",
                                "title": t, "dek": "リード", "body": (s * 80)[:900], "tags": [t], "link": "",
                                "subjectNames": [t], "sources": ["https://www.city.example.lg.jp/a.html"]}
            decisions = [{"topicId": "t1", "decision": "write", "article": art("紫陽花の寺", "紫陽花が咲く。")},
                         {"topicId": "t2", "decision": "write", "article": art("海の散歩道", "海沿いを歩く。")}]

            def fake_check(entry, client=None):
                ok = entry["title"] == "海の散歩道"
                return {"passed": ok, "entry": entry, "verdict": "confirmed" if ok else "review_required",
                        "reasons": [] if ok else ["骨格の記述が一次情報で未確認"], "claims": [], "summary": "",
                        "primarySources": [], "autofix": [], "audits": 1}

            rejections, log = [], []
            with mock.patch.object(g, "ID_COUNTER_PATH", counter), \
                    mock.patch.object(g, "refill_stock_topics_if_needed", lambda e, t, l: (t, False)), \
                    mock.patch.object(g, "call_claude_stock_selection", lambda *a: decisions), \
                    mock.patch.object(g, "_gate_client", object()), \
                    mock.patch.object(pg, "check_draft", fake_check), \
                    mock.patch.dict(os.environ, {"PUBLISH_GATE": "on"}):
                accepted, topics = g.run_stock_generation([], topics, "2026-09-25", log, gate_rejections=rejections)
            self.assertEqual([a["title"] for a in accepted], ["海の散歩道"])
            self.assertEqual(accepted[0]["id"], 50)
            self.assertEqual(accepted[0]["slug"], "chigasaki-tourism-0050")
            self.assertEqual(topics[0]["status"], "skipped")
            self.assertIn("公開前監査で不合格", topics[0]["skipReason"])
            self.assertEqual(topics[1]["status"], "generated")
            self.assertEqual(rejections[0]["articleType"], "stock")


class ReportIssuesTest(unittest.TestCase):
    def write_report(self, d, rejected):
        path = os.path.join(d, "report.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"status": "ok", "date": "2026-09-25", "accepted_ids": [10], "gate_rejected": rejected}, f,
                      ensure_ascii=False)
        return path

    def rejected(self, title="誤りのある記事"):
        return pg.rejection_record(draft(title), {
            "verdict": "fix", "reasons": ["一次情報と矛盾する記述(contradicted)が1件"],
            "claims": [claim("開催日", "contradicted", role="central", av="10月3日", pv="10月10日")],
            "summary": "開催日が違う", "primarySources": [], "autofix": []}, "news")

    def test_creates_issue_with_reason_and_claims(self):
        calls = []

        def fake_request(method, path, token, payload=None):
            calls.append((method, path, payload))
            return [] if method == "GET" else {"html_url": "https://github.com/x/y/issues/1"}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"}):
            self.assertEqual(rgr.main(["--report", self.write_report(d, [self.rejected()])], request=fake_request), 0)
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/repos/owner/repo/issues")
        self.assertIn("誤りのある記事", posts[0][2]["title"])
        body = posts[0][2]["body"]
        for s in ("公開していません", "contradicted", "10月3日", "10月10日", "開催日"):
            self.assertIn(s, body)

    def test_skips_existing_open_issue(self):
        title = rgr.issue_title(self.rejected(), "2026-09-25")
        calls = []

        def fake_request(method, path, token, payload=None):
            calls.append(method)
            return [{"title": title}] if method == "GET" else {}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"}):
            rgr.main(["--report", self.write_report(d, [self.rejected()])], request=fake_request)
        self.assertNotIn("POST", calls)

    def test_shortfall_creates_one_issue_with_reasons(self):
        calls = []

        def fake_request(method, path, token, payload=None):
            calls.append((method, payload))
            return [] if method == "GET" else {}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "owner/repo"}):
            path = os.path.join(d, "report.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"status": "ok", "date": "2026-09-26", "accepted_ids": [10],
                           "gate_rejected": [self.rejected()],
                           "shortfall": {"total": 1, "min": 3, "target": 5,
                                         "reasons": ["公開前監査で不合格 1件(品質基準は緩めずに見送り)"]}},
                          f, ensure_ascii=False)
            rgr.main(["--report", path], request=fake_request)
        posts = [p for m, p in calls if m == "POST"]
        self.assertEqual(len(posts), 2)  # 不合格記事1件 + 最低件数未達1件
        self.assertEqual(posts[1]["title"], rgr.shortfall_title("2026-09-26"))
        for s in ("1件", "最低ライン 3件", "目標 5件", "品質基準", "誤りのある記事"):
            self.assertIn(s, posts[1]["body"])

    def test_no_rejections_and_dry_run_make_no_requests(self):
        def boom(*a, **kw):
            raise AssertionError("GitHub APIを呼んではいけない")

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rgr.main(["--report", self.write_report(d, [])], request=boom), 0)
            self.assertEqual(rgr.main(["--report", self.write_report(d, [self.rejected()]), "--dry-run"], request=boom), 0)
            self.assertEqual(rgr.main(["--report", os.path.join(d, "missing.json")], request=boom), 0)


if __name__ == "__main__":
    unittest.main()
