# -*- coding: utf-8 -*-
"""Daily Articles の日次終了条件(confirmed_today >= 3)と補充 run の引き継ぎ(daily_state.py / daily_pr.py)のテスト。
APIは呼ばない(GitHub API・Anthropic クライアントはスタブ。git の永続化は一時リポジトリだけで試す)。
実行: python -m unittest tests/test_daily_state.py -v
"""
import json
import os
import subprocess
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
import daily_state as ds  # noqa: E402
from test_daily_pipeline import (CONFIRMED, CORE_CONTRADICTED, FakeGitHub, Workspace, article, no_fetch,  # noqa: E402
                                 pr, run_report)
from test_publish_gate import FakeClient  # noqa: E402

DATE = "2026-10-06"


def today_article(i, title="記事"):
    return article(i, f"{title}{i}", date=DATE, slug=f"s{i}")


def merged_pr(branch, meta):
    return pr(state="closed", merged_at="t", merge_commit_sha="m", body=dpr.pr_body(meta),
              head={"ref": branch, "sha": "abc", "repo": {"full_name": "o/r"}})


class AuthenticationError(Exception):
    """anthropic.AuthenticationError の代わり(同じクラス名で判定されることを確かめる)"""


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.patches = [
            mock.patch.object(ds, "CONTEXT_PATH", os.path.join(t, "ctx.json")),
            mock.patch.object(ds, "ERRORS_PATH", os.path.join(t, "errors.json")),
            mock.patch.object(dpr, "META_PATH", os.path.join(t, "meta.json")),
            mock.patch.object(dpr, "PUBLISH_TIMEOUT_SEC", 0),
            mock.patch.dict(os.environ, {"GITHUB_OUTPUT": os.path.join(t, "out"),
                                         "GITHUB_STEP_SUMMARY": os.path.join(t, "sum"),
                                         "GITHUB_EVENT_NAME": "workflow_dispatch",
                                         "GITHUB_ACTOR": "github-actions[bot]",
                                         "GITHUB_TRIGGERING_ACTOR": "github-actions[bot]"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def outputs(self):
        with open(os.environ["GITHUB_OUTPUT"], encoding="utf-8") as f:
            return dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    @staticmethod
    def dispatches(gh):
        return [c for c in gh.calls if c[0] == "POST" and c[1].endswith(f"/{dpr.DAILY_WORKFLOW}/dispatches")]

    @staticmethod
    def issues(gh):
        return [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")]


# ---------- 終了条件(純粋関数) ----------

class CompletionRuleTest(unittest.TestCase):
    def test_two_is_not_complete_and_continues_refill(self):
        self.assertEqual(ds.next_action(2, 0), "refill")
        self.assertEqual(ds.next_action(0, 1), "refill")

    def test_three_or_more_is_complete(self):
        self.assertEqual(ds.next_action(3, 0), ds.COMPLETE)
        self.assertEqual(ds.next_action(5, 2), ds.COMPLETE)
        # 3本に届いていればシステム障害があっても Complete(止める理由がない)
        self.assertEqual(ds.next_action(3, 1, [{"reason": "x"}]), ds.COMPLETE)

    def test_only_system_failure_or_round_limit_stops(self):
        self.assertEqual(ds.next_action(2, 0, [{"reason": "RateLimitError"}]), ds.SYSTEM_FAILURE)
        self.assertEqual(ds.next_action(2, ds.DAILY_MAX_REFILL_ROUNDS), ds.ROUND_LIMIT)
        self.assertEqual(ds.next_action(2, ds.DAILY_MAX_REFILL_ROUNDS - 1), "refill")

    def test_refill_round_targets_only_shortfall(self):
        self.assertEqual(ds.run_quota(0, 0, 5, 3), (3, 5))   # 通常の当日 run は従来どおり
        self.assertEqual(ds.run_quota(2, 1, 5, 3), (1, 1))   # 補充 run は不足分だけ
        self.assertEqual(ds.run_quota(0, 2, 5, 3), (3, 3))
        self.assertEqual(ds.run_quota(3, 1, 5, 3), (0, 0))

    def test_today_ids_excludes_other_days_and_merged(self):
        arts = [today_article(1), today_article(2, title="x"), article(3, "昨日", date="2026-10-05"),
                dict(today_article(4), mergedInto="s1")]
        self.assertEqual(ds.today_ids(arts, DATE), [1, 2])

    def test_system_error_classification(self):
        self.assertTrue(ds.system_error_reason(AuthenticationError("invalid x-api-key")))
        self.assertTrue(ds.system_error_reason(RuntimeError("Your credit balance is too low to access the API")))
        self.assertTrue(ds.system_error_in_text("監査処理で例外: RateLimitError: rate_limit_error"))
        # 候補ごとの問題(公式サイトの 403・形式異常など)はシステム障害ではない
        self.assertIsNone(ds.system_error_reason(urllib.error.HTTPError("https://city.example.jp", 403, "x", {}, None)))
        self.assertIsNone(ds.system_error_reason(ValueError("[news] レスポンスが空配列でした。")))
        self.assertIsNone(ds.system_error_in_text("監査処理で例外: KeyError: 'claims'"))
        self.assertTrue(ds.github_error_reason(urllib.error.HTTPError("u", 503, "x", {}, None)))
        self.assertIsNone(ds.github_error_reason(urllib.error.HTTPError("u", 422, "x", {}, None)))


# ---------- plan: 未完了なら次の round(補充 run)へ ----------

class PlanRoundTest(StateTestCase):
    def gh_for(self, rounds):
        """rounds: {branch: pulls}"""
        routes = {}
        for branch, pulls in rounds.items():
            routes[("GET", f"/repos/o/r/pulls?head=o:{branch}&")] = pulls
        routes[("GET", "/repos/o/r/pulls?")] = []
        routes[("GET", "/repos/o/r/issues?")] = []
        routes[("POST", "/repos/o/r/issues")] = {"html_url": "u"}
        return FakeGitHub(routes)

    def meta(self, rnd, ids):
        return {"date": DATE, "branch": dpr.branch_for(DATE, rnd), "published_ids": ids,
                "published_slugs": [f"s{i}" for i in ids], "held_ids": [], "all_published_confirmed": True}

    def test_two_confirmed_after_merge_starts_refill_round_for_shortfall(self):
        b0 = dpr.branch_for(DATE)
        gh = self.gh_for({b0: [merged_pr(b0, self.meta(0, [1, 2]))]})
        store = ds.MemoryStore({DATE: {"rejected": [ds.compact({"title": "見送り"}, "x", 0)]}})
        code = dpr.cmd_plan(gh, DATE, store, articles=[today_article(1), today_article(2)])
        self.assertEqual(code, 0)
        out = self.outputs()
        self.assertEqual((out["mode"], out["branch"], out["round"]), ("fresh", f"daily/{DATE}-r1", "1"))
        ctx = ds.load_context()
        self.assertEqual((ctx["round"], ctx["date"], ctx["confirmed_today"]), (1, DATE, 2))
        self.assertEqual(len(ctx["rejected"]), 1)  # 見送り済みの対象を補充 run に引き継ぐ
        self.assertEqual(store.days[DATE]["status"], ds.INCOMPLETE)

    def test_three_confirmed_is_done_and_does_not_generate(self):
        b0 = dpr.branch_for(DATE)
        gh = self.gh_for({b0: [merged_pr(b0, self.meta(0, [1, 2, 3]))]})
        code = dpr.cmd_plan(gh, DATE, ds.MemoryStore(), articles=[today_article(i) for i in (1, 2, 3)])
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["mode"], "done")

    def test_refill_round_open_pr_is_resumed_not_regenerated(self):
        b0, b1 = dpr.branch_for(DATE), dpr.branch_for(DATE, 1)
        gh = self.gh_for({b0: [merged_pr(b0, self.meta(0, [1, 2]))],
                          b1: [pr(body=dpr.pr_body(self.meta(1, [3])), head={"ref": b1, "sha": "abc",
                                                                            "repo": {"full_name": "o/r"}})]})
        dpr.cmd_plan(gh, DATE, ds.MemoryStore(), articles=[today_article(1), today_article(2)])
        out = self.outputs()
        self.assertEqual((out["mode"], out["branch"]), ("resume", b1))

    def test_round_limit_stops_with_failure_and_issue(self):
        rounds = {dpr.branch_for(DATE, n): [merged_pr(dpr.branch_for(DATE, n), self.meta(n, []))]
                  for n in range(ds.DAILY_MAX_REFILL_ROUNDS + 1)}
        gh = self.gh_for(rounds)
        store = ds.MemoryStore()
        code = dpr.cmd_plan(gh, DATE, store, articles=[today_article(1), today_article(2)])
        self.assertEqual(code, 1)
        self.assertEqual(self.outputs()["mode"], "stopped")
        self.assertEqual(store.days[DATE]["status"], ds.ROUND_LIMIT)
        self.assertTrue(self.issues(gh))

    def test_stopped_day_is_not_retried_by_bot_but_owner_can_resume(self):
        b0 = dpr.branch_for(DATE)
        failure = {"status": ds.SYSTEM_FAILURE, "failure": {"reason": "AuthenticationError"}}
        gh = self.gh_for({b0: [merged_pr(b0, self.meta(0, [1, 2]))]})
        code = dpr.cmd_plan(gh, DATE, ds.MemoryStore({DATE: failure}), articles=[today_article(1)])
        self.assertEqual(code, 1)  # watchdog・補充 run(bot)では生成しない
        self.assertEqual(self.outputs()["mode"], "stopped")
        store = ds.MemoryStore({DATE: failure})
        code = dpr.cmd_plan(gh, DATE, store, articles=[today_article(1)], human=True)
        self.assertEqual(code, 0)  # オーナーの手動実行で再開
        self.assertEqual(self.outputs()["mode"], "fresh")
        self.assertEqual(store.days[DATE]["status"], ds.INCOMPLETE)


# ---------- Fact Audit 後(公開前): 見送り対象の記録・公開0件の run の続き ----------

class AfterAuditTest(StateTestCase):
    def gh(self):
        return FakeGitHub({("GET", "/repos/o/r/issues?"): [], ("POST", "/repos/o/r/issues"): {"html_url": "u"}})

    def report(self, accepted=(), gate_rejected=(), attempts=()):
        return {"status": "ok", "date": DATE, "accepted_ids": list(accepted), "gate_rejected": list(gate_rejected),
                "planning": {"attempts": list(attempts)}}

    def test_rejected_and_held_targets_are_recorded_for_next_round(self):
        ds.save_context({"date": DATE, "round": 0})
        store = ds.MemoryStore()
        rejected = {"title": "不合格の記事", "area": "藤沢", "link": "https://a.example.jp/ng",
                    "summary": "", "reasons": ["矛盾"], "draft": {"title": "不合格の記事", "subjectNames": ["対象A"]}}
        holds = [{"id": 9, "verdict": "fix", "entry": dict(today_article(9, "保留"), subjectNames=["対象B"]),
                  "stockTopicId": "s7", "summary": ""}]
        attempts = [{"id": "n1", "titleIdea": "企画だけで終わった", "subject": "対象C", "area": "鎌倉", "cat": "e"},
                    {"id": "n2", "titleIdea": "採用した企画", "subject": "対象D", "selected": True}]
        code = dpr.cmd_after_audit(self.gh(), store, self.report([1, 2], [rejected], attempts), holds,
                                   articles=[today_article(1), today_article(2)])
        self.assertEqual(code, 0)
        titles = [r["title"] for r in store.days[DATE]["rejected"]]
        self.assertEqual(titles, ["不合格の記事", "保留9", "企画だけで終わった"])
        self.assertIn("s7", ds.rejected_topic_ids(store.days[DATE]))
        self.assertEqual(store.days[DATE]["rounds"]["0"]["published_ids"], [1, 2])

    def test_zero_published_run_dispatches_refill_instead_of_success_end(self):
        ds.save_context({"date": DATE, "round": 1})
        gh = FakeGitHub({("POST", "/repos/o/r/actions/workflows/"): None})
        store = ds.MemoryStore()
        code = dpr.cmd_after_audit(gh, store, self.report(), [], articles=[today_article(1), today_article(2)])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.dispatches(gh)), 1)
        self.assertEqual(store.days[DATE]["status"], ds.INCOMPLETE)
        self.assertTrue(store.days[DATE]["rounds"]["1"]["refill_dispatched"])

    def test_system_failure_stops_without_refill_and_notifies(self):
        ds.save_context({"date": DATE, "round": 0})
        gh = self.gh()
        store = ds.MemoryStore()
        broken = {"title": "監査できなかった", "summary": "監査処理で例外: AuthenticationError: invalid x-api-key",
                  "reasons": ["監査処理で例外"]}
        code = dpr.cmd_after_audit(gh, store, self.report([], [broken]), [], articles=[today_article(1)])
        self.assertEqual(code, 1)
        self.assertFalse(self.dispatches(gh))
        self.assertEqual(store.days[DATE]["status"], ds.SYSTEM_FAILURE)
        self.assertTrue(self.issues(gh))
        self.assertEqual(ds.load_context()["status"], ds.SYSTEM_FAILURE)  # verify_daily_run.py が failure にする

    def test_quality_rejection_alone_is_not_a_system_failure(self):
        ds.save_context({"date": DATE, "round": 0})
        gh = FakeGitHub({("POST", "/repos/o/r/actions/workflows/"): None})
        ng = {"title": "矛盾あり", "summary": "開催日が一次情報と矛盾", "reasons": ["一次情報と矛盾する記述"]}
        code = dpr.cmd_after_audit(gh, ds.MemoryStore(), self.report([], [ng]), [], articles=[])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.dispatches(gh)), 1)  # 品質で落ちただけなら別候補で補充を続ける

    def test_without_daily_plan_context_state_is_untouched(self):
        store = ds.MemoryStore()  # Manual Daily Top-up など、plan を経ない実行
        self.assertEqual(dpr.cmd_after_audit(FakeGitHub({}), store, self.report(), [], articles=[]), 0)
        self.assertEqual(store.saved, [])


