# -*- coding: utf-8 -*-
"""Daily Articles の PR経由・自動マージ運用(daily_fact_audit / daily_pr / x_post_log_store /
resolve_daily_hold / verify_daily_run の台帳チェック)のテスト。
APIは呼ばない(Anthropic・GitHub API・X・本番URLはすべてスタブに差し替える)。
実行: python -m unittest tests/test_daily_pipeline.py -v
"""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import daily_fact_audit as dfa  # noqa: E402
import daily_pr as dp  # noqa: E402
import fact_audit as fa  # noqa: E402
import resolve_daily_hold as rdh  # noqa: E402
import verify_daily_run as vdr  # noqa: E402
import x_post_log_store as xls  # noqa: E402

URL = "https://www.city.example.lg.jp/event.html"
_real_http_get = fa._http_get


def setUpModule():
    fa._http_get = lambda url, limit: (_ for _ in ()).throw(OSError("テストでは画像を取得しない"))


def tearDownModule():
    fa._http_get = _real_http_get


def claim(text, status, role="detail", av="", pv="", **kw):
    c = {"claim": text, "role": role, "status": status, "basis": "primary_page",
         "imageReading": "not_applicable", "articleValue": av, "primaryValue": pv,
         "logicallyCompatible": status != "contradicted", "primaryUrl": URL, "note": ""}
    c.update(kw)
    return c


def result(claims, **kw):
    raw = {"primarySources": [URL], "claims": claims, "hasQuotedComment": False,
           "needsHuman": False, "verdict": "confirmed", "summary": "テスト"}
    raw.update(kw)
    return fa.normalize_result(raw, 1)[0]


def article(aid, title="海岸の花火大会", body="花火大会は10月3日に開催。開始は19時。会場は海岸。", **kw):
    a = {"id": aid, "slug": f"fujisawa-event-{aid:04d}", "title": title, "dek": "リード",
         "area": "藤沢", "cat": "e", "articleType": "news", "link": URL, "sources": [URL], "body": body}
    a.update(kw)
    return a


CONFIRMED = [claim("10月3日開催", "confirmed", role="central"), claim("駐車場なし", "not_found_in_primary")]


# ---------- daily_fact_audit ----------

