import unittest
from datetime import datetime, timezone
from local_media_signal import headlines

class MediaTests(unittest.TestCase):
    def test_recent_headline_only_and_same_domain(self):
        xml=b'''<rss><channel><item><title>New cafe</title><link>https://shonan.keizai.biz/headline/1/</link><pubDate>Fri, 06 Nov 2026 12:00:00 GMT</pubDate></item><item><title>Wrong domain</title><link>https://unrelated.example/</link><pubDate>Fri, 06 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>'''
        rows=headlines(lambda url:xml,datetime(2026,11,7,tzinfo=timezone.utc))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['leadSourceType'],'local_media')
        self.assertNotIn('sourceUrl',rows[0])
    def test_failure_never_invents_a_candidate(self):
        def fail(url):raise OSError('unavailable')
        self.assertEqual(headlines(fail),[])
