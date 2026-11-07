import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import audit_receipts as ar

class ReceiptTests(unittest.TestCase):
    def test_only_identical_content_and_same_run_can_reuse(self):
        result={'verdict':'confirmed','claims':[], 'primarySources':['https://official.example'],
                'anomalies':False,'hasQuotedComment':False,'needsHuman':False}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,DAILY_REUSE_FULL_AUDIT='1',GITHUB_RUN_ID='123'), patch.object(ar,'location',side_effect=lambda e:Path(d)/(ar.fingerprint(e)+'.json')):
            entry=dict(id='pending',slug='',title='店',body='料金100円',sources=['https://official.example'])
            with patch('publish_gate.evaluate',return_value=(True,[],[])):
                ar.save(entry,result)
                self.assertIsNotNone(ar.load(dict(entry,id=42,slug='shop-0042')))
                for field,value in [('body','料金200円'),('sources',['https://different.example']),('title','別の店')]:
                    self.assertIsNone(ar.load(dict(entry,**{field:value})))
                with patch.dict(os.environ,GITHUB_RUN_ID='124'):self.assertIsNone(ar.load(entry))
                with patch('publish_gate.evaluate',return_value=(False,['bad'],[])):self.assertIsNone(ar.load(entry))

    def test_full_audit_then_final_audit_reuses_real_confirmed_result(self):
        from test_daily_pipeline import CONFIRMED, article
        from test_publish_gate import FakeClient
        import publish_gate as pg
        import daily_fact_audit as dfa
        import fact_audit as fa
        entry=article(0,'Same original')
        client=FakeClient({'Same original':[CONFIRMED,CONFIRMED]})
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,DAILY_REUSE_FULL_AUDIT='1',GITHUB_RUN_ID='same-run'), patch.object(ar,'location',side_effect=lambda e:Path(d)/(ar.fingerprint(e)+'.json')), patch.object(fa,'official_page_images',return_value=[]):
            checked=pg.check_draft(entry,client=client)
            self.assertTrue(checked['passed'])
            with patch.object(pg,'_audit_safely',side_effect=AssertionError('second paid audit')):
                final=dfa.audit_article(client,dict(checked['entry'],id=42,slug='fujisawa-event-0042'))
                self.assertTrue(final['confirmed'])
            self.assertIsNone(ar.load(dict(entry,body='Changed facts')))
