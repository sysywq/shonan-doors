# -*- coding: utf-8 -*-
"""ニュース鮮度ゲート(news_freshness.py)と、全ニュース経路への組み込みのテスト。APIは呼ばない。
実行: python -m unittest tests/test_news_freshness.py -v
"""
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import daily_state as ds  # noqa: E402
import generate_articles as g  # noqa: E402
import news_freshness as nf  # noqa: E402

TODAY = "2026-10-09"


def opening(opening_date, title="新店がオープン", **kw):
    item = {"cat": "g", "newsKind": "store_opening", "title": title, "dek": "",
            "announcementDate": "", "openingDate": opening_date, "effectiveDate": "", "freshnessException": ""}
    item.update(kw)
    return item


def event(start, end="", **kw):
    item = {"cat": "e", "newsKind": "event", "title": "秋のまつり", "dek": "", "eventStartDate": start,
            "eventEndDate": end, "announcementDate": "", "openingDate": "", "effectiveDate": ""}
    item.update(kw)
    return item


def news(**kw):
    item = {"cat": "l", "newsKind": "news", "title": "市の新しい制度", "dek": "",
            "announcementDate": "", "openingDate": "", "effectiveDate": ""}
    item.update(kw)
    return item


class StoreOpeningTest(unittest.TestCase):
    def test_2026_10_09_incident_opened_9_16_is_rejected(self):
        # 本番の事例: 9/16開店の「油そば総本店×壱角家」を 10/9 に新店ニュースとして公開した(23日後)
        r = nf.check(opening("2026-09-16", title="油そばと家系ラーメン、二刀流で登場 - 「油そば総本店×壱角家」、"
                                                 "ジアウトレット湘南平塚に9月16日オープン",
                             announcementDate="2026-09-01"), TODAY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["days"], 23)
        self.assertIn("15日以上", r["reason"])

    def test_future_opening_announced_more_than_a_month_ago_is_allowed(self):
        # 9/30発表 → 10/30開店 → 10/9 記事化は OK(発表日の古さでは排除しない)
        self.assertTrue(nf.check(opening("2026-10-30", announcementDate="2026-09-30"), TODAY)["ok"])
        r = nf.check(opening("2026-10-30", announcementDate="2026-08-01"), TODAY)
        self.assertTrue(r["ok"])
        self.assertEqual(r["days"], -21)
        self.assertIn("開店まであと21日", r["reason"])

    def test_elapsed_day_bands(self):
        self.assertTrue(nf.check(opening("2026-10-09"), TODAY)["ok"])   # 当日
        self.assertTrue(nf.check(opening("2026-10-02"), TODAY)["ok"])   # 7日
        self.assertFalse(nf.check(opening("2026-10-01"), TODAY)["ok"])  # 8日・例外理由なし
        r = nf.check(opening("2026-10-01", freshnessException="開店記念の特典が10月20日まで"), TODAY)
        self.assertTrue(r["ok"])
        self.assertIn("例外", r["reason"])
        self.assertTrue(nf.check(opening("2026-09-25", freshnessException="特典継続中"), TODAY)["ok"])   # 14日
        self.assertFalse(nf.check(opening("2026-09-24", freshnessException="特典継続中"), TODAY)["ok"])  # 15日

    def test_unconfirmed_opening_date_is_not_filled_in(self):
        r = nf.check(opening(""), TODAY)
        self.assertFalse(r["ok"])
        self.assertIn("確認できていない", r["reason"])
        self.assertFalse(nf.check(opening("10月30日"), TODAY)["ok"])     # 形式不正も補完しない
        self.assertFalse(nf.check(opening("2026-02-30"), TODAY)["ok"])   # 存在しない日付

    def test_title_with_opening_words_is_judged_by_opening_date_even_if_mislabelled(self):
        item = news(title="「○○食堂」、平塚に9月16日オープン", announcementDate="2026-10-08")
        self.assertEqual(nf.classify(item), "store_opening")
        self.assertFalse(nf.check(item, TODAY)["ok"])

    def test_title_date_must_match_opening_date(self):
        r = nf.check(opening("2026-10-08", title="「○○」、9月16日オープン"), TODAY)
        self.assertFalse(r["ok"])
        self.assertIn("一致しない", r["reason"])
        self.assertTrue(nf.check(opening("2026-10-09", title="「くし葉」、10月9日グランドオープン"), TODAY)["ok"])

    def test_future_announcement_date_is_rejected(self):
        self.assertFalse(nf.check(opening("2026-10-30", announcementDate="2026-10-20"), TODAY)["ok"])


