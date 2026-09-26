# -*- coding: utf-8 -*-
"""Daily Articles の PR 経由運用(記事単位の保留・保留Issue・Approve/Reject・当日PR・自動マージ条件・X投稿ログ)のテスト。
APIは呼ばない(Anthropicクライアント・GitHub API・URL確認はすべてスタブに差し替える)。Xへの実投稿もしない。
実行: python -m unittest tests/test_daily_pipeline.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import daily_fact_audit as dfa  # noqa: E402
import daily_pr as dpr  # noqa: E402
import fact_audit as fa  # noqa: E402
import report_daily_holds as rdh  # noqa: E402
import resolve_daily_hold as rdhold  # noqa: E402
import verify_daily_run as vdr  # noqa: E402
import x_post_log_store as xls  # noqa: E402
from test_publish_gate import FakeClient, audit_response, claim  # noqa: E402

_real_http_get = fa._http_get


def setUpModule():
    def _no_network(url, limit):
        raise OSError("テストでは公式ページの画像を取得しない")
    fa._http_get = _no_network


def tearDownModule():
    fa._http_get = _real_http_get


CONFIRMED = audit_response([claim("10月3日開催", "confirmed", role="central")])
CORE_UNVERIFIED = audit_response([claim("10月3日開催", "confirmed", role="central"),
                                  claim("会場は海岸", "not_found_in_primary", role="dek")])
DETAIL_TIME = audit_response([claim("10月3日開催", "confirmed", role="central"),
                              claim("開始時刻", "contradicted", av="10時開始", pv="11時開始")])
CORE_CONTRADICTED = audit_response([claim("主催", "contradicted", role="central", av="藤沢市主催", pv="民間団体の主催")])


def article(i, title, atype="news", **kw):
    a = {"id": i, "slug": f"fujisawa-event-{i:04d}", "title": title, "dek": "リード", "area": "藤沢", "cat": "e",
         "articleType": atype, "date": "2026-09-26", "link": "https://www.city.example.lg.jp/event.html",
         "sources": ["https://www.city.example.lg.jp/event.html"],
         "body": "イベントは10時開始。会場は海岸。" * 5, "tags": [title]}
    a.update(kw)
    return a


def no_fetch(url):
    return "公式ページ: イベントは11時開始です。"


class Workspace:
    """articles.json / stock_topics.json / event_series.json / 実行レポートを一時ディレクトリに置く"""
    def __init__(self, articles, topics=None, series=None, report=None):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.paths = {k: os.path.join(d, f"{k}.json") for k in ("articles", "topics", "series", "report", "holds")}
        self.write("articles", articles)
        self.write("topics", topics or [])
        self.write("series", series or [])
        self.write("report", report or {})
        self.out_dir = os.path.join(d, "audit_reports")

    def write(self, key, data):
        with open(self.paths[key], "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def read(self, key):
        with open(self.paths[key], encoding="utf-8") as f:
            return json.load(f)

    def argv(self):
        return ["--report", self.paths["report"], "--holds-out", self.paths["holds"], "--out-dir", self.out_dir,
                "--articles", self.paths["articles"], "--stock-topics", self.paths["topics"],
                "--event-series", self.paths["series"]]


def run_report(articles, **kw):
    r = {"status": "ok", "date": "2026-09-26", "accepted_ids": [a["id"] for a in articles],
         "accepted_slugs": [a["slug"] for a in articles], "gate_rejected": [], "shortfall": None}
    r.update(kw)
    return r


class DailyFactAuditTest(unittest.TestCase):
    def run_audit(self, ws, client, base_keys=frozenset()):
        self.addCleanup(ws.dir.cleanup)
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        with mock.patch.dict(os.environ, env, clear=True):
            return dfa.main(ws.argv(), client=client, fetcher=no_fetch, image_fetcher=lambda c: None,
                            base_series_keys=set(base_keys))

    def test_only_non_confirmed_article_is_held_and_others_are_published(self):
        # 5件中 C だけ review_required → C だけ保留、A/B/D/E は公開対象のまま
        existing = article(1, "既存記事", eventSeriesKey="old-fes")
        new = [article(101, "A"), article(102, "B"), article(103, "C", atype="stock", cat="t"),
               article(104, "D", eventSeriesKey="new-fes-d"), article(105, "E")]
        new[2]["eventSeriesKey"] = "new-fes-c"
        topics = [{"id": "t0100", "status": "generated", "generatedArticleId": 103, "query": "q"},
                  {"id": "t0101", "status": "candidate", "generatedArticleId": None, "query": "q2"}]
        series = [{"seriesKey": "old-fes", "canonicalName": "既存記事"},
                  {"seriesKey": "new-fes-c", "canonicalName": "C"},
                  {"seriesKey": "new-fes-d", "canonicalName": "D"}]
        ws = Workspace([existing] + new, topics, series,
                       run_report(new, news_ids=[101, 102, 104, 105], stock_ids=[103]))
        client = FakeClient({"A": [CONFIRMED], "B": [CONFIRMED], "C": [CORE_UNVERIFIED], "D": [CONFIRMED],
                             "E": [CONFIRMED]})
        self.assertEqual(self.run_audit(ws, client, base_keys={"old-fes"}), 0)

        self.assertEqual([a["id"] for a in ws.read("articles")], [1, 101, 102, 104, 105])
        report = ws.read("report")
        self.assertEqual(report["accepted_ids"], [101, 102, 104, 105])
        self.assertEqual(report["pre_audit_accepted_ids"], [101, 102, 103, 104, 105])
        self.assertEqual((report["news_count"], report["stock_count"], report["stock_ids"]), (4, 0, []))
        self.assertEqual([h["id"] for h in report["held"]], [103])
        self.assertTrue(report["fact_audit"]["all_published_confirmed"])
        self.assertIsNone(report["shortfall"])  # 4件 >= 最低3件
        t = {x["id"]: x for x in ws.read("topics")}
        self.assertEqual(t["t0100"]["status"], "held")
        self.assertIn("Fact Audit で保留", t["t0100"]["holdReason"])
        self.assertEqual(t["t0101"]["status"], "candidate")
        # C だけが新規登録したシリーズは消し、main にあったもの・公開対象のものは残す
        self.assertEqual([s["seriesKey"] for s in ws.read("series")], ["old-fes", "new-fes-d"])
        holds = ws.read("holds")
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["entry"]["slug"], "fujisawa-event-0103")
        self.assertEqual(holds[0]["verdict"], "review_required")
        self.assertEqual(holds[0]["stockTopicId"], "t0100")
        self.assertTrue(any(f.endswith(".jsonl") for f in os.listdir(ws.out_dir)))

    def test_uniquely_fixable_error_is_autofixed_and_verified_without_human(self):
        a = article(101, "A", body="開始は10時開始の予定。会場は海岸。")
        ws = Workspace([a], report=run_report([a]))
        client = FakeClient({"A": [DETAIL_TIME]})
        self.run_audit(ws, client)
        published = ws.read("articles")
        self.assertEqual(published[0]["body"], "開始は11時開始の予定。会場は海岸。")
        report = ws.read("report")
        self.assertEqual(report["accepted_ids"], [101])
        self.assertEqual(report["fact_audit"]["autofixed_ids"], [101])
        self.assertEqual(client.calls, ["A"])  # verify はコード判定で済み、full の再監査はしない
        self.assertTrue(any(f.startswith("fact_verify_") for f in os.listdir(ws.out_dir)))

    def test_below_minimum_publishes_only_confirmed_and_reports_shortfall(self):
        arts = [article(101, "A"), article(102, "B"), article(103, "C")]
        ws = Workspace(arts, report=run_report(arts))
        client = FakeClient({"A": [CONFIRMED], "B": [CORE_CONTRADICTED], "C": [RuntimeError("API error")]})
        self.run_audit(ws, client)
        report = ws.read("report")
        self.assertEqual(report["accepted_ids"], [101])
        self.assertEqual(report["shortfall"]["total"], 1)
        self.assertTrue(any("保留" in r for r in report["shortfall"]["reasons"]))
        holds = {h["id"]: h for h in ws.read("holds")}
        self.assertEqual(set(holds), {102, 103})
        self.assertTrue(holds[103]["anomalies"])  # 監査できなかった記事も公開しない(fail-closed)

    def test_no_articles_is_noop(self):
        ws = Workspace([], report={"status": "no_articles_passed_gate", "accepted_ids": []})
        self.assertEqual(self.run_audit(ws, FakeClient({})), 0)
        self.assertEqual(ws.read("holds"), [])
        self.assertTrue(ws.read("report")["fact_audit"]["all_published_confirmed"])

    def test_separate_holds_without_git_base_uses_title(self):
        held = [{"id": 2, "entry": article(2, "C", eventSeriesKey="k"), "reasons": ["x"]}]
        _a, _t, series = dfa.separate_holds([article(2, "C")], [], [{"seriesKey": "k", "canonicalName": "C"},
                                                                     {"seriesKey": "k2", "canonicalName": "X"}],
                                            held, [], None)
        self.assertEqual([s["seriesKey"] for s in series], ["k2"])


def hold(**kw):
    h = {"id": 103, "slug": "fujisawa-event-0103", "title": "C", "articleType": "news", "area": "藤沢", "cat": "e",
         "date": "2026-09-26", "verdict": "review_required", "reasons": ["骨格の記述が一次情報で未確認・確認不能1件"],
         "claims": [dict(claim("会場は海岸", "not_found_in_primary", role="dek", av="海岸"), imageReading="not_applicable")],
         "summary": "会場を確認できず", "primarySources": ["https://www.city.example.lg.jp/event.html"],
         "autofix": [], "escalation": {"kind": "core_unverified", "reason": "重要な記述が公式情報で確認できない"},
         "entry": article(103, "C"), "stockTopicId": None}
    h.update(kw)
    return h


class ReportDailyHoldsTest(unittest.TestCase):
    def test_issue_has_required_sections_and_payload_roundtrip(self):
        body = rdh.issue_body(hold(), "https://github.com/o/r/actions/runs/1")
        for label in ("対象トピック:", "問題箇所:", "AI判断:", "確認してほしい内容:", "公式ソースURL:",
                      "[Approve]", "[Reject]", "@claude Approve", "@claude Reject"):
            self.assertIn(label, body)
        self.assertIn("https://www.city.example.lg.jp/event.html", body)
        payload = rdh.decode_payload(body)
        self.assertEqual(payload["entry"]["slug"], "fujisawa-event-0103")
        self.assertEqual(payload["entry"]["body"], article(103, "C")["body"])

    def test_image_hold_shows_direct_image_link_and_value_option(self):
        checks = [{"claim": "開催日", "articleValue": "10月3日", "imageUrl": "https://www.city.example.lg.jp/flyer.png",
                   "pageUrl": "https://www.city.example.lg.jp/event.html", "candidate": "10月8日?"}]
        body = rdh.issue_body(hold(imageChecks=checks, escalation={"kind": "image_reading"}))
        self.assertIn("公式画像(直リンク): https://www.city.example.lg.jp/flyer.png", body)
        self.assertIn("正しい値:", body)
        self.assertIn("10月8日?", body)

    def test_creates_one_issue_per_hold_and_skips_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "holds.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([hold(), hold(id=104, title="D")], f, ensure_ascii=False)
            calls = []

            def request(method, p, token, payload=None):
                calls.append((method, p, payload))
                if method == "GET":
                    return [{"title": rdh.issue_title(hold())}]
                return {"html_url": "u"}
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}):
                self.assertEqual(rdh.main(["--holds", path], request=request), 0)
            posts = [c for c in calls if c[0] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertTrue(posts[0][2]["title"].startswith("[Daily Articles 保留] D"))

    def test_dry_run_and_no_holds_make_no_requests(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "holds.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([hold()], f, ensure_ascii=False)
            request = mock.Mock()
            self.assertEqual(rdh.main(["--holds", path, "--dry-run"], request=request), 0)
            self.assertEqual(rdh.main(["--holds", os.path.join(d, "none.json")], request=request), 0)
            request.assert_not_called()


class ResolveDailyHoldTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace([article(1, "既存の別記事", link="https://other.example.jp/", sources=[],
                                     body="まったく別の内容の記事本文。" * 5, tags=["別"])],
                            topics=[{"id": "t0100", "status": "held", "generatedArticleId": 103, "query": "q",
                                     "holdReason": "x", "heldAt": "y"}])
        self.conf = os.path.join(self.ws.dir.name, "conf.json")
        self.addCleanup(self.ws.dir.cleanup)

    def resolve(self, decision, responses, payload=None, value=""):
        client = FakeClient({"C": responses})
        return rdhold.resolve(payload or hold(entry=article(103, "C", eventSeriesKey="c-fes")), decision, value,
                              issue="9", client=client, fetcher=no_fetch, image_fetcher=lambda c: None,
                              articles_path=self.ws.paths["articles"], stock_topics_path=self.ws.paths["topics"],
                              event_series_path=self.ws.paths["series"], confirmations_path=self.conf)

    def test_approve_confirmed_publishes_same_id_and_slug(self):
        code, msg, entry = self.resolve("approve", [CONFIRMED])
        self.assertEqual(code, 0, msg)
        arts = self.ws.read("articles")
        self.assertEqual([a["id"] for a in arts], [1, 103])
        self.assertEqual(arts[1]["slug"], "fujisawa-event-0103")
        self.assertEqual(self.ws.read("topics")[0]["status"], "generated")
        self.assertNotIn("holdReason", self.ws.read("topics")[0])
        self.assertEqual([s["seriesKey"] for s in self.ws.read("series")], ["c-fes"])

    def test_approve_publishes_when_only_owner_confirmed_points_remain(self):
        code, msg, _ = self.resolve("approve", [CORE_UNVERIFIED])
        self.assertEqual(code, 0, msg)
        self.assertEqual(len(self.ws.read("articles")), 2)

    def test_approve_with_remaining_contradiction_does_not_publish(self):
        code, msg, _ = self.resolve("approve", [CORE_CONTRADICTED])
        self.assertEqual(code, 3)
        self.assertIn("矛盾", msg)
        self.assertEqual(len(self.ws.read("articles")), 1)
        self.assertEqual(self.ws.read("topics")[0]["status"], "held")

    def test_approve_with_audit_exception_does_not_publish(self):
        code, _msg, _ = self.resolve("approve", [RuntimeError("API error")])
        self.assertEqual(code, 3)
        self.assertEqual(len(self.ws.read("articles")), 1)

    def test_reject_keeps_article_unpublished_and_skips_topic(self):
        code, _msg, entry = self.resolve("reject", [])
        self.assertEqual((code, entry), (0, None))
        self.assertEqual(len(self.ws.read("articles")), 1)
        t = self.ws.read("topics")[0]
        self.assertEqual(t["status"], "skipped")
        self.assertIn("Issue #9", t["skipReason"])

    def test_already_published_or_broken_payload_is_rejected(self):
        self.ws.write("articles", [article(103, "C")])
        self.assertEqual(self.resolve("approve", [CONFIRMED])[0], 2)
        self.assertEqual(self.resolve("approve", [], payload={"entry": {"title": "x"}})[0], 2)

    def test_load_payload_requires_hold_issue_title(self):
        body = rdh.issue_body(hold())
        args = types.SimpleNamespace(payload_file="", article_id=0, issue="9")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}):
            ok = rdhold.load_payload(args, request=lambda *a, **k: {"title": rdh.issue_title(hold()), "body": body})
            ng = rdhold.load_payload(args, request=lambda *a, **k: {"title": "別のIssue", "body": body})
        self.assertEqual(ok["id"], 103)
        self.assertIsNone(ng)


def pr(**kw):
    p = {"number": 7, "state": "open", "draft": False, "mergeable": True, "merged_at": None,
         "base": {"ref": "main"}, "user": {"login": "github-actions[bot]"},
         "head": {"ref": "daily/2026-09-26", "sha": "abc", "repo": {"full_name": "o/r"}}, "body": ""}
    p.update(kw)
    return p


META = {"date": "2026-09-26", "branch": "daily/2026-09-26", "published_ids": [101, 102], "published_slugs": ["a", "b"],
        "held_ids": [103], "all_published_confirmed": True, "pr": 7}
FILES = ["data/articles.json", "data/id_counter.json", "articles/a/index.html", "sitemap.xml", "index.html"]


class MergeDecisionTest(unittest.TestCase):
    def blockers(self, meta=META, p=None, sha="abc", ids=(1, 101, 102), files=FILES, ci="success"):
        return dpr.merge_blockers(meta, p or pr(), "o/r", sha, list(ids), files, ci)

    def test_all_conditions_met_allows_merge(self):
        self.assertEqual(self.blockers(), [])

    def test_held_article_is_not_a_blocker_once_separated(self):
        # 保留記事(103)が articles.json に無ければ、confirmed 記事だけの PR はマージしてよい
        self.assertEqual(self.blockers(meta=dict(META, held_ids=[103, 104])), [])

    def test_each_failed_condition_blocks(self):
        cases = {
            "CI": dict(ci="failure"),
            "confirmed": dict(meta=dict(META, all_published_confirmed=False)),
            "保留記事": dict(ids=(101, 102, 103)),
            "公開対象の記事": dict(ids=(101,)),
            "元データ・生成物以外": dict(files=FILES + [".github/workflows/ci.yml"]),
            "検証したコミット": dict(sha="other"),
            "競合": dict(p=pr(mergeable=False)),
            "作成者": dict(p=pr(user={"login": "someone"})),
            "open": dict(p=pr(state="closed")),
            "当日branch": dict(p=pr(head={"ref": "daily/2026-09-26", "sha": "abc", "repo": {"full_name": "fork/r"}})),
            "変更ファイルがない": dict(files=[]),
        }
        for word, kw in cases.items():
            with self.subTest(word):
                b = self.blockers(**kw)
                self.assertTrue(b and any(word in r for r in b), b)

    def test_decide_mode(self):
        self.assertEqual(dpr.decide_mode([], False), ("fresh", None))
        self.assertEqual(dpr.decide_mode([], True), ("resume", None))
        self.assertEqual(dpr.decide_mode([pr()], True)[0], "resume")
        self.assertEqual(dpr.decide_mode([pr(state="closed", merged_at="t")], True)[0], "done")
        self.assertEqual(dpr.decide_mode([pr(state="closed")], True)[0], "closed")

    def test_ci_state(self):
        self.assertEqual(dpr.ci_state([]), "none")
        self.assertEqual(dpr.ci_state([{"status": "in_progress"}]), "pending")
        self.assertEqual(dpr.ci_state([{"status": "completed", "conclusion": "failure"},
                                       {"status": "completed", "conclusion": "success"}]), "success")
        old_fail = [{"status": "completed", "conclusion": "failure", "created_at": "2026-09-26T00:00:00Z"}]
        self.assertEqual(dpr.ci_state(old_fail), "failure")
        self.assertEqual(dpr.ci_state(old_fail, since="2026-09-26T01:00:00Z"), "none")

    def test_meta_roundtrip_in_pr_body_and_commit_message(self):
        self.assertEqual(dpr.decode_meta(dpr.pr_body(META)), META)
        self.assertEqual(dpr.decode_meta("chore: x\n\n" + dpr.encode_meta(META) + "\n"), META)
        self.assertIsNone(dpr.decode_meta("なし"))

    def test_meta_from_report(self):
        m = dpr.meta_from_report({"accepted_ids": [1], "accepted_slugs": ["s"], "held": [{"id": 2}],
                                  "fact_audit": {"verdicts": {"1": "confirmed"}, "all_published_confirmed": True}},
                                 "d", "daily/d")
        self.assertEqual((m["published_ids"], m["held_ids"], m["all_published_confirmed"]), ([1], [2], True))
        # Fact Audit の記録が無ければ confirmed とはみなさない
        self.assertFalse(dpr.meta_from_report({"accepted_ids": [1]}, "d", "daily/d")["all_published_confirmed"])


class FakeGitHub(dpr.GitHub):
    def __init__(self, routes):
        self.calls = []
        super().__init__("o/r", "t", self._route)
        self.routes = routes

    def _route(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        for (m, prefix), value in self.routes.items():
            if m == method and path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value(payload) if callable(value) else value
        return None


class DailyPrCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meta_path = os.path.join(self.tmp.name, "meta.json")
        self.patches = [mock.patch.object(dpr, "META_PATH", self.meta_path),
                        mock.patch.dict(os.environ, {"GITHUB_OUTPUT": os.path.join(self.tmp.name, "out"),
                                                     "GITHUB_STEP_SUMMARY": os.path.join(self.tmp.name, "sum")})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def output(self):
        with open(os.environ["GITHUB_OUTPUT"], encoding="utf-8") as f:
            return f.read()

    def test_plan_fresh_and_resume_reuses_existing_pr(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls?"): [], ("GET", "/repos/o/r/commits/"): None})
        self.assertEqual(dpr.cmd_plan(gh, "2026-09-26"), 0)
        self.assertIn("mode=fresh", self.output())
        gh = FakeGitHub({("GET", "/repos/o/r/pulls?"): [pr(body=dpr.pr_body(META))],
                         ("GET", "/repos/o/r/commits/"): {"commit": {"message": ""}}})
        self.assertEqual(dpr.cmd_plan(gh, "2026-09-26"), 0)
        self.assertIn("mode=resume", self.output())
        self.assertEqual(dpr.load_meta()["published_ids"], [101, 102])
        self.assertEqual(dpr.load_meta()["pr"], 7)

    def test_open_reuses_existing_pr_instead_of_creating_duplicate(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls?"): [pr()]})
        dpr.cmd_open(gh, dict(META, pr=None))
        self.assertFalse([c for c in gh.calls if c[0] == "POST"])
        gh = FakeGitHub({("GET", "/repos/o/r/pulls?"): [], ("POST", "/repos/o/r/pulls"): {"number": 8}})
        dpr.cmd_open(gh, dict(META, pr=None))
        post = [c for c in gh.calls if c[0] == "POST"][0]
        self.assertEqual((post[2]["head"], post[2]["base"]), ("daily/2026-09-26", "main"))
        self.assertIsNotNone(dpr.decode_meta(post[2]["body"]))

    def test_ci_dispatches_and_waits_for_success(self):
        runs = iter([{"workflow_runs": []}, {"workflow_runs": [{"head_sha": "abc", "status": "in_progress"}]},
                     {"workflow_runs": [{"head_sha": "abc", "status": "completed", "conclusion": "success"}]}])
        gh = FakeGitHub({("GET", "/repos/o/r/pulls/7"): pr(), ("GET", "/repos/o/r/actions/"): lambda _p: next(runs),
                         ("POST", "/repos/o/r/actions/workflows/ci.yml/dispatches"): None})
        self.assertEqual(dpr.cmd_ci(gh, META, sleep=lambda s: None), 0)
        self.assertTrue([c for c in gh.calls if c[0] == "POST" and "dispatches" in c[1]])

    def test_ci_failure_blocks(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls/7"): pr(),
                         ("GET", "/repos/o/r/actions/"): {"workflow_runs": [
                             {"head_sha": "abc", "status": "in_progress"}]}})
        runs = iter([{"workflow_runs": [{"head_sha": "abc", "status": "in_progress"}]},
                     {"workflow_runs": [{"head_sha": "abc", "status": "completed", "conclusion": "failure"}]}])
        gh.routes[("GET", "/repos/o/r/actions/")] = lambda _p: next(runs)
        self.assertEqual(dpr.cmd_ci(gh, META, sleep=lambda s: None), 1)

    def merge_gh(self, **pr_kw):
        return FakeGitHub({
            ("GET", "/repos/o/r/pulls/7/files"): [{"filename": f} for f in FILES],
            ("GET", "/repos/o/r/pulls/7"): pr(**pr_kw),
            ("GET", "/repos/o/r/actions/"): {"workflow_runs": [
                {"head_sha": "abc", "status": "completed", "conclusion": "success"}]},
            ("GET", "/repos/o/r/issues?"): [],
            ("PUT", "/repos/o/r/pulls/7/merge"): {"sha": "merged"},
            ("POST", "/repos/o/r/issues"): {"html_url": "u"},
        })

    def test_merge_when_all_conditions_pass(self):
        gh = self.merge_gh()
        with mock.patch.object(dpr, "local_head", return_value="abc"), \
                mock.patch.object(dpr, "local_article_ids", return_value=[1, 101, 102]):
            self.assertEqual(dpr.cmd_merge(gh, META, sleep=lambda s: None), 0)
        put = [c for c in gh.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2]["sha"], "abc")

    def test_merge_blocked_creates_issue_and_does_not_merge(self):
        gh = self.merge_gh()
        with mock.patch.object(dpr, "local_head", return_value="abc"), \
                mock.patch.object(dpr, "local_article_ids", return_value=[1, 101, 102, 103]):
            self.assertEqual(dpr.cmd_merge(gh, META, sleep=lambda s: None), 1)
        self.assertFalse([c for c in gh.calls if c[0] == "PUT"])
        issue = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")][0]
        self.assertIn("保留記事", issue[2]["body"])

    def test_merge_api_error_is_reported(self):
        gh = self.merge_gh()
        gh.routes[("PUT", "/repos/o/r/pulls/7/merge")] = urllib.error.HTTPError("u", 405, "x", {}, None)
        with mock.patch.object(dpr, "local_head", return_value="abc"), \
                mock.patch.object(dpr, "local_article_ids", return_value=[1, 101, 102]):
            self.assertEqual(dpr.cmd_merge(gh, META, sleep=lambda s: None), 1)

    def test_published_outputs_only_live_articles_after_merge(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls/7"): pr(merged_at="t", merge_commit_sha="m"),
                         ("GET", "/repos/o/r/compare/"): {"status": "identical"}})
        with mock.patch.object(dpr, "PUBLISH_TIMEOUT_SEC", 0):
            code = dpr.cmd_published(gh, META, check=lambda url: url.endswith("/a/"), sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertIn("article_ids=101\n", self.output())

    def test_published_requires_merge(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls/7"): pr()})
        self.assertEqual(dpr.cmd_published(gh, META, check=lambda url: True), 1)
        self.assertIn("article_ids=\n", self.output())

    def test_newly_added_ignores_existing_and_merged(self):
        before = [article(1, "x")]
        after = before + [article(2, "y"), article(3, "z", mergedInto="fujisawa-event-0001")]
        self.assertEqual(dpr.newly_added(before, after), [(2, "fujisawa-event-0002")])

    def test_wait_published_times_out(self):
        t = iter(range(0, 10000, 100))
        live = dpr.wait_published([1, 2], ["a", "b"], check=lambda u: "/a/" in u, sleep=lambda s: None,
                                  clock=lambda: next(t), timeout=250)
        self.assertEqual(live, [1])


class XPostLogStoreTest(unittest.TestCase):
    def test_merge_logs_is_union_keeping_first_post(self):
        a = {"posts": [{"article_id": 1, "tweet_id": "1", "posted_at": "2026-09-01T00:00:00Z"}]}
        b = {"posts": [{"article_id": 1, "tweet_id": "9", "posted_at": "2026-09-02T00:00:00Z"},
                       {"article_id": 2, "tweet_id": "2", "posted_at": "2026-09-03T00:00:00Z"}]}
        merged = xls.merge_logs(a, b)
        self.assertEqual([(r["article_id"], r["tweet_id"]) for r in merged["posts"]], [(1, "1"), (2, "2")])
        self.assertEqual(xls.merge_logs(None, {"posts": [{"x": 1}]}), {"posts": []})


class VerifyHoldsExcludedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "data"))
        with open(os.path.join(self.tmp.name, "sitemap.xml"), "w", encoding="utf-8") as f:
            f.write("<loc>https://www.shonandoors.com/articles/fujisawa-event-0101/</loc>")
        self.p = mock.patch.object(vdr, "ROOT", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def write_articles(self, ids):
        with open(os.path.join(self.tmp.name, "data", "articles.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": i} for i in ids], f)

    def test_held_article_absent_passes(self):
        self.write_articles([101])
        vdr.verify_holds_excluded([{"id": 103, "slug": "fujisawa-event-0103"}])

    def test_held_article_in_articles_json_or_sitemap_fails(self):
        self.write_articles([101, 103])
        with self.assertRaises(SystemExit):
            vdr.verify_holds_excluded([{"id": 103, "slug": "fujisawa-event-0103"}])
        self.write_articles([])
        with self.assertRaises(SystemExit):
            vdr.verify_holds_excluded([{"id": 101, "slug": "fujisawa-event-0101"}])


WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
# GitHub App では .github/workflows を変更できないため、適用待ちの workflow は pending-workflows/ に置く。
# オーナーが .github/workflows へ反映して pending-workflows/ を消すまでは、そちらを優先して確認する。
PENDING = os.path.join(ROOT, "pending-workflows")


def workflow_names():
    names = set(os.listdir(WORKFLOWS))
    if os.path.isdir(PENDING):
        names |= set(os.listdir(PENDING))
    return sorted(n for n in names if n.endswith((".yml", ".yaml")))


def read_workflow(name):
    path = os.path.join(PENDING, name)
    if not os.path.exists(path):
        path = os.path.join(WORKFLOWS, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


class WorkflowTest(unittest.TestCase):
    def test_workflows_are_valid_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML が無い環境では構文確認をスキップ")
        for name in workflow_names():
            with self.subTest(name):
                try:
                    data = yaml.safe_load(read_workflow(name))
                except yaml.YAMLError as e:
                    self.fail(f"{name}: {e}")
                self.assertIn("jobs", data)
                self.assertIn(True if True in data else "on", data)  # PyYAML は on を True と読む

    def test_no_workflow_pushes_to_main_directly(self):
        for name in workflow_names():
            text = read_workflow(name)
            with self.subTest(name):
                self.assertNotIn("git pull --rebase origin main", text)
                for line in text.splitlines():
                    s = line.strip()
                    if s.startswith("git push"):
                        self.assertIn("refs/heads/${{ steps.plan.outputs.branch }}", s, s)

    def test_daily_articles_saves_event_series_and_uses_pr_flow(self):
        text = read_workflow("daily-articles.yml")
        self.assertIn("data/event_series.json", text)
        for cmd in ("daily_pr.py plan", "daily_fact_audit.py", "report_daily_holds.py", "verify_daily_run.py",
                    "daily_pr.py open", "daily_pr.py ci", "daily_pr.py merge", "daily_pr.py published",
                    "unittest discover", "build.py"):
            self.assertIn(cmd, text)
        # X投稿・IndexNow は公開確認できた記事IDだけを使う
        self.assertIn("steps.published.outputs.article_ids", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("workflow_dispatch", read_workflow("ci.yml"))


if __name__ == "__main__":
    unittest.main()
