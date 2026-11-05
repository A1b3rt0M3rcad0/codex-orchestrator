from __future__ import annotations
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from codex_orchestrator.budget import Ledger, UsageObserver, price, tokens, validate_usage
from codex_orchestrator.config import DEFAULTS
from codex_orchestrator.contracts import BudgetError

USAGE={'input_tokens':100,'cached_input_tokens':20,'output_tokens':10,'reasoning_output_tokens':4}
RATES={'input':'2','cached_input':'0.5','output':'10'}

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS);self.cfg['budgets'].update(call_tokens=200,run_tokens=1000,task_tokens=500)
        self.records=[];self.ledger=Ledger(self.cfg,self.records,threading.RLock(),lambda:None)

    def reserve(self, task='T1', role='luna', ancestors=None):
        return self.ledger.reserve(role,task,ancestors or [task],'implement')

    def test_cached_and_reasoning_tokens_not_double_counted(self):
        self.assertEqual(tokens(validate_usage(USAGE)),110)
        self.assertEqual(price(USAGE,RATES),Decimal('0.00027'))

    def test_unknown_price_remains_unknown(self):
        self.assertIsNone(price(USAGE,None))

    def test_invalid_telemetry_fails_closed(self):
        for value in [None,{}, {'input_tokens':-1,'output_tokens':2}, {**USAGE,'cached_input_tokens':101},
                      {**USAGE,'reasoning_output_tokens':11},{**USAGE,'input_tokens':True}]:
            with self.subTest(value=value),self.assertRaises(BudgetError):validate_usage(value)

    def test_reservations_are_atomic_under_concurrency(self):
        self.cfg['budgets']['run_tokens']=200
        def admit(i):
            try:self.reserve('T'+str(i));return True
            except BudgetError:return False
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(admit,range(8)))
        self.assertEqual(sum(results),1);self.assertEqual(len(self.records),1)

    def test_subtree_children_charge_parent(self):
        self.cfg['budgets']['task_tokens']=200
        self.reserve('child1',ancestors=['P','child1'])
        with self.assertRaisesRegex(BudgetError,'P'):self.reserve('child2',ancestors=['P','child2'])
        self.assertEqual(len(self.records),1)

    def test_completed_usage_releases_unused_reservation(self):
        r=self.reserve();self.ledger.finish(r,USAGE,True,'completed')
        self.assertEqual(self.ledger.totals()[0],110)
        self.assertEqual(self.reserve()['reserved_tokens'],200)

    def test_unknown_usage_keeps_reservation_and_stops_new_calls(self):
        r=self.reserve()
        with self.assertRaisesRegex(BudgetError,'Missing'):self.ledger.finish(r,None,False,'failed')
        self.assertEqual(r['status'],'unknown');self.assertEqual(self.ledger.totals()[0],200)
        with self.assertRaisesRegex(BudgetError,'Unresolved'):self.reserve('T2')

    def test_actual_overrun_is_recorded_not_clipped(self):
        r=self.reserve();over={**USAGE,'input_tokens':250}
        with self.assertRaisesRegex(BudgetError,'Per-call'):self.ledger.observe(r,over)
        self.assertEqual(r['usage']['input_tokens'],250);self.assertEqual(self.ledger.charge(r)[0],260)

    def test_call_and_per_task_count_budgets(self):
        self.cfg['budgets']['task_calls']=1;r=self.reserve();self.ledger.finish(r,USAGE,True,'completed')
        with self.assertRaisesRegex(BudgetError,'subtree call'):self.reserve()
        self.cfg['runtime']['max_calls']=1
        with self.assertRaisesRegex(BudgetError,'Model call'):self.reserve('T2')

    def test_per_model_ceiling(self):
        self.cfg['budgets']['model_tokens']={'luna':200};self.reserve('T1')
        with self.assertRaisesRegex(BudgetError,'luna'):self.reserve('T2')
        self.reserve('T3','terra')

    def test_financial_reservation_admission(self):
        self.cfg['prices']={'luna':RATES};self.cfg['budgets']['run_cost_usd']='0.001'
        with self.assertRaises(BudgetError):self.reserve()
        self.assertEqual(self.records,[])

    def test_repaired_task_uses_original_ancestor_cap(self):
        self.cfg['budgets']['task_calls']=1;r=self.reserve();self.ledger.finish(r,USAGE,True,'completed')
        with self.assertRaises(BudgetError):self.reserve('repair',ancestors=['T1','repair'])

    def test_strict_mode_never_admits_unenforceable_cli_cap(self):
        self.cfg['budgets']['mode']='strict'
        with self.assertRaisesRegex(BudgetError,'hard per-request'):self.reserve()
        self.assertEqual(self.records,[])

    def test_persisted_records_cannot_reset_consumption(self):
        r=self.reserve();self.ledger.finish(r,USAGE,True,'completed')
        self.cfg['budgets']['task_tokens']=200
        restored=json.loads(json.dumps(self.records))
        ledger=Ledger(self.cfg,restored,threading.RLock(),lambda:None)
        with self.assertRaises(BudgetError):ledger.reserve('luna','T1',['T1'],'implement')

    def test_request_and_turn_telemetry_not_added_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'events';q=Path(temp)/'requests';r=self.reserve()
            p.write_text(json.dumps({'type':'turn.completed','usage':USAGE})+'\n')
            q.write_text(json.dumps({'usage':USAGE})+'\n')
            observer=UsageObserver(p,self.ledger,r,q);observer();observer()
            self.assertEqual(tokens(observer.best_usage()),110)
            self.assertEqual(r['usage'],USAGE)

    def test_partial_event_and_multiple_turns(self):
        self.cfg['budgets']['call_tokens']=300
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'events';r=self.reserve();observer=UsageObserver(p,self.ledger,r)
            data=json.dumps({'type':'turn.completed','usage':USAGE})
            p.write_text(data[:10]);observer();self.assertIsNone(observer.usage)
            p.write_text(data+'\n'+data+'\n');observer()
            self.assertEqual(tokens(observer.usage),220)

    def test_optional_unknown_hook_shape_does_not_forge_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'events';q=Path(temp)/'requests';r=self.reserve()
            q.write_text(json.dumps({'usage':{'different_schema':99}})+'\n')
            observer=UsageObserver(p,self.ledger,r,q);observer();self.assertIsNone(observer.best_usage())
