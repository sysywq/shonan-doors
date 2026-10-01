import os
import unittest
from unittest import mock

import x_signal


class XSignalTest(unittest.TestCase):
    def test_recent_post_becomes_metadata_lead(self):
        response = {
            "data": [{
                "id": "123",
                "author_id": "u1",
                "text": "藤沢駅前に新しいカフェが10月オープン予定です",
                "created_at": "2026-10-01T10:00:00Z",
                "public_metrics": {
                    "like_count": 10,
                    "retweet_count": 2,
                    "reply_count": 1,
                    "quote_count": 0,
                },
            }],
            "includes": {"users": [{"id": "u1", "username": "sample_shop", "name": "Sample"}]},
        }
        with mock.patch.dict(os.environ, {
            "X_BEARER_TOKEN": "test-token",
            "X_MAX_QUERIES": "1",
        }, clear=False), mock.patch.object(x_signal, "_get", return_value=response):
            leads = x_signal.discover_x_leads(["藤沢"])
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["leadSourceType"], "x_recent_search")
        self.assertEqual(lead["sourceUrl"], "")
        self.assertEqual(lead["leadAuthor"], "sample_shop")
        self.assertEqual(lead["leadEngagement"], 13)
        self.assertIn("/status/123", lead["leadUrl"])

    def test_query_contains_change_terms_and_retweet_filter(self):
        query = x_signal._query_for("藤沢")
        self.assertIn("藤沢", query)
        self.assertIn("オープン", query)
        self.assertIn("-is:retweet", query)

    def test_missing_token_fails_soft(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(x_signal.discover_x_leads(["藤沢"]), [])


if __name__ == "__main__":
    unittest.main()
