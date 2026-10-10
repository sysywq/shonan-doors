# -*- coding: utf-8 -*-
"""Daily Publication Watchdog(daily_watchdog.py)の判定のテスト。GitHub API・本番URLは呼ばない。
実行: python -m unittest tests/test_daily_watchdog.py -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import daily_state as ds  # noqa: E402
import daily_watchdog as wd  # noqa: E402

DATE = "2026-10-10"
# JST 09:12(10/10 に実行中の run を誤って停滞と判定した時刻)
NOW = datetime(2026, 10, 10, 0, 12, tzinfo=timezone.utc)
ARTICLES = [(184, "kamakura-event-0184"), (185, "zushi-life-0185"), (186, "fujisawa-gourmet-0186")]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(i, status="completed", conclusion="success", created=None, started=None, updated=None, now=NOW):
    created = created or now - timedelta(minutes=30)
    return {"id": i, "status": status, "conclusion": conclusion if status == "completed" else None,
            "created_at": iso(created), "run_started_at": iso(started or created),
            "updated_at": iso(updated or created + timedelta(minutes=1))}


def state(status=ds.INCOMPLETE):
    return ds.new_state(DATE) | {"status": status}


class DecideTest(unittest.TestCase):
    def decide(self, runs=(), st=None, today=(), verified=(), now=NOW, **kw):
        return wd.decide(now, list(runs), st or state(), list(today), list(verified), **kw)

    def test_verified_three_articles_is_ok_even_while_running(self):
        action, _, _ = self.decide([run(1, status="in_progress")], today=ARTICLES, verified=[184, 185, 186])
        self.assertEqual(action, wd.OK)

    def test_in_progress_run_with_old_updated_at_is_not_stale(self):
        # 10/10: 08:49 JST に始まった run の updated_at は開始直後のまま。23分後でも実行中として待つ
        started = NOW - timedelta(minutes=23)
        r = run(38006270615, status="in_progress", created=started, updated=started + timedelta(seconds=3))
        action, reason, _ = self.decide([r])
        self.assertEqual(action, wd.WAIT)
        self.assertIn("38006270615", reason)

    def test_queued_run_blocks_dispatch(self):
        self.assertEqual(self.decide([run(2, status="pending")])[0], wd.WAIT)

    def test_run_beyond_job_timeout_is_stale_and_cancelled_on_dispatch(self):
        started = NOW - timedelta(minutes=wd.STALE_MIN + 5)
        action, reason, detail = self.decide([run(3, status="in_progress", created=started)])
        self.assertEqual(action, wd.DISPATCH)
        self.assertEqual(detail["stale_run_ids"], [3])

    def test_before_schedule_waits(self):
        early = datetime(2026, 10, 9, 20, 10, tzinfo=timezone.utc)  # JST 05:10
        self.assertEqual(self.decide(now=early)[0], wd.WAIT)

    def test_missing_cron_dispatches(self):
        late = datetime(2026, 10, 9, 20, 23, tzinfo=timezone.utc)  # JST 05:23、当日の run なし
        yesterday = run(9, created=late - timedelta(hours=20))
        action, reason, _ = self.decide([yesterday], now=late)
        self.assertEqual(action, wd.DISPATCH)
        self.assertIn("cron", reason)

    def test_failed_run_dispatches(self):
        action, reason, _ = self.decide([run(4, conclusion="failure")])
        self.assertEqual(action, wd.DISPATCH)
        self.assertIn("失敗", reason)

    def test_success_but_short_dispatches(self):
        action, reason, _ = self.decide([run(5)], today=ARTICLES[:2], verified=[184, 185])
        self.assertEqual(action, wd.DISPATCH)
        self.assertIn("3本未達", reason)

    def test_cooldown_after_run_end(self):
        r = run(6, created=NOW - timedelta(minutes=40), updated=NOW - timedelta(minutes=2))
        self.assertEqual(self.decide([r])[0], wd.WAIT)

    def test_stopped_day_is_not_redispatched(self):
        for status in ds.STOPPED:
            self.assertEqual(self.decide([run(7, conclusion="failure")], st=state(status))[0], wd.STOPPED)

    def test_closed_daily_pr_is_not_redispatched(self):
        self.assertEqual(self.decide([run(8)], closed_daily_prs=[250])[0], wd.STOPPED)

    def test_run_budget_halts(self):
        runs = [run(i, created=NOW - timedelta(minutes=60 + i)) for i in range(wd.max_runs_per_day())]
        self.assertEqual(self.decide(runs)[0], wd.HALT)

    def test_consecutive_failures_halt(self):
        runs = [run(i, conclusion="failure", created=NOW - timedelta(minutes=60 + i))
                for i in range(wd.MAX_CONSECUTIVE_FAILURES)]
        self.assertEqual(self.decide(runs)[0], wd.HALT)

    def test_production_not_reflected_requests_redeploy_not_generation(self):
        action, reason, _ = self.decide([run(10)], today=ARTICLES, verified=[184], last_main_push_min=45)
        self.assertEqual(action, wd.REDEPLOY)

    def test_production_reflection_grace(self):
        self.assertEqual(self.decide([run(11)], today=ARTICLES, verified=[], last_main_push_min=5)[0], wd.WAIT)

    def test_redeploy_limit_halts(self):
        action, _, _ = self.decide([run(12)], today=ARTICLES, verified=[], last_main_push_min=90,
                                   redeploys_today=wd.MAX_REDEPLOYS)
        self.assertEqual(action, wd.HALT)

    def test_runs_from_previous_day_do_not_count(self):
        old = [run(i, conclusion="failure", created=NOW - timedelta(hours=12, minutes=i)) for i in range(10)]
        self.assertEqual(self.decide(old)[0], wd.DISPATCH)


class ProductionTest(unittest.TestCase):
    def test_uses_slug_url_and_requires_listing(self):
        seen = []

        def check(url):
            seen.append(url)
            return True

        listing = '<a href="/articles/kamakura-event-0184/"></a><a href="/articles/zushi-life-0185/"></a>'
        ids, status = wd.verify_production(ARTICLES, check=check, listing=listing)
        self.assertEqual(ids, [184, 185])
        self.assertIn("https://www.shonandoors.com/articles/kamakura-event-0184/", seen)
        self.assertFalse(any(u.endswith("/articles/184/") for u in seen))
        self.assertFalse(status[186]["listed"])

    def test_today_articles_skip_merged(self):
        arts = [{"id": 1, "slug": "a", "date": DATE}, {"id": 2, "slug": "b", "date": DATE, "mergedInto": 1},
                {"id": 3, "slug": "c", "date": "2026-10-09"}]
        self.assertEqual(wd.today_articles_from(arts, DATE), [(1, "a")])


if __name__ == "__main__":
    unittest.main()