class EventTest(unittest.TestCase):
    def test_ended_event_is_rejected(self):
        r = nf.check(event("2026-10-03", "2026-10-05"), TODAY)
        self.assertFalse(r["ok"])
        self.assertIn("終了済み", r["reason"])
        self.assertFalse(nf.check(event("2026-10-08"), TODAY)["ok"])  # 単日・前日

    def test_ongoing_and_future_events_are_allowed_regardless_of_announcement(self):
        self.assertTrue(nf.check(event("2026-10-01", "2026-10-31"), TODAY)["ok"])  # 会期中
        self.assertTrue(nf.check(event("2026-10-09"), TODAY)["ok"])                 # 当日
        r = nf.check(event("2026-11-03", announcementDate="2026-07-01"), TODAY)
        self.assertTrue(r["ok"])
        self.assertIn("開催まであと25日", r["reason"])

    def test_event_needs_confirmed_start_date(self):
        self.assertFalse(nf.check(event(""), TODAY)["ok"])
        self.assertFalse(nf.check(event("2026-10-20", "2026-10-10"), TODAY)["ok"])  # 終了日が開始日より前

    def test_cat_e_is_always_an_event(self):
        self.assertEqual(nf.classify({"cat": "e", "newsKind": "store_opening", "title": "オープンガーデン"}), "event")


class OtherNewsTest(unittest.TestCase):
    def test_announcement_based(self):
        self.assertTrue(nf.check(news(announcementDate="2026-10-06"), TODAY)["ok"])
        self.assertFalse(nf.check(news(announcementDate="2026-09-19"), TODAY)["ok"])
        self.assertFalse(nf.check(news(), TODAY)["ok"])  # 日付が確認できない

    def test_future_effective_date_is_allowed(self):
        # 例: 9月に発表された「11月末で閉館」
        self.assertTrue(nf.check(news(announcementDate="2026-09-01", effectiveDate="2026-11-30"), TODAY)["ok"])
        # 実施日が過去なら、実施日から数える(発表が新しくても過去の出来事を速報扱いしない)
        self.assertFalse(nf.check(news(announcementDate="2026-10-08", effectiveDate="2026-09-01"), TODAY)["ok"])


class YearBoundaryAndTimezoneTest(unittest.TestCase):
    def test_year_boundary(self):
        self.assertTrue(nf.check(event("2027-01-03"), "2026-12-28")["ok"])
        self.assertFalse(nf.check(event("2026-01-03"), "2026-12-28")["ok"])  # 年の取り違え=終了済み
        self.assertTrue(nf.check(opening("2026-12-28"), "2027-01-02")["ok"])   # 5日
        self.assertFalse(nf.check(opening("2026-12-10"), "2027-01-02")["ok"])  # 23日
        self.assertTrue(nf.check(opening("2027-01-15"), "2026-12-20")["ok"])   # 年明けに開店予定
        self.assertFalse(nf.check(opening("2028-01-15"), "2026-12-20")["ok"])  # 1年以上先は年の誤りを疑う

    def test_today_is_computed_in_jst(self):
        # UTC 10/8 15:30 は JST 10/9 0:30
        self.assertEqual(nf.today_jst(datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)), "2026-10-09")
        self.assertEqual(nf.today_jst(datetime(2026, 12, 31, 15, 0, tzinfo=timezone.utc)), "2027-01-01")
        with self.assertRaises(ValueError):
            nf.today_jst(datetime(2026, 10, 9, 0, 0))

    def test_jst_boundary_changes_verdict(self):
        item = opening("2026-09-24", freshnessException="特典継続中")
        utc_evening = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)  # JST 10/8 1:00 → 14日目
        self.assertTrue(nf.check(item, nf.today_jst(utc_evening))["ok"])
        self.assertFalse(nf.check(item, nf.today_jst(datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)))["ok"])


class CandidateStageTest(unittest.TestCase):
    def test_stale_places_lead_is_dropped_before_drafting(self):
        lead = {"articleType": "news", "cat": "g", "titleIdea": "○○ - 平塚でオープン予定", "openingDate": "2026-09-16"}
        self.assertIn("15日以上", nf.candidate_is_stale(lead, TODAY))

    def test_candidates_without_dates_or_in_exception_band_are_kept_for_draft_check(self):
        self.assertIsNone(nf.candidate_is_stale({"articleType": "news", "cat": "g", "titleIdea": "新店"}, TODAY))
        self.assertIsNone(nf.candidate_is_stale({"articleType": "news", "cat": "g", "titleIdea": "新店がオープン",
                                                 "openingDate": "2026-09-29"}, TODAY))
        self.assertIsNone(nf.candidate_is_stale({"articleType": "news", "cat": "g", "titleIdea": "新店がオープン",
                                                 "openingDate": "2026-10-30", "announcementDate": "2026-09-30"},
                                                TODAY))

    def test_stock_is_never_blocked(self):
        self.assertIsNone(nf.candidate_is_stale({"articleType": "stock", "cat": "e", "eventStartDate": "2020-01-01"},
                                                TODAY))
        kept = g.final_freshness_guard([{"articleType": "stock", "cat": "g", "title": "鎌倉のパン屋ガイド"}],
                                       TODAY, [], record=False)
        self.assertEqual(len(kept), 1)