# ---------- 公開確認後: Complete / 補充 run / Facebook 対象 ----------

class PublishedFinalizeTest(StateTestCase):
    def gh(self, branch):
        return FakeGitHub({("GET", "/repos/o/r/pulls/7"): pr(merged_at="t", merge_commit_sha="m",
                                                              head={"ref": branch, "sha": "abc",
                                                                    "repo": {"full_name": "o/r"}}),
                           ("GET", "/repos/o/r/compare/"): {"status": "identical"},
                           ("POST", "/repos/o/r/actions/workflows/"): None,
                           ("GET", "/repos/o/r/issues?"): [], ("POST", "/repos/o/r/issues"): {"html_url": "u"}})

    def meta(self, rnd, ids):
        return {"date": DATE, "branch": dpr.branch_for(DATE, rnd), "published_ids": ids,
                "published_slugs": [f"s{i}" for i in ids], "held_ids": [], "all_published_confirmed": True,
                "pr": 7, "round": rnd}

    def test_two_published_is_incomplete_and_refill_continues(self):
        store = ds.MemoryStore()
        gh = self.gh(dpr.branch_for(DATE))
        code = dpr.cmd_published(gh, self.meta(0, [1, 2]), check=lambda u: True, sleep=lambda s: None,
                                 store=store, articles=[today_article(1), today_article(2)])
        self.assertEqual(code, 0)  # confirmed 2本は公開・Facebook へ進む
        self.assertEqual(self.outputs()["article_ids"], "1,2")
        self.assertEqual(store.days[DATE]["status"], ds.INCOMPLETE)  # ただしその日は未完了
        self.assertEqual(len(self.dispatches(gh)), 1)  # 不足分の補充 run を起動

    def test_third_confirmed_in_refill_completes_and_posts_only_new_article(self):
        store = ds.MemoryStore({DATE: {"rounds": {"0": {"published_ids": [1, 2], "refill_dispatched": True}}}})
        gh = self.gh(dpr.branch_for(DATE, 1))
        code = dpr.cmd_published(gh, self.meta(1, [3]), check=lambda u: True, sleep=lambda s: None, store=store,
                                 articles=[today_article(i) for i in (1, 2, 3)])
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["article_ids"], "3")  # Facebook・IndexNow は新規 confirmed 分だけ
        self.assertEqual(store.days[DATE]["status"], ds.COMPLETE)
        self.assertFalse(self.dispatches(gh))  # Complete 後は補充しない

    def test_rerun_does_not_dispatch_twice(self):
        store = ds.MemoryStore()
        gh = self.gh(dpr.branch_for(DATE))
        for _ in range(2):
            dpr.cmd_published(gh, self.meta(0, [1, 2]), check=lambda u: True, sleep=lambda s: None, store=store,
                              articles=[today_article(1), today_article(2)])
        self.assertEqual(len(self.dispatches(gh)), 1)

    def test_system_failure_after_publish_fails_run_and_hands_articles_over_later(self):
        store = ds.MemoryStore({DATE: {"rounds": {"0": {"system_errors": [{"where": "企画", "reason": "RateLimitError"}]}}}})
        gh = self.gh(dpr.branch_for(DATE))
        code = dpr.cmd_published(gh, self.meta(0, [1]), check=lambda u: True, sleep=lambda s: None, store=store,
                                 articles=[today_article(1)])
        self.assertEqual(code, 1)  # 明示的に failure(Facebook・IndexNow は走らない)
        self.assertFalse(self.dispatches(gh))
        self.assertEqual(store.days[DATE]["status"], ds.SYSTEM_FAILURE)
        self.assertEqual(store.days[DATE]["social_pending"], [1])
        # 再開後の補充 run で3本目が confirmed → 未投稿の1と新規の3だけを渡す(投稿済みの記事は渡さない)
        store.days[DATE].update(status=ds.INCOMPLETE)
        gh = self.gh(dpr.branch_for(DATE, 1))
        code = dpr.cmd_published(gh, self.meta(1, [3]), check=lambda u: True, sleep=lambda s: None, store=store,
                                 articles=[today_article(i) for i in (1, 2, 3)])
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["article_ids"], "3,1")
        self.assertEqual(store.days[DATE]["social_pending"], [])


