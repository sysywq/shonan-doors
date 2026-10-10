# -*- coding: utf-8 -*-
"""Daily Publication Watchdog(daily_watchdog.py)の判定テスト。APIは呼ばない(GitHub API・本番URLはスタブ)。
実行: python -m unittest tests/test_daily_watchdog.py -v
"""
import base64
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import daily_state as ds  # noqa: E402
import daily_watchdog as wd  # noqa: E402

# 2026-10-10 09:12 JST
NOW = datetime(2026, 10, 10, 0, 12, tzinfo=timezone.utc)


def run(i, status="completed", event="schedule", created="2026-10-09T23:49:18Z", started=None, updated=None,
        actor="sysywq"):
    return {"id": i, "status": status, "event": event, "created_at": created,
            "run_started_at": started or created, "updated_at": updated or created,
            "triggering_actor": {"login": actor}}


def state(status=ds.INCOMPLETE, reason=""):
    s = ds.new_state("2026-10-10")
    s["status"] = status
    if reason:
        s["failure"] = {"reason": reason}
    return s


class DecideTest(unittest.TestCase):
    def test_running_daily_run_is_not_treated_as_stale(self):
        # 10/10 の誤起動: 23分前に始まった in_progress の run(updated_at は起動時のまま)を stale と判定して重複起動した
        runs = [run(1, status="in_progress", updated="2026-10-09T23:49:21Z")]
        action, _ = wd.decide(NOW, runs, state(), [], [])
        self.assertEqual(action, "wait")

    def test_pending_run_blocks_dispatch(self):
        runs = [run(2, status="pending", event="workflow_dispatch", created="2026-10-10T00:10:00Z")]
        self.assertEqual(wd.decide(NOW, runs, state(), [], [])[0], "wait")

    def test_run_past_job_timeout_is_stale_and_recovered(self):
        runs = [run(1, status="in_progress", created="2026-10-09T18:00:00Z")]
        self.assertEqual(wd.decide(NOW, runs, state(), [], [])[0], "dispatch")

    def test_missing_cron_dispatches_after_grace(self):
        runs = [run(9, created="2026-10-09T03:00:00Z")]  # 前日の run だけ
        self.assertEqual(wd.decide(NOW, runs, state(), [], [])[0], "dispatch")
        early = datetime(2026, 10, 9, 20, 7, tzinfo=timezone.utc)  # 05:07 JST
        self.assertEqual(wd.decide(early, runs, state(), [], [])[0], "wait")

    def test_published_target_is_ok(self):
        runs = [run(1, status="in_progress")]
        self.assertEqual(wd.decide(NOW, runs, state(), [1, 2, 3], [1, 2, 3])[0], "ok")

    def test_shortfall_after_completed_run_dispatches(self):
        runs = [run(1, created="2026-10-09T22:00:00Z", updated="2026-10-09T23:00:00Z")]
        self.assertEqual(wd.decide(NOW, runs, state(), [1, 2], [1, 2])[0], "dispatch")

    def test_cooldown_after_run_just_finished(self):
        runs = [run(1, created="2026-10-09T23:00:00Z", updated="2026-10-10T00:08:00Z")]
        self.assertEqual(wd.decide(NOW, runs, state(), [1], [1])[0], "wait")

    def test_stopped_day_is_not_restarted_by_bot(self):
        runs = [run(1, created="2026-10-09T22:00:00Z")]
        action, reason = wd.decide(NOW, runs, state(ds.SYSTEM_FAILURE, "usage limits"), [], [])
        self.assertEqual(action, "stopped")
        self.assertIn("usage limits", reason)

    def test_bot_dispatch_cap(self):
        cap = ds.DAILY_MAX_REFILL_ROUNDS + wd.WATCHDOG_EXTRA_DISPATCHES
        runs = [run(i, event="workflow_dispatch", created="2026-10-09T21:00:00Z", actor="github-actions[bot]")
                for i in range(cap)]
        self.assertEqual(wd.decide(NOW, runs, state(), [], [])[0], "limit")
        # 人の手動実行は上限に数えない
        runs = [run(i, event="workflow_dispatch", created="2026-10-09T21:00:00Z") for i in range(cap)]
        self.assertEqual(wd.decide(NOW, runs, state(), [], [])[0], "dispatch")

    def test_no_dispatch_late_night(self):
        late = datetime(2026, 10, 10, 13, 7, tzinfo=timezone.utc)  # 22:07 JST
        runs = [run(1, created="2026-10-09T22:00:00Z")]
        self.assertEqual(wd.decide(late, runs, state(), [1], [1])[0], "limit")

    def test_deploy_lag_detected_after_grace(self):
        runs = [run(1, created="2026-10-09T22:00:00Z")]
        recent = datetime(2026, 10, 10, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(wd.decide(NOW, runs, state(), [1, 2, 3], [1], recent)[0], "wait")
        old = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(wd.decide(NOW, runs, state(), [1, 2, 3], [1], old)[0], "deploy_lag")


class LiveArticlesTest(unittest.TestCase):
    def test_checks_slug_url_and_listing(self):
        # 旧 watchdog は /articles/<id>/ を見ていたため常に 404 だった。本番 URL は /articles/<slug>/
        rows = [{"id": 181, "slug": "a-0181"}, {"id": 182, "slug": "b-0182"}, {"id": 183, "slug": "c-0183"}]
        seen = []

        def check(url):
            seen.append(url)
            return True
        live = wd.live_articles(rows, check=check, listing='<a href="/articles/a-0181/"><a href="/articles/b-0182/">')
        self.assertEqual(live, [181, 182])
        self.assertIn("https://www.shonandoors.com/articles/a-0181/", seen)


class MainTest(unittest.TestCase):
    def fake(self, runs, articles, days=None):
        calls = []

        def request(method, path, token, payload=None):
            calls.append((method, path))
            if "/actions/workflows/" in path and method == "GET":
                return {"workflow_runs": runs}
            if "daily_state.json" in path:
                return {"content": base64.b64encode(json.dumps({"days": days or {}}).encode()).decode()}
            if "articles.json" in path:
                return {"content": base64.b64encode(json.dumps(articles).encode()).decode()}
            if path.endswith("/commits/main"):
                return {"commit": {"committer": {"date": "2026-10-09T04:17:23Z"}}}
            return None
        return request, calls

    def test_dispatches_once_when_short(self):
        request, calls = self.fake([run(1, created="2026-10-09T22:00:00Z")], [])
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_STEP_SUMMARY": ""}):
            code = wd.main(now=NOW, request=request, check=lambda u: False, listing="")
        self.assertEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "POST"],
                         [("POST", "/repos/o/r/actions/workflows/daily-articles.yml/dispatches")])

    def test_no_dispatch_while_daily_run_in_progress(self):
        request, calls = self.fake([run(1, status="in_progress")], [])
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_STEP_SUMMARY": ""}):
            self.assertEqual(wd.main(now=NOW, request=request, check=lambda u: False, listing=""), 0)
        self.assertFalse([c for c in calls if c[0] == "POST"])


class UsageLimitTest(unittest.TestCase):
    def test_usage_limit_is_system_failure(self):
        class BadRequestError(Exception):
            status_code = 400
        e = BadRequestError("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
                            "'message': 'You have reached your specified API usage limits. You will regain access "
                            "on 2026-11-01 at 00:00 UTC.'}}")
        self.assertTrue(ds.system_error_reason(e))


if __name__ == "__main__":
    unittest.main()
