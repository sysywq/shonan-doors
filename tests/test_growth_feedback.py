import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import generate_articles as ga
import growth_feedback as gf


class GrowthFeedbackTest(unittest.TestCase):
    def test_canonical_does_not_join_other_domains(self):
        self.assertEqual(gf.canonical("https://www.shonandoors.com/articles/fujisawa-event-0129/?ref=x"),
                         "/articles/fujisawa-event-0129/")
        self.assertEqual(gf.canonical("https://example.com/articles/fujisawa-event-0129/"), "")

    def test_observed_impressions_do_not_inflate_site_totals_or_infer_trend(self):
        today = dt.date(2026, 9, 27)
        dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(1, 8)]
        tables = {tab: [["Date"]] for tab in gf.TABS}
        tables["Article_Master"] = [["Article_ID", "Canonical_Path"], [129, "/articles/fujisawa-event-0129/"]]
        tables["GSC_Daily"] = [["Date", "Clicks", "Impressions"]] + [[d, 100, 1000] for d in dates]
        tables["GSC_Query_Page"] = [["Date", "Query", "Page", "Clicks", "Impressions", "Average_Position"],
                                    [dates[0], "藤沢 海", "https://www.shonandoors.com/articles/fujisawa-event-0129/", 1, 25, 8],
                                    [dates[0], "他", "https://other.example/articles/fujisawa-event-0129/", 0, 100, 5]]
        signal = gf.analyze(tables, [{"id": 129, "slug": "fujisawa-event-0129"}], today=today)
        self.assertFalse(signal["trend_available"])
        self.assertEqual(signal["unmapped_gsc_rows"], 1)
        self.assertEqual(len(signal["opportunities"]), 1)
        self.assertEqual(signal["opportunities"][0]["impressions"], 25)
        self.assertEqual(len(gf.proposals(signal, [["Analysis_ID"]])), 1)
        self.assertEqual(len(gf.proposals(signal, [["Analysis_ID"], [gf.proposals(signal, [["Analysis_ID"]])[0][0]]])), 0)

    def test_stale_counter_never_reissues_published_id(self):
        with tempfile.TemporaryDirectory() as directory:
            article_path = os.path.join(directory, "articles.json")
            counter_path = os.path.join(directory, "counter.json")
            with open(article_path, "w") as f:
                json.dump([{"id": 129}], f)
            with open(counter_path, "w") as f:
                json.dump({"next_id": 129}, f)
            with mock.patch.object(ga, "ARTICLES_JSON_PATH", article_path), mock.patch.object(ga, "ID_COUNTER_PATH", counter_path):
                self.assertEqual(ga.reserve_ids(2), [130, 131])
                self.assertEqual(ga.reserve_ids(1), [132])
            with open(counter_path) as f:
                self.assertEqual(json.load(f)["next_id"], 133)


if __name__ == "__main__":
    unittest.main()