# ---------- 補充 round の生成・監査: 不足分だけ・見送り済みは再利用しない・品質基準は同じ ----------

class RefillAuditTest(StateTestCase):
    def run_refill(self, existing_today, new_articles, client, batches, rejected=()):
        ds.save_context({"date": DATE, "round": 1, "rejected": list(rejected)})
        arts = existing_today + new_articles
        ws = Workspace(arts, report=run_report(new_articles, date=DATE))
        self.addCleanup(ws.dir.cleanup)
        calls = []

        def topup(need, avoid, today, event_series, gate_rejections, round_no):
            calls.append({"need": need, "avoid": avoid})
            return [dict(c) for c in batches.pop(0)] if batches else []

        counter = iter(range(301, 400))
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        with mock.patch.dict(os.environ, env, clear=True):
            dfa.main(ws.argv(), client=client, fetcher=no_fetch, image_fetcher=lambda c: None,
                     base_series_keys=set(), topup=topup, reserve_ids=lambda n: [next(counter) for _ in range(n)])
        return ws.read("report"), calls

    def candidate(self, title, n, **kw):
        c = article(0, title, id="today-run-pending-id", slug="", date=DATE,
                    link=f"https://www.city.example.lg.jp/refill{n}.html",
                    sources=[f"https://www.city.example.lg.jp/refill{n}.html"], subjectNames=[f"補充{n}号"],
                    body="さしすせそたちつてと"[n % 10] * 60 + str(n) * 60 + "。")
        c.update(kw)
        return c

    def test_refill_round_needs_only_the_shortfall(self):
        client = FakeClient({"新規A": [CORE_CONTRADICTED], "新規B": [CONFIRMED]})
        report, calls = self.run_refill([today_article(1), today_article(2)], [today_article(10, "新規A")],
                                        client, [[self.candidate("新規B", 1)]])
        self.assertEqual(calls[0]["need"], 1)  # 2本公開済み → 3本目の1件だけ
        self.assertEqual(report["accepted_ids"], [301])
        self.assertIsNone(report["shortfall"])

    def test_rejected_target_of_same_day_is_not_reused(self):
        rejected = [ds.compact({"title": "前の round で不合格", "link": "https://www.city.example.lg.jp/refill1.html",
                                "subjectNames": ["補充1号"]}, "公開前監査で不合格", 0)]
        client = FakeClient({"新規A": [CORE_CONTRADICTED], "再挑戦": [CONFIRMED], "別候補": [CONFIRMED]})
        report, calls = self.run_refill([today_article(1), today_article(2)], [today_article(10, "新規A")], client,
                                        [[self.candidate("再挑戦", 1), self.candidate("別候補", 2)]], rejected)
        self.assertIn("rejected-today", [a.get("id") for a in calls[0]["avoid"]])
        self.assertNotIn("再挑戦", client.calls)  # 同じ一次情報・対象は生成・監査し直さない
        self.assertEqual(report["accepted_ids"], [301])

    def test_quality_bar_is_not_lowered_to_reach_three(self):
        client = FakeClient({"新規A": [CORE_CONTRADICTED], "B": [CORE_CONTRADICTED], "C": [CORE_CONTRADICTED],
                             "D": [CORE_CONTRADICTED]})
        report, _ = self.run_refill([today_article(1), today_article(2)], [today_article(10, "新規A")], client,
                                    [[self.candidate("B", 1)], [self.candidate("C", 2)], [self.candidate("D", 3)]])
        self.assertEqual(report["accepted_ids"], [])  # 矛盾がある記事は1本足りなくても公開しない
        self.assertEqual(len(report["held"]), 4)
        self.assertNotIn("confirmed", {h["verdict"] for h in report["held"]})