def draft(title, **kw):
    item = {"cat": "g", "area": "平塚", "scene": "mall", "title": title, "dek": f"{title}のリード",
            "body": "本文。" * 120, "tags": [title], "link": "https://example-shop.jp/news",
            "subjectNames": [title], "sources": ["https://example-shop.jp/news"],
            "eventStartDate": "", "eventEndDate": "", "eventSeriesKey": "",
            "newsKind": "store_opening", "announcementDate": "", "openingDate": "", "effectiveDate": "",
            "freshnessException": "", "address": "", "access": "", "hours": "", "closedDays": "",
            "instagram": "", "facebook": "", "x": "", "tiktok": ""}
    item.update(kw)
    return item


class NewsGenerationGateTest(unittest.TestCase):
    """run_news_generation(企画・横断選定・top-up・refill・post-audit top-up の共通経路)で鮮度を判定する。"""

    def setUp(self):
        g._freshness_rejections.clear()
        self.addCleanup(g._freshness_rejections.clear)

    def run_generation(self, batches, label="planned_fallback_1"):
        calls = iter(batches)
        audited = []

        def gate(entry, article_type, gate_rejections, log_lines):
            audited.append(entry["title"])
            return entry

        with mock.patch.object(g, "call_claude_news", lambda *a, **k: next(calls)), \
                mock.patch.object(g, "run_publish_gate", gate):
            accepted, log, _ = g.run_news_generation([], TODAY, [], gate_rejections=[], count=2, max_refills=1,
                                                     strict=False, label=label, defer_ids=True)
        return accepted, log, audited

    def test_stale_opening_is_rejected_before_fact_audit_and_fresh_one_is_kept(self):
        stale = draft("「油そば総本店×壱角家」、9月16日オープン", openingDate="2026-09-16", announcementDate="2026-09-01")
        future = draft("「○○ベーカリー」、10月30日オープン", openingDate="2026-10-30", announcementDate="2026-09-30")
        ended = draft("秋の市", cat="e", newsKind="event", eventStartDate="2026-10-04")
        accepted, log, audited = self.run_generation([[stale, future], [ended]])
        self.assertEqual([a["title"] for a in accepted], [future["title"]])
        self.assertNotIn(stale["title"], audited)  # 鮮度切れは監査(API)にかけない
        self.assertTrue(any("ニュース鮮度ゲート" in line and "23日経過" in line for line in log))
        self.assertEqual(accepted[0]["freshness"]["openingDate"], "2026-10-30")
        self.assertEqual([r["title"] for r in g._freshness_rejections], [stale["title"], "秋の市"])
        self.assertEqual(g.freshness_rejected_drafts()[0]["id"], "freshness-rejected")

    def test_shortfall_does_not_let_stale_news_through(self):
        # 目標に届かなくても基準外のニュースは採らない(refill しても鮮度切れなら0件のまま)
        stale = draft("「A店」、9月16日オープン", openingDate="2026-09-16")
        stale2 = draft("「B店」がオープン", openingDate="")
        accepted, _log, audited = self.run_generation([[stale], [stale2]])
        self.assertEqual(accepted, [])
        self.assertEqual(audited, [])
        self.assertEqual(len(g._freshness_rejections), 2)

    def test_final_guard_rechecks_before_ids_are_reserved(self):
        fresh = {"articleType": "news", "cat": "g", "title": "新店がオープン",
                 "freshness": {"kind": "store_opening", "openingDate": "2026-10-05"}}
        stale = {"articleType": "news", "cat": "g", "title": "新店がオープン2",
                 "freshness": {"kind": "store_opening", "openingDate": "2026-09-16"}}
        no_record = {"articleType": "news", "cat": "l", "title": "市の発表"}
        log = []
        kept = g.final_freshness_guard([fresh, stale, no_record], TODAY, log)
        self.assertEqual(kept, [fresh])
        self.assertEqual(len(log), 2)

    def test_rejections_are_avoided_in_later_rounds_of_the_same_day(self):
        report = {"freshness_rejected": [{"title": "「A店」、9月16日オープン", "area": "平塚", "cat": "g",
                                          "subjectNames": ["A店"], "reason": "開店から23日経過"}]}
        rejected = ds.rejected_from_run(report, [], 0)
        self.assertEqual(rejected[0]["subjectNames"], ["A店"])
        self.assertIn("ニュース鮮度ゲート", rejected[0]["reason"])

    def test_news_tool_schema_requires_the_separate_dates(self):
        required = g.NEWS_ARTICLE_TOOL["input_schema"]["properties"]["articles"]["items"]["required"]
        for key in ("newsKind", "announcementDate", "openingDate", "effectiveDate", "freshnessException",
                    "eventStartDate", "eventEndDate"):
            self.assertIn(key, required)


if __name__ == "__main__":
    unittest.main()