class DecideTest(unittest.TestCase):
    def test_all_confirmed_allows_auto_merge(self):
        arts = {1: article(1), 2: article(2)}
        decision, fixed = dfa.decide([1, 2], {1: result(CONFIRMED), 2: result(CONFIRMED)}, arts)
        self.assertTrue(decision["auto_merge"])
        self.assertEqual(decision["confirmed_ids"], [1, 2])
        self.assertEqual((decision["held"], fixed), ([], {}))

    def test_no_accepted_articles_is_vacuously_ok(self):
        decision, _ = dfa.decide([], {}, {})
        self.assertTrue(decision["auto_merge"])

    def test_one_non_confirmed_blocks_auto_merge(self):
        bad = result([claim("会場", "source_unavailable", role="title")])
        decision, _ = dfa.decide([1, 2], {1: result(CONFIRMED), 2: bad}, {1: article(1), 2: article(2)})
        self.assertFalse(decision["auto_merge"])
        self.assertEqual(decision["confirmed_ids"], [1])
        self.assertEqual([h["id"] for h in decision["held"]], [2])

    def test_missing_audit_result_is_held(self):
        decision, _ = dfa.decide([1], {}, {1: article(1)})
        self.assertFalse(decision["auto_merge"])
        self.assertIn("結果がない", decision["held"][0]["reasons"][0])

    def test_missing_article_is_held(self):
        decision, _ = dfa.decide([9], {}, {})
        self.assertFalse(decision["auto_merge"])

    def test_quoted_comment_is_not_confirmed(self):
        r = result(CONFIRMED, hasQuotedComment=True)
        decision, _ = dfa.decide([1], {1: r}, {1: article(1)})
        self.assertFalse(decision["auto_merge"])

    def test_unique_fix_is_autofixed_and_verified(self):
        r = result(CONFIRMED + [claim("開始時刻", "contradicted", av="19時", pv="19時30分")])
        seen = {}

        def fake_verify(fixed, targets, client_getter, fetcher, stats):
            seen["body"], seen["targets"] = fixed["body"], [t["claim"] for t in targets]
            return [dict(t, verifyResult="resolved") for t in targets]

        decision, fixed = dfa.decide([1], {1: r}, {1: article(1)}, client_getter=lambda: None,
                                     verify=fake_verify)
        self.assertTrue(decision["auto_merge"])
        self.assertEqual(decision["autofixed_ids"], [1])
        self.assertIn("開始は19時30分", fixed[1]["body"])
        self.assertEqual(seen["targets"], ["開始時刻"])

    def test_unresolved_verify_keeps_hold(self):
        r = result(CONFIRMED + [claim("開始時刻", "contradicted", av="19時", pv="19時30分")])
        decision, _ = dfa.decide(
            [1], {1: r}, {1: article(1)}, client_getter=lambda: None,
            verify=lambda f, t, *a: [dict(c, verifyResult="still_contradicted") for c in t])
        self.assertFalse(decision["auto_merge"])
        self.assertIn("verify で未解決", decision["held"][0]["reasons"][0])

    def test_ambiguous_image_is_held_with_image_check(self):
        c = claim("開催日", "not_found_in_primary", role="central", av="10月3日", pv="10月3日か8日",
                  basis="official_image", imageReading="ambiguous", imageUrl="https://www.city.example.lg.jp/p.jpg")
        decision, _ = dfa.decide([1], {1: result(CONFIRMED[:1] + [c])}, {1: article(1)})
        h = decision["held"][0]
        self.assertEqual(h["escalation"]["kind"], "image_reading")
        body = dfa.hold_body(h, "2026-09-26", "daily/2026-09-26", "https://github.com/o/r/pull/5")
        for want in ("対象トピック", "確認対象", "AI読取候補", "確認してほしいこと", "公式ソースURL",
                     "画像直リンク: https://www.city.example.lg.jp/p.jpg", "[Approve]", "[Reject]",
                     "https://github.com/o/r/pull/5"):
            self.assertIn(want, body)
        self.assertIn("公式画像の確認", dfa.hold_title(h, "2026-09-26"))
        payload = dfa.decode_hold_payload(body)
        self.assertEqual((payload["articleId"], payload["branch"]), (1, "daily/2026-09-26"))
        self.assertEqual(payload["imageChecks"][0]["articleValue"], "10月3日")


class RunAuditTest(unittest.TestCase):
    """fact_audit.main(mode=full)を実際に通し、accepted_ids だけが監査されることを確かめる。"""

    class Client:
        def __init__(self, resp):
            self.resp, self.calls, self.messages = resp, [], self

        def create(self, **kw):
            content = kw["messages"][0]["content"]
            art = json.JSONDecoder().raw_decode(content.split("\n\n", 1)[1])[0]
            self.calls.append(art["id"])
            block = types.SimpleNamespace(type="tool_use", name="submit_audit", input=self.resp)
            return types.SimpleNamespace(content=[block], stop_reason="tool_use")

    def test_only_accepted_ids_are_audited(self):
        with tempfile.TemporaryDirectory() as d:
            arts = [article(1), article(2), article(3)]
            paths = {k: os.path.join(d, f"{k}.json") for k in ("articles", "report", "decision")}
            json.dump(arts, open(paths["articles"], "w"))
            json.dump({"accepted_ids": [2, 3], "date": "2026-09-26"}, open(paths["report"], "w"))
            client = self.Client({"primarySources": [URL], "claims": CONFIRMED, "hasQuotedComment": False,
                                  "needsHuman": False, "verdict": "confirmed", "summary": ""})
            args = types.SimpleNamespace(report=paths["report"], articles=paths["articles"],
                                         decision=paths["decision"], out_dir=os.path.join(d, "out"))
            with mock.patch.object(dfa, "AUDIT_SLEEP_SEC", 0), redirect_stdout(io.StringIO()):
                self.assertEqual(dfa.run_audit(args, client=client), 0)
            decision = json.load(open(paths["decision"]))
            self.assertEqual(sorted(client.calls), [2, 3])
            self.assertTrue(decision["auto_merge"])
            self.assertEqual(decision["date"], "2026-09-26")


# ---------- daily_pr ----------

