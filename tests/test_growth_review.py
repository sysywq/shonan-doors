import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import growth_review as gr


class GrowthReviewTest(unittest.TestCase):
    def setUp(self):
        self.proposal = ["Analysis_ID", "Status", "Entity_Type", "Entity_ID", "Observation"]
        self.articles = [{"id": 81, "slug": "fujisawa-gourmet-0081", "title": "現行見出し",
                          "dek": "現行説明", "sources": ["https://example.org/"]}]

    def test_only_exactly_one_approved_article_can_be_reviewed(self):
        tables = {"AI_Analysis": [self.proposal, ["a", "Proposed", "Article", "81", "20 impressions"]]}
        with self.assertRaisesRegex(ValueError, "Approved"):
            gr.review_packet(tables, self.articles, "a")
        tables["AI_Analysis"][1][1] = "Approved"
        proposal, article = gr.review_packet(tables, self.articles, "a")
        packet = gr.render(proposal, article)
        self.assertIn("20 impressions", packet)
        self.assertIn("現行見出し", packet)
        self.assertIn("https://example.org/", packet)
        tables["AI_Analysis"].append(tables["AI_Analysis"][1])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            gr.review_packet(tables, self.articles, "a")

    def test_merged_or_missing_article_is_rejected(self):
        tables = {"AI_Analysis": [self.proposal, ["a", "Approved", "Article", "81", ""]]}
        with self.assertRaisesRegex(ValueError, "missing or merged"):
            gr.review_packet(tables, [], "a")
        with self.assertRaisesRegex(ValueError, "missing or merged"):
            gr.review_packet(tables, [{**self.articles[0], "mergedInto": 82}], "a")


if __name__ == "__main__":
    unittest.main()