# ---------- 例外で止まった run・GitHub 障害 ----------

class CrashAndGitHubFailureTest(StateTestCase):
    def test_crash_with_system_error_stops_the_day(self):
        ds.save_context({"date": DATE, "round": 1})
        store = ds.MemoryStore()
        self.assertTrue(ds.mark_system_failure_from_crash(AuthenticationError("invalid x-api-key"), "gen", store))
        self.assertEqual(store.days[DATE]["status"], ds.SYSTEM_FAILURE)

    def test_crash_with_ordinary_error_does_not_stop(self):
        ds.save_context({"date": DATE, "round": 1})
        store = ds.MemoryStore()
        self.assertFalse(ds.mark_system_failure_from_crash(KeyError("title"), "gen", store))
        self.assertEqual(store.saved, [])

    def test_github_outage_stops_and_fails(self):
        gh = FakeGitHub({("GET", "/repos/o/r/pulls?"): urllib.error.HTTPError("u", 503, "x", {}, None)})
        store = ds.MemoryStore()
        code = dpr.main(["plan", "--date", DATE], gh=gh, store=store)
        self.assertEqual(code, 1)
        self.assertEqual(store.days[DATE]["status"], ds.SYSTEM_FAILURE)


class GitStoreTest(unittest.TestCase):
    """データbranchへの保存(一時リポジトリだけを使う。本物の origin には触れない)"""

    def test_save_and_load_roundtrip_unions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            remote, work = os.path.join(d, "remote.git"), os.path.join(d, "work")
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(["git", "init", "-q", work], check=True)
            subprocess.run(["git", "-C", work, "remote", "add", "origin", remote], check=True)

            def git(*args, input_text=None, check=True):
                return subprocess.run(["git", "-C", work, *args], input=input_text, capture_output=True,
                                      text=True, check=check)

            with mock.patch.object(ds, "REMOTE", "origin"), mock.patch.object(ds, "BRANCH", "bot/daily-state"):
                store = ds.GitStore(git)
                self.assertEqual(store.load(DATE)["status"], ds.INCOMPLETE)  # branch がまだ無い
                s = store.load(DATE)
                ds.add_rejected(s, [ds.compact({"title": "A"}, "x", 0)])
                store.save(s)
                s2 = store.load(DATE)
                s2["rejected"] = [ds.compact({"title": "B"}, "x", 1)]
                s2["status"] = ds.COMPLETE
                store.save(s2)
                loaded = store.load(DATE)
            self.assertEqual(loaded["status"], ds.COMPLETE)
            self.assertEqual(sorted(r["title"] for r in loaded["rejected"]), ["A", "B"])
            files = subprocess.run(["git", "-C", remote, "ls-tree", "-r", "--name-only", "bot/daily-state"],
                                   capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(files, ["data/daily_state.json"])


if __name__ == "__main__":
    unittest.main()