def pr(state="open", merged=False, head_sha="abc", ref="daily/2026-09-26", **kw):
    p = {"number": 7, "state": state, "merged_at": "2026-09-26T00:00:00Z" if merged else None,
         "draft": False, "base": {"ref": "main"}, "head": {"ref": ref, "sha": head_sha},
         "mergeable": True, "html_url": "https://github.com/o/r/pull/7", "merge_commit_sha": "m1"}
    p.update(kw)
    return p


CONFIRMED_TRAILERS = dp.parse_trailers(dp.commit_trailers("2026-09-26", [1, 2], {"auto_merge": True}))


class ModeTest(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(dp.decide_mode(False, [])[0], "generate")
        self.assertEqual(dp.decide_mode(True, [])[0], "resume_branch")
        self.assertEqual(dp.decide_mode(True, [pr()])[0], "resume_open")
        self.assertEqual(dp.decide_mode(True, [pr(state="closed")])[0], "abandoned")
        self.assertEqual(dp.decide_mode(True, [pr(state="closed"), pr(state="closed", merged=True)])[0],
                         "published")

    def test_branch_name_validates_date(self):
        self.assertEqual(dp.branch_name("2026-09-26"), "daily/2026-09-26")
        with self.assertRaises(ValueError):
            dp.branch_name("../main")


class MergeDecisionTest(unittest.TestCase):
    B = "daily/2026-09-26"

    def test_all_pass(self):
        self.assertEqual(dp.merge_decision(CONFIRMED_TRAILERS, pr(), self.B, "abc", "success"), (True, []))

    def test_trailers_roundtrip(self):
        self.assertEqual(CONFIRMED_TRAILERS["Daily-Accepted-Ids"], "1,2")
        self.assertEqual(dp.parse_ids(CONFIRMED_TRAILERS["Daily-Accepted-Ids"]), [1, 2])
        held = dp.parse_trailers(dp.commit_trailers("2026-09-26", [1], {"auto_merge": False}))
        self.assertEqual(held["Daily-Fact-Audit"], "held")

    def assertBlocked(self, *args):
        ok, reasons = dp.merge_decision(*args)
        self.assertFalse(ok)
        self.assertTrue(reasons)

    def test_blocked_cases(self):
        held = dict(CONFIRMED_TRAILERS, **{"Daily-Fact-Audit": "held"})
        self.assertBlocked(held, pr(), self.B, "abc", "success")
        self.assertBlocked({}, pr(), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(), self.B, "abc", "failure")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(), self.B, "abc", "timeout")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(head_sha="zzz"), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(state="closed"), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(draft=True), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(base={"ref": "dev"}), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(ref="other"), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, pr(mergeable=False), self.B, "abc", "success")
        self.assertBlocked(CONFIRMED_TRAILERS, None, self.B, "abc", "success")

    def test_unknown_mergeable_is_left_to_github(self):
        self.assertTrue(dp.merge_decision(CONFIRMED_TRAILERS, pr(mergeable=None), self.B, "abc", "success")[0])


