import unittest
from datetime import datetime, timezone
from scripts.claude_budget import reserve, settle, BudgetExceeded

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.p = dict(monthly_usd=80, daily_usd=2.5, reservation_usd=.5,
                      historical_spend_usd={'2026-11':100})
        self.now = datetime(2026,12,10,5,tzinfo=timezone.utc)
    def test_reported_exhaustion_blocks_november_and_resets_in_december(self):
        with self.assertRaises(BudgetExceeded):
            reserve({},self.p,datetime(2026,11,30,23,tzinfo=timezone.utc),'a','news')
        self.assertEqual(reserve({},self.p,self.now,'a','news')['calls']['a']['usd'],.5)
    def test_outstanding_reservations_survive_rerun_and_refuse_sixth_call(self):
        s={}
        for i in range(5):s=reserve(s,self.p,self.now,str(i),'news')
        with self.assertRaises(BudgetExceeded):reserve(s,self.p,self.now,'6','news')
        self.assertEqual(len(reserve(s,self.p,self.now,'1','news')['calls']),5)
    def test_actual_search_inclusive_cost_stops_later_calls(self):
        s=reserve({},self.p,self.now,'a','audit')
        s=settle(s,'a',2.4)
        with self.assertRaises(BudgetExceeded):reserve(s,self.p,self.now,'b','news')
    def test_early_month_cannot_consume_full_month_allowance(self):
        from unittest.mock import patch
        now=datetime(2026,12,1,1,tzinfo=timezone.utc)
        s=reserve({},self.p,now,'a','audit');s=settle(s,'a',2.4)
        with patch.dict('os.environ', CLAUDE_BUDGET_PURPOSE='optional'), self.assertRaises(BudgetExceeded):reserve(s,self.p,now,'b','audit')
    def test_unknown_cost_keeps_reservation(self):
        s=reserve({},self.p,self.now,'a','news')
        self.assertEqual(settle(s,'a',None)['calls']['a']['status'],'reserved')

    def test_compare_and_swap_conflict_rechecks_updated_spend(self):
        import base64,json,urllib.error
        from scripts.claude_budget import GitHubLedger
        holder=self
        class ConflictingLedger(GitHubLedger):
            def __init__(self):self.puts=0;self.state={'calls':{}}
            def initialize(self):pass
            def request(self,method,path,data=None):
                if method=='GET':return dict(sha=str(self.puts),content=base64.b64encode(json.dumps(self.state).encode()).decode())
                self.puts+=1
                if self.puts==1:
                    self.state=reserve({},holder.p,holder.now,'another-job','audit')
                    self.state=settle(self.state,'another-job',2.3)
                    raise urllib.error.HTTPError(path,409,'conflict',{},None)
                raise AssertionError('Should refuse after another run spent the allowance')
        ledger=ConflictingLedger()
        with self.assertRaises(BudgetExceeded):
            ledger.update(lambda s:reserve(s,self.p,self.now,'our-call','news'))
        self.assertEqual(ledger.puts,1)

    def test_one_article_configuration_stops_after_first_confirmed(self):
        import daily_state as ds
        from editorial_selection import plan
        self.assertEqual(ds.next_action(1,0,minimum=1,max_rounds=0),ds.COMPLETE)
        self.assertEqual(ds.next_action(0,0,minimum=1,max_rounds=0),ds.ROUND_LIMIT)
        candidates=[dict(id='a',area='藤沢',cat='g'),dict(id='b',area='鎌倉',cat='c')]
        judgments={'a':dict(demand=25,timing=20,usefulness=20,originality=15),
                   'b':dict(demand=1,timing=1,usefulness=1,originality=1)}
        ranked=plan(candidates,judgments,{},[],'2026-12-02',target=1)
        self.assertEqual(ranked[0][0]['id'],'a')
        self.assertEqual(ranked[0][1]['slot'],'regular')

    def test_midmonth_installation_does_not_wait_for_old_spend_to_catch_up(self):
        from unittest.mock import patch
        p=dict(monthly_usd=190,daily_usd=8,reservation_usd=.5,historical_spend_usd={'2026-11':100})
        now=datetime(2026,11,7,1,tzinfo=timezone.utc)
        with patch.dict('os.environ',CLAUDE_BUDGET_PURPOSE='minimum'):
            result=reserve({},p,now,'first','news')
        self.assertEqual(result['calls']['first']['usd'],.5)
    def test_optional_work_cannot_spend_future_minimum_budget(self):
        from unittest.mock import patch
        p=dict(monthly_usd=190,daily_usd=8,reservation_usd=.5,minimum_daily_reserve_usd=4,historical_spend_usd={'2026-11':100})
        with patch.dict('os.environ',CLAUDE_BUDGET_PURPOSE='optional'), self.assertRaises(BudgetExceeded):
            reserve({},p,datetime(2026,11,7,1,tzinfo=timezone.utc),'extra','growth')
