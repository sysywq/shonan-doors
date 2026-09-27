import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import growth_autorevise as ar
import growth_feedback as gf


class GrowthAutoRevisionTest(unittest.TestCase):
    def tables(self, now, count=14, impressions=120, query='湘南 コーヒー'):
        dates = [(now - dt.timedelta(days=i)).isoformat() for i in range(1, count + 1)]
        path = 'https://www.shonandoors.com/articles/cafe-0081/'
        t = {tab: [['Date']] for tab in gf.TABS}
        t['GSC_Daily'] = [['Date', 'Clicks', 'Impressions']] + [[d, 1, 200] for d in dates]
        t['GSC_Query_Page'] = [['Date', 'Query', 'Page', 'Clicks', 'Impressions', 'Average_Position'],
                               [dates[0], query, path, 0, impressions, 10]]
        t['Article_Master'] = [['Article_ID', 'Canonical_Path'], [81, '/articles/cafe-0081/']]
        t['Action_Log'] = [['Action_ID', 'Timestamp', 'Article_ID', 'URL', 'Action_Type', 'Reason',
                            'Before', 'After', 'Actor', 'Experiment_ID', 'Notes']]
        return t

    def article(self, now, age=40, title='湘南の焙煎所'):
        return {'id': 81, 'slug': 'cafe-0081', 'title': title, 'dek': '焙煎を楽しむ',
                'date': (now - dt.timedelta(days=age)).isoformat(), 'articleType': 'news'}

    def test_skips_sparse_recent_and_already_matching(self):
        now = dt.date(2026, 10, 20)
        for count, impressions, age, title in [(7, 120, 40, '湘南の焙煎所'),
                                                (14, 21, 40, '湘南の焙煎所'),
                                                (14, 120, 7, '湘南の焙煎所'),
                                                (14, 120, 40, '湘南 コーヒーの店')]:
            t, a = self.tables(now, count, impressions), self.article(now, age, title)
            self.assertEqual(ar.eligible(t, [a], gf.analyze(t, [a], now), now), [])

    def test_eligible_mature_article_and_deduplication(self):
        now = dt.date(2026, 10, 20)
        t, a = self.tables(now), self.article(now)
        signal = gf.analyze(t, [a], now)
        self.assertEqual(len(ar.eligible(t, [a], signal, now)), 1)
        t['Action_Log'].append(['id', '', 81, '', 'AUTO_TITLE_DEK'])
        self.assertEqual(ar.eligible(t, [a], signal, now), [])

    def test_audit_must_pass_and_cannot_change_body(self):
        now = dt.date(2026, 10, 20)
        t, a = self.tables(now), self.article(now)
        candidate = dict(a, title='湘南 コーヒーの焙煎所', dek='湘南のコーヒーを焙煎所で楽しむ')
        with mock.patch.object(ar, 'draft', return_value=candidate), \
             mock.patch('publish_gate.check_draft', return_value={'passed': False, 'verdict': 'review_required'}):
            self.assertIsNone(ar.prepare(t, [a], object(), now))
        changed = dict(candidate, body='new unsupported detail')
        with mock.patch.object(ar, 'draft', return_value=candidate), \
             mock.patch('publish_gate.check_draft', return_value={'passed': True, 'entry': changed}):
            self.assertIsNone(ar.prepare(t, [a], object(), now))

    def test_action_logged_only_once(self):
        now = dt.date(2026, 10, 20)
        t = self.tables(now)
        meta = {'action_id': 'growth-auto-key', 'article_id': 81, 'slug': 'cafe-0081',
                'before': {'title': 'a', 'dek': 'b'}, 'after': {'title': 'c', 'dek': 'd'},
                'query': '湘南 コーヒー', 'period': ['2026-10-13', '2026-10-19'],
                'baseline': {'impressions': 120, 'clicks': 0, 'position': 10}}
        with mock.patch.object(ar, 'append_action') as append:
            ar.append_once('id', t, meta)
            append.assert_called_once()
            self.assertEqual(append.call_args.args[1][4], 'AUTO_TITLE_DEK')
            t['Action_Log'].append(['growth-auto-key'])
            ar.append_once('id', t, meta)
            append.assert_called_once()


if __name__ == '__main__':
    unittest.main()