class FakeApi:
    """daily_pr.Api と同じ形のスタブ。呼ばれた書き込み系APIを記録する。"""
    repo = "o/r"

    def __init__(self, message, prs=(), pr_detail=None, merge_res=None, sha="abc"):
        self.message, self.prs, self.sha = message, list(prs), sha
        self.pr_detail = pr_detail or pr()
        self.merge_res = merge_res if merge_res is not None else {"merged": True, "sha": "m1"}
        self.writes = []

    def head_commit(self, branch):
        return self.sha, self.message

    def prs_for(self, branch):
        return self.prs

    def pr(self, number):
        return self.pr_detail

    def branch_exists(self, branch):
        return True

    def call(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if method == "POST" and path == "/pulls":
            return pr()
        if method == "PUT":
            if isinstance(self.merge_res, Exception):
                raise self.merge_res
            return self.merge_res
        return {}


def commit_message(audit=True):
    return "chore: 本日分の記事を自動追加\n\n" + dp.commit_trailers("2026-09-26", [1], {"auto_merge": audit})


class MergeStageTest(unittest.TestCase):
    args = types.SimpleNamespace(branch="daily/2026-09-26")

    def run_stage(self, api, ci="success"):
        calls = []

        def fake_ci(api_, branch, sha):
            calls.append((branch, sha))
            return ci

        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
                mock.patch("sys.stderr", io.StringIO()):
            code = dp.cmd_merge_stage(self.args, api=api, ci=fake_ci)
        return code, calls

    def test_confirmed_and_ci_success_merges(self):
        api = FakeApi(commit_message(True), prs=[pr()])
        code, calls = self.run_stage(api)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("daily/2026-09-26", "abc")])
        put = [w for w in api.writes if w[0] == "PUT"]
        self.assertEqual(put[0][1], "/pulls/7/merge")
        self.assertEqual(put[0][2]["sha"], "abc")  # 当日commitのままの時だけマージされる
        self.assertFalse([w for w in api.writes if w[0] == "POST"])  # 既存PRを再利用

    def test_held_audit_never_merges_or_runs_ci(self):
        api = FakeApi(commit_message(False), prs=[pr()])
        code, calls = self.run_stage(api)
        self.assertEqual((code, calls), (0, []))
        self.assertFalse([w for w in api.writes if w[0] == "PUT"])

    def test_ci_failure_does_not_merge(self):
        api = FakeApi(commit_message(True), prs=[pr()])
        code, _ = self.run_stage(api, ci="failure")
        self.assertEqual(code, 1)
        self.assertFalse([w for w in api.writes if w[0] == "PUT"])

    def test_extra_commit_on_pr_does_not_merge(self):
        api = FakeApi(commit_message(True), prs=[pr()], pr_detail=pr(head_sha="human"))
        code, _ = self.run_stage(api)
        self.assertEqual(code, 1)
        self.assertFalse([w for w in api.writes if w[0] == "PUT"])

    def test_non_bot_head_commit_is_held(self):
        api = FakeApi("fix: 手で修正", prs=[pr()])
        code, calls = self.run_stage(api)
        self.assertEqual((code, calls), (0, []))
        self.assertFalse([w for w in api.writes if w[0] == "PUT"])

    def test_creates_pr_when_missing(self):
        api = FakeApi(commit_message(True), prs=[])
        self.run_stage(api)
        posts = [w for w in api.writes if w[0] == "POST" and w[1] == "/pulls"]
        self.assertEqual(posts[0][2]["head"], "daily/2026-09-26")
        self.assertEqual(posts[0][2]["base"], "main")

    def test_merge_rejected_by_github_fails(self):
        err = urllib.error.HTTPError("u", 405, "Method Not Allowed", {}, None)
        api = FakeApi(commit_message(True), prs=[pr()], merge_res=err)
        code, _ = self.run_stage(api)
        self.assertEqual(code, 1)


class CiWaitTest(unittest.TestCase):
    def test_waits_for_completed_run_of_same_sha(self):
        runs = iter([
            {"workflow_runs": []},
            {"workflow_runs": [{"head_sha": "abc", "status": "in_progress", "created_at": "9999"}]},
            {"workflow_runs": [{"head_sha": "other", "status": "completed", "conclusion": "success", "created_at": "9999"},
                               {"head_sha": "abc", "status": "completed", "conclusion": "success", "created_at": "9999"}]},
        ])
        api = types.SimpleNamespace(calls=[])

        def call(method, path, payload=None):
            api.calls.append((method, path, payload))
            return {} if method == "POST" else next(runs)

        api.call = call
        with redirect_stdout(io.StringIO()):
            got = dp.dispatch_ci_and_wait(api, "daily/2026-09-26", "abc", sleep=lambda s: None)
        self.assertEqual(got, "success")
        self.assertEqual(api.calls[0], ("POST", "/actions/workflows/ci.yml/dispatches", {"ref": "daily/2026-09-26"}))

    def test_timeout(self):
        clock = iter(range(0, 10_000, 100))
        api = types.SimpleNamespace(call=lambda m, p, d=None: {"workflow_runs": []})
        got = dp.dispatch_ci_and_wait(api, "b", "abc", timeout=300, sleep=lambda s: None, now=lambda: next(clock))
        self.assertEqual(got, "timeout")


