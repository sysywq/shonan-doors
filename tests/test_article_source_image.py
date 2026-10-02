import os
import tempfile
import unittest

import article_source_image as asi


class ArticleSourceImageTests(unittest.TestCase):
    def test_only_allowlisted_source_is_used(self):
        self.assertTrue(asi.allowed_source("https://prtimes.jp/main/html/rd/p/123.html"))
        self.assertFalse(asi.allowed_source("https://shonanjin.com/news/example"))
        self.assertFalse(asi.allowed_source("https://example.com/photo.jpg"))

    def test_extract_og_image(self):
        doc = '<html><head><meta property="og:image" content="/img/main.jpg"></head></html>'
        self.assertEqual(
            asi.extract_og_image(doc, "https://prtimes.jp/main/html/x.html"),
            "https://prtimes.jp/img/main.jpg",
        )

    def test_enrich_and_credit(self):
        article = {
            "id": 999, "title": "test",
            "sources": ["https://prtimes.jp/main/html/rd/p/test.html"],
            "link": "",
        }
        page = b'<meta property="og:image" content="https://cdn.example.test/main.jpg">'

        def getter(url, _limit):
            if "prtimes.jp" in url:
                return page, "text/html", url
            return b"jpeg", "image/jpeg", url

        old_dir = asi.IMAGE_DIR
        try:
            with tempfile.TemporaryDirectory() as d:
                asi.IMAGE_DIR = d
                updated, changed = asi.enrich_article(article, getter=getter)
                self.assertTrue(changed)
                self.assertEqual(updated["heroImage"], "/assets/images/articles/999.jpg")
                self.assertEqual(updated["heroImageCredit"], "画像出典：PR TIMES")
                self.assertEqual(updated["heroImageSourceUrl"], article["sources"][0])
                self.assertTrue(os.path.exists(os.path.join(d, "999.jpg")))
        finally:
            asi.IMAGE_DIR = old_dir

    def test_non_allowlisted_article_falls_back(self):
        article = {"id": 1, "sources": ["https://example.com/news"], "link": ""}
        updated, changed = asi.enrich_article(article, getter=lambda *_: self.fail("must not fetch"))
        self.assertFalse(changed)
        self.assertEqual(updated, article)


if __name__ == "__main__":
    unittest.main()
