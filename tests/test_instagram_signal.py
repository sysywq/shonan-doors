import os
import unittest
from unittest import mock

import instagram_signal as ig


class InstagramSignalTest(unittest.TestCase):
    def test_watchlist_from_articles_extracts_verified_instagram(self):
        articles = [{
            "area": "茅ヶ崎",
            "cat": "c",
            "title": "美術館の記事",
            "subjectNames": ["茅ヶ崎市美術館"],
            "snsLinks": {"instagram": "https://www.instagram.com/chigasakimuseum/"},
        }]
        watch = ig.watchlist_from_articles(articles)
        self.assertEqual(len(watch), 1)
        self.assertEqual(watch[0]["handle"], "chigasakimuseum")
        self.assertEqual(watch[0]["area"], "茅ヶ崎")

    def test_business_discovery_recent_change_post_becomes_lead(self):
        articles = [{
            "area": "茅ヶ崎",
            "cat": "c",
            "title": "美術館の記事",
            "subjectNames": ["茅ヶ崎市美術館"],
            "snsLinks": {"instagram": "https://www.instagram.com/chigasakimuseum/"},
        }]
        response = {
            "business_discovery": {
                "username": "chigasakimuseum",
                "name": "茅ヶ崎市美術館",
                "media": {"data": [{
                    "id": "m1",
                    "caption": "10月から新しい企画展を開催します",
                    "permalink": "https://www.instagram.com/p/test/",
                    "timestamp": "2099-10-01T00:00:00+0000",
                    "media_type": "IMAGE",
                }]}
            }
        }
        with mock.patch.dict(os.environ, {
            "INSTAGRAM_FACEBOOK_ACCESS_TOKEN": "token",
            "INSTAGRAM_BUSINESS_USER_ID": "123",
            "INSTAGRAM_MAX_ACCOUNTS": "1",
        }, clear=False), mock.patch.object(ig, "_business_discovery", return_value=response):
            leads = ig.discover_watchlist_leads(articles)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["leadSourceType"], "instagram_business_discovery")
        self.assertEqual(leads[0]["area"], "茅ヶ崎")
        self.assertEqual(leads[0]["sourceUrl"], "https://www.instagram.com/p/test/")

    def test_missing_facebook_login_credentials_fails_soft(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ig.discover_instagram_leads([]), [])

    def test_hashtag_lead_requires_change_signal(self):
        with mock.patch.dict(os.environ, {
            "INSTAGRAM_FACEBOOK_ACCESS_TOKEN": "token",
            "INSTAGRAM_BUSINESS_USER_ID": "123",
            "INSTAGRAM_HASHTAGS": "藤沢",
            "INSTAGRAM_MAX_HASHTAGS": "1",
        }, clear=False), mock.patch.object(ig, "_hashtag_id", return_value="h1"), mock.patch.object(
            ig, "_get", return_value={"data": [
                {"id": "m1", "caption": "藤沢で新店舗オープン予定", "permalink": "https://www.instagram.com/p/a/", "timestamp": "2099-10-01T00:00:00+0000"},
                {"id": "m2", "caption": "今日はいい天気です", "permalink": "https://www.instagram.com/p/b/", "timestamp": "2099-10-01T00:00:00+0000"},
            ]}
        ):
            leads = ig.discover_hashtag_leads()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["id"], "ig-hashtag-m1")


if __name__ == "__main__":
    unittest.main()