class PublishTest(unittest.TestCase):
    def test_new_article_ids_are_those_added_by_merge(self):
        before = [{"id": 1}, {"id": 2}]
        after = before + [{"id": 3}, {"id": 4, "mergedInto": 1}, {"id": 5}]
        self.assertEqual(dp.new_article_ids(before, after), [3, 5])

    def test_only_published_urls_are_returned(self):
        state = {"n": 0}

        def check(url):
            state["n"] += 1
            return url.endswith("a/") or (url.endswith("b/") and state["n"] > 3)

        clock = iter(range(0, 10_000, 10))
        with redirect_stdout(io.StringIO()):
            got = dp.wait_published({1: "https://x/a/", 2: "https://x/b/", 3: "https://x/c/"}, check=check,
                                    timeout=100, sleep=lambda s: None, now=lambda: next(clock))
        self.assertEqual(got, [1, 2])  # 公開を確認できない記事は X投稿・IndexNow の対象外

    def test_publish_targets_from_merge_commit(self):
        files = {"m^1": [{"id": 1}], "m": [{"id": 1}, {"id": 2}]}

        def runner(cmd, **kw):
            rev = cmd[2].split(":")[0]
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(files[rev]))

        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}):
            dp.cmd_publish_targets(types.SimpleNamespace(merge_sha="m", article_ids=""), runner=runner)
        self.assertIn("article_ids=2", out.getvalue())


# ---------- x_post_log_store ----------

class XLogTest(unittest.TestCase):
    def test_merge_is_union_keeping_first(self):
        a = {"posts": [{"article_id": 1, "tweet_id": "a"}, {"article_id": 2, "tweet_id": "b"}]}
        b = {"posts": [{"article_id": 2, "tweet_id": "x"}, {"article_id": 3, "tweet_id": "c"}]}
        self.assertEqual(xls.merge_logs(a, b)["posts"],
                         [{"article_id": 1, "tweet_id": "a"}, {"article_id": 2, "tweet_id": "b"},
                          {"article_id": 3, "tweet_id": "c"}])
        self.assertEqual(xls.merge_logs(None, {"posts": "broken"}), {"posts": []})

    def test_push_and_pull_via_data_branch_never_touch_main(self):
        with tempfile.TemporaryDirectory() as d:
            remote, work = os.path.join(d, "remote.git"), os.path.join(d, "work")
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@e")

            def sh(*cmd, cwd=None):
                return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout

            sh("git", "init", "-q", "--bare", "-b", "main", remote)
            sh("git", "init", "-q", "-b", "main", work)
            os.makedirs(os.path.join(work, "data"))
            path = os.path.join(work, "data", "x_post_log.json")
            json.dump({"posts": [{"article_id": 1}]}, open(path, "w"))
            sh("git", "add", ".", cwd=work)
            sh("git", "commit", "-qm", "init", cwd=work)
            sh("git", "remote", "add", "origin", remote, cwd=work)
            sh("git", "push", "-q", "origin", "main", cwd=work)
            main_before = sh("git", "rev-parse", "main", cwd=remote)

            def runner(cmd, **kw):
                return subprocess.run(cmd, **dict(kw, cwd=work))

            with redirect_stdout(io.StringIO()):
                json.dump({"posts": [{"article_id": 1}, {"article_id": 2}]}, open(path, "w"))
                self.assertEqual(xls.push("log", runner=runner, path=path), 0)
                # 別の実行が追加した記録も消さずに取り込む
                json.dump({"posts": [{"article_id": 3}]}, open(path, "w"))
                self.assertEqual(xls.push("log", runner=runner, path=path), 0)
                json.dump({"posts": []}, open(path, "w"))
                merged = xls.pull(runner=runner, path=path)
            self.assertEqual(sorted(xls.posted_ids(merged)), [1, 2, 3])
            self.assertEqual(sh("git", "rev-parse", "main", cwd=remote), main_before)  # main は変わらない
            files = sh("git", "ls-tree", "--name-only", "x-post-log", cwd=remote).split()
            self.assertEqual(files, ["x_post_log.json"])


# ---------- resolve_daily_hold ----------

class ResolveHoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.paths = {k: os.path.join(d, f"{k}.json") for k in ("articles", "conf", "stock")}
        json.dump([article(1), article(2, articleType="stock")], open(self.paths["articles"], "w"))
        json.dump([], open(self.paths["conf"], "w"))
        json.dump([{"id": "t1", "status": "generated", "generatedArticleId": 2}], open(self.paths["stock"], "w"))

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, payload, decision, value=""):
        return rdh.resolve(payload, decision, value, "12", articles_path=self.paths["articles"],
                           confirmations_path=self.paths["conf"], stock_path=self.paths["stock"])

    CHECK = {"claim": "開始時刻", "articleValue": "19時", "imageUrl": "https://e/p.jpg",
             "pageUrl": URL, "candidate": "19時か18時"}

    def test_approve_image_records_and_fixes_value(self):
        code, _ = self.resolve({"articleId": 1, "imageChecks": [self.CHECK]}, "approve", "18時")
        self.assertEqual(code, 0)
        arts = json.load(open(self.paths["articles"]))
        self.assertIn("開始は18時", arts[0]["body"])
        conf = json.load(open(self.paths["conf"]))
        self.assertEqual((conf[0]["decision"], conf[0]["value"]), ("approve", "18時"))

    def test_approve_non_image_needs_editing(self):
        code, _ = self.resolve({"articleId": 1, "imageChecks": []}, "approve")
        self.assertEqual(code, 3)

    def test_reject_removes_article_and_resets_stock_topic(self):
        code, _ = self.resolve({"articleId": 2, "imageChecks": []}, "reject")
        self.assertEqual(code, 0)
        self.assertEqual([a["id"] for a in json.load(open(self.paths["articles"]))], [1])
        self.assertEqual(json.load(open(self.paths["stock"]))[0]["status"], "skipped")

    def test_unknown_article(self):
        self.assertEqual(self.resolve({"articleId": 99}, "reject")[0], 2)


# ---------- verify_daily_run の台帳チェック ----------

class LedgerCheckTest(unittest.TestCase):
    def test_unstaged_event_series_is_detected(self):
        out = " M data/event_series.json\nM  data/articles.json\n?? data/x_post_log.json\n"
        runner = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        self.assertEqual(vdr.unstaged_ledgers(runner), ["data/event_series.json", "data/x_post_log.json"])

    def test_workflow_stages_event_series(self):
        with open(workflow_files()["daily-articles.yml"], encoding="utf-8") as f:
            wf = f.read()
        self.assertIn("data/event_series.json", wf)
        self.assertNotIn("git push\n", wf)  # main への直接pushをしない


try:
    import yaml
except ImportError:  # PyYAML が無い環境では構文確認だけ省略する
    yaml = None


def workflow_files():
    """検査対象の workflow(ファイル名 → パス)。GitHub App が .github/workflows/ を更新できないため、
    オーナーが適用する前の更新版(.github/workflow-updates/)があればそちらを優先する。"""
    files = {}
    for sub in ("workflows", "workflow-updates"):
        d = os.path.join(ROOT, ".github", sub)
        if os.path.isdir(d):
            files.update({n: os.path.join(d, n) for n in os.listdir(d) if n.endswith((".yml", ".yaml"))})
    return files


class WorkflowSafetyTest(unittest.TestCase):
    """workflow の構文と、main への直接pushが残っていないことを確認する。"""

    @unittest.skipIf(yaml is None, "PyYAML がない")
    def test_workflow_syntax_and_job_references(self):
        files = workflow_files()
        for name, path in sorted(files.items()):
            with open(path, encoding="utf-8") as f:
                wf = yaml.safe_load(f)
            self.assertIn("jobs", wf, name)
            self.assertIn(True, wf, name)  # PyYAML は `on:` を True として読む
            jobs = wf["jobs"]
            for job_id, job in jobs.items():
                needs = job.get("needs") or []
                for n in [needs] if isinstance(needs, str) else needs:
                    self.assertIn(n, jobs, f"{name}:{job_id} needs {n}")
                uses = job.get("uses") or ""
                if uses.startswith("./.github/workflows/"):
                    self.assertIn(os.path.basename(uses), files, f"{name}:{job_id} {uses}")
                self.assertTrue(job.get("steps") or uses, f"{name}:{job_id}")

        def load(n):
            with open(files[n], encoding="utf-8") as f:
                return yaml.safe_load(f)

        self.assertEqual(load("daily-articles.yml")["concurrency"],
                         {"group": "daily-articles", "cancel-in-progress": False})
        self.assertIn("workflow_dispatch", load("ci.yml")[True])
        self.assertIn("workflow_call", load("daily-publish.yml")[True])

    def test_no_direct_push_to_main(self):
        for name, path in workflow_files().items():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("git pull --rebase origin main", text, name)
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("git push") or s.startswith("run: git push"):
                    self.assertIn("refs/heads/$BRANCH", s, f"{name}: {s}")


if __name__ == "__main__":
    unittest.main()
