from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from codex_orchestrator.contracts import OrchestratorError, canonical
from codex_orchestrator.evidence import approval_payload, handoff
from codex_orchestrator.runtime import Engine, approve, gate, ready_batch, recover_intents, report, run
from codex_orchestrator.system import atomic_json, changed, command, exclusive, git, snapshot, store_root
from helpers import ProjectCase


class RuntimeTests(ProjectCase):
    def roles(self):
        return [(c['role'],c['mode']) for c in self.calls()]

    def test_luna_end_to_end_direct_route_and_global_gate(self):
        state=self.execute()
        self.assertEqual(self.roles(), [('astra','contract'),('luna','implement'),('terra','review'),('astra','global')])
        self.assertTrue(gate(self.root,state['id'])['approved'])
        self.assertEqual((self.root/'value.txt').read_text(),'initial')
        self.assertEqual((Path(state['worktree'])/'value.txt').read_text(),'ok')
        self.assertEqual(changed(self.root),[])
        self.assertEqual(git(self.root,'branch','--show-current'),'main')
        for call in self.calls():
            self.assertIn('agents.enabled=false',call['argv'])
            self.assertEqual(call['argv'][call['argv'].index('--sandbox')+1],
                             'workspace-write' if call['mode']=='implement' else 'read-only')

    def test_terra_route(self):
        self.plan['tasks'][0]['tier']='terra';self.flush();self.execute()
        self.assertEqual(self.roles(),[('astra','contract'),('terra','implement'),('sol','review'),('astra','global')])

    def test_sol_route_does_not_self_approve(self):
        self.plan['tasks'][0]['tier']='sol';self.flush();self.execute()
        self.assertEqual(self.roles(),[('astra','contract'),('sol','implement'),('astra','review'),('astra','global')])

    def test_contract_rejected_before_any_implementation(self):
        self.blocked('contract-reject','contract was not approved')
        self.assertEqual([c['mode'] for c in self.calls()],['contract'])

    def test_unexpected_baseline_stops_before_inference(self):
        self.plan['tasks'][0]['test_contract']['cases'][0]['baseline']='pass';self.flush()
        self.blocked('ok','Unexpected baseline');self.assertEqual(self.calls(),[])

    def test_failing_test_returns_to_executor_before_paid_review(self):
        self.scenario('test-fix');state=self.execute()
        self.assertEqual([c['role'] for c in self.calls()],['astra','luna','luna','terra','astra'])
        self.assertEqual(state['attempt_totals']['T1']['luna'],2)

    def test_review_rejection_and_compact_factual_handoff(self):
        self.scenario('reject-once');self.execute()
        implementations=[c for c in self.calls() if c['mode']=='implement']
        self.assertEqual(len(implementations),2)
        second=implementations[1]['request']['handoff']
        self.assertTrue(second['last_attempts']);self.assertIn('counterexample',second['unresolved'])
        for review in [c for c in self.calls() if c['mode']=='review']:
            self.assertNotIn('handoff',review['request'])
            self.assertNotIn('history',review['request']['task'])

    def test_escalation_preserves_failure_and_changes_reviewer(self):
        self.scenario('escalate');s=self.execute()
        self.assertEqual([c['role'] for c in self.calls()],['astra','luna','terra','sol','astra'])
        transfer=[c for c in self.calls() if c['mode']=='implement' and c['role']=='terra'][0]
        self.assertIn('counterexample',transfer['request']['handoff']['unresolved'])
        self.assertEqual(s['tasks']['T1']['tier'],'terra')

    def test_attempt_limits_stop_infinite_rework(self):
        self.cfg['attempts']={'luna':1,'terra':1,'sol':1};self.flush()
        self.blocked('endless','Sol attempt limit')
        self.assertEqual([c['role'] for c in self.calls() if c['mode']=='implement'],['luna','terra','sol'])

    def test_nested_delegation_has_separate_worktrees(self):
        self.plan['tasks'][0]['tier']='sol';self.flush();self.scenario('delegate');s=self.execute()
        self.assertEqual(set(s['tasks']),{'T1','T1.child','T1.child.child'})
        self.assertEqual(len({t['worktree'] for t in s['tasks'].values()}),3)
        self.assertTrue(all(t['status']=='integrated' for t in s['tasks'].values()))
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_delegation_cannot_replace_oracle_or_expand_scope(self):
        self.plan['tasks'][0]['tier']='terra';self.flush()
        self.blocked('delegate-oracle','reviewed acceptance oracle')

    def test_delegation_cannot_expand_scope(self):
        self.plan['tasks'][0]['tier']='terra';self.flush()
        self.blocked('delegate-escape','scope exceeds parent')

    def test_repeated_delegation_is_rejected(self):
        self.plan['tasks'][0]['tier']='terra';self.flush()
        self.blocked('delegate-twice','Invalid/repeated delegation')

    def test_scope_violation_stops_before_local_review(self):
        self.blocked('scope','Out-of-scope')
        self.assertEqual([c['mode'] for c in self.calls()],['contract','implement'])

    def test_executor_cannot_weaken_immutable_tests(self):
        self.blocked('tamper-tests','Protected acceptance')
        self.assertEqual((self.root/'oracle.py').read_text().splitlines()[0],'from pathlib import Path')

    def test_executor_cannot_change_git_head(self):
        self.blocked('head-change','HEAD')

    def test_review_read_only_is_checked_even_with_untrusted_backend(self):
        self.blocked('tamper-review','Reviewer changed')

    def test_stale_review_snapshot_rejected(self):
        self.blocked('stale-review','different source snapshot')

    def test_approval_with_failed_acceptance_rejected(self):
        self.blocked('false-approve','Approval conflicts')

    def test_invalid_model_json_contract_rejected(self):
        self.blocked('invalid-output','fields must be')

    def test_blocked_reviewer_stops(self):
        self.blocked('blocked-review','Review blocked')

    def test_mutating_real_check_is_rejected(self):
        os.environ['CO_TEST_MUTATING_CHECK']='1'
        self.blocked('ok','changed source')

    def test_global_failed_checks_cannot_be_approved(self):
        os.environ['CO_TEST_GLOBAL_STRICT']='1'
        self.blocked('ok','Global approval rejected')

    def test_global_findings_repair_with_shared_attempt_accounting(self):
        os.environ['CO_TEST_GLOBAL_STRICT']='1';self.scenario('global-fix');s=self.execute()
        self.assertEqual(s['global_round'],2)
        repairs=[t for t in s['tasks'].values() if t.get('repair')]
        self.assertEqual(len(repairs),1);self.assertEqual(repairs[0]['attempt_key'],'T1')
        self.assertEqual(s['attempt_totals']['T1']['luna'],2)
        self.assertIn('T1',repairs[0]['ancestors'])
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_out_of_scope_global_findings_stop_for_replanning(self):
        self.blocked('global-scope','explicit replanning')

    def test_checkpoint_repair_finishes_before_dependent_starts(self):
        self.second_task(depends=True);self.cfg['runtime']['checkpoint_every']=1;self.flush()
        os.environ['CO_TEST_SMOKE_STRICT']='1';self.scenario('checkpoint-fix');s=self.execute()
        modes=[(c['task_id'],c['mode']) for c in self.calls()]
        repair_index=next(i for i,(t,m) in enumerate(modes) if t.startswith('repair-') and m=='review')
        second_index=modes.index(('T2','implement'));self.assertLess(repair_index,second_index)
        self.assertGreaterEqual(len(s['checkpoints']),2)
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_checkpoint_failure_does_not_release_dependents(self):
        self.second_task(depends=True);self.cfg['runtime']['checkpoint_every']=1;self.flush()
        os.environ['CO_TEST_SMOKE_STRICT']='1';self.blocked('ok','Failing checkpoint')
        self.assertFalse(any(c['task_id']=='T2' for c in self.calls()))

    def test_parallel_tasks_isolated_and_integrated_in_plan_order(self):
        self.second_task();self.cfg['runtime']['parallel_workers']=2;self.flush();self.scenario('parallel')
        s=self.execute();impl=[c for c in self.calls() if c['mode']=='implement']
        self.assertEqual(len({x['worktree'] for x in impl}),2)
        self.assertLess(abs(impl[0]['started']-impl[1]['started']),2.0)
        integrated=[e['task'] for e in s['events'] if e['event']=='integrated']
        self.assertEqual(integrated,['T1','T2'])
        self.assertTrue(any(c['mode']=='integration_review' and c['task_id']=='T2' for c in self.calls()))
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_parallel_worker_failure_cancels_batch_without_integration(self):
        self.second_task();self.cfg['runtime']['parallel_workers']=2;self.flush()
        s=self.blocked('parallel-failure')
        self.assertFalse(any(e['event']=='integrated' for e in s['events']))
        self.assertEqual((self.root/'value.txt').read_text(),'initial')
        for task in s['tasks'].values():
            if task.get('worktree'):self.assertFalse((Path(task['worktree'])/'late.txt').exists())

    def test_checkpoint_repair_budget_survives_resume(self):
        self.second_task(depends=True);self.cfg['runtime']['checkpoint_every']=1;self.flush()
        os.environ['CO_TEST_SMOKE_STRICT']='1';s=self.blocked('checkpoint-never-fix','Checkpoint repair budget')
        count=len(self.calls())
        with self.assertRaisesRegex(OrchestratorError,'Checkpoint repair budget'):run(self.root,run_id=s['id'])
        self.assertEqual(len(self.calls()),count)
        self.assertFalse(any(c['task_id']=='T2' for c in self.calls()))

    def test_serial_dependencies_have_no_speculative_start(self):
        self.second_task(depends=True);self.cfg['runtime']['parallel_workers']=2;self.flush();s=self.execute()
        calls=self.calls();i1=next(i for i,c in enumerate(calls) if c['task_id']=='T1' and c['mode']=='review')
        i2=next(i for i,c in enumerate(calls) if c['task_id']=='T2' and c['mode']=='implement')
        self.assertLess(i1,i2);self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_call_budget_includes_contract_and_reviews(self):
        self.cfg['runtime']['max_calls']=2;self.flush()
        self.blocked('ok','Model call budget');self.assertEqual(len(self.calls()),2)

    def test_task_call_budget_includes_reviewer(self):
        self.cfg['budgets']={'task_calls':1};self.flush()
        self.blocked('ok','Task/subtree call');self.assertEqual(len(self.calls()),2)

    def test_missing_usage_blocks_resume_without_reset(self):
        s=self.blocked('missing-usage','telemetry');count=len(self.calls());self.scenario('ok')
        with self.assertRaisesRegex(OrchestratorError,'telemetry'):run(self.root,run_id=s['id'])
        self.assertEqual(len(self.calls()),count)
        self.assertEqual(s['calls'][-1]['status'],'unknown')

    def test_observed_ceiling_cancels_inflight_worker(self):
        s=self.blocked('overspend','token')
        self.assertFalse((Path(s['tasks']['T1']['worktree'])/'not-allowed.txt').exists())
        self.assertGreater(s['calls'][-1]['usage']['input_tokens'],s['calls'][-1]['reserved_tokens'])

    def test_request_telemetry_can_cancel_before_turn_completes(self):
        s=self.blocked('request-overspend','telemetry')
        self.assertFalse((Path(s['tasks']['T1']['worktree'])/'not-allowed.txt').exists())

    def test_timeout_blocks_without_blind_retries(self):
        self.cfg['runtime']['model_timeout']=1;self.flush();s=self.blocked('timeout','telemetry')
        self.assertEqual(len(self.calls()),2);self.assertEqual(s['calls'][-1]['status'],'unknown')

    def test_strict_budget_refuses_any_model_call(self):
        self.cfg['budgets']={'mode':'strict'};self.flush();self.blocked('ok','Strict billing cap')
        self.assertEqual(self.calls(),[])

    def test_hook_and_turn_usage_not_double_counted_end_to_end(self):
        self.scenario('hook-usage');s=self.execute()
        usage=report(s)['usage_by_role_and_stage']['luna/implement']['tokens']
        self.assertEqual(usage['input_tokens'],100);self.assertEqual(usage['output_tokens'],10)
        self.assertEqual(usage['reasoning_output_tokens'],4)

    def test_original_dirty_checkout_is_refused(self):
        (self.root/'value.txt').write_text('user edit')
        with self.assertRaisesRegex(OrchestratorError,'Commit your'):self.execute()
        self.assertEqual(self.calls(),[])

    def test_repository_lock_prevents_concurrent_controllers(self):
        with exclusive(store_root(self.root)/'controller.lock'):
            with self.assertRaisesRegex(OrchestratorError,'Another controller'):self.execute()

    def test_initial_task_limit_checked_before_inference(self):
        self.second_task();self.cfg['runtime']['max_tasks']=1;self.flush()
        with self.assertRaisesRegex(OrchestratorError,'task budget'):self.execute()
        self.assertEqual(self.calls(),[])

    def test_worktrees_cannot_live_in_git_metadata(self):
        self.cfg['runtime']['worktree_root']=str(self.root/'.git/work');self.flush()
        with self.assertRaisesRegex(OrchestratorError,'outside project'):self.execute()

    def test_gate_rejects_edits_after_approval_and_resume_is_idempotent(self):
        s=self.execute();count=len(self.calls());run(self.root,run_id=s['id'])
        self.assertEqual(len(self.calls()),count)
        (Path(s['worktree'])/'value.txt').write_text('unreviewed')
        with self.assertRaisesRegex(OrchestratorError,'stale'):gate(self.root,s['id'])
        with self.assertRaisesRegex(OrchestratorError,'stale'):run(self.root,run_id=s['id'])

    def test_resume_does_not_repeat_implementation_after_invalid_review(self):
        s=self.blocked('invalid-output');self.scenario('ok');done=run(self.root,run_id=s['id'])
        self.assertEqual(done['status'],'approved')
        self.assertEqual(sum(c['mode']=='implement' for c in self.calls()),1)

    def test_resume_rejects_external_task_worktree_changes(self):
        s=self.blocked('invalid-output');(Path(s['tasks']['T1']['worktree'])/'value.txt').write_text('external')
        with self.assertRaisesRegex(OrchestratorError,'outside controller'):run(self.root,run_id=s['id'])


class CriticalTests(ProjectCase):
    def sign(self,state,identity='reviewer@example.invalid'):
        statement=Path(self.temp.name)/'approval.json';signature=Path(self.temp.name)/'approval.sig'
        statement.write_bytes(canonical(approval_payload(state)))
        command(['openssl','pkeyutl','-sign','-rawin','-inkey',str(self.key),'-in',str(statement),'-out',str(signature)],self.root)
        return statement,signature,identity

    def test_risk_floor_adversarial_review_and_real_signed_approval(self):
        self.critical();s=self.execute()
        self.assertEqual(s['status'],'awaiting_human')
        self.assertEqual(s['tasks']['T1']['tier'],'sol')
        self.assertTrue(any(c['mode']=='adversarial' for c in self.calls()))
        with self.assertRaisesRegex(OrchestratorError,'not approved'):gate(self.root,s['id'])
        self.assertTrue(approve(self.root,s['id'],*self.sign(s))['approved'])
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_dynamic_risk_detection_reexecutes_at_required_tier(self):
        self.signer();self.plan['tasks'][0]['adversarial_checks']=['negative']
        self.plan['tasks'][0]['test_contract']['mutations']=[{"file":"value.txt","find":"ok","replace":"bad","check":"unit","failure_marker":"AssertionError"}]
        self.flush();self.scenario('risk-after')
        s=self.execute();roles=[c['role'] for c in self.calls() if c['mode']=='implement']
        self.assertEqual(roles,['luna','sol']);self.assertEqual(s['status'],'awaiting_human')

    def test_critical_parent_cannot_delegate_away_risk(self):
        self.critical();self.blocked('risk-launder','Risk floor forbids')
        self.assertFalse(any(c['role'] in {'luna','terra'} for c in self.calls()))

    def test_adversarial_rejection_returns_to_executor(self):
        self.critical();self.scenario('adversarial-reject');s=self.execute()
        self.assertEqual(s['status'],'awaiting_human')
        self.assertEqual(sum(c['mode']=='implement' for c in self.calls()),2)
        self.assertEqual(sum(c['mode']=='adversarial' for c in self.calls()),2)

    def test_wrong_signer_signature_or_altered_statement_rejected(self):
        self.critical();s=self.execute();statement,signature,identity=self.sign(s)
        with self.assertRaisesRegex(OrchestratorError,'authorized public key'):approve(self.root,s['id'],statement,signature,'unknown')
        good=signature.read_bytes();signature.write_bytes(bytes([good[0]^1])+good[1:])
        with self.assertRaises(OrchestratorError):approve(self.root,s['id'],statement,signature,identity)
        signature.write_bytes(good);statement.write_bytes(statement.read_bytes()+b' ')
        with self.assertRaisesRegex(OrchestratorError,'stale'):approve(self.root,s['id'],statement,signature,identity)
        self.assertEqual(self.states()[-1]['status'],'awaiting_human')

    def test_signer_allowlist_and_evidence_are_pinned(self):
        self.critical();s=self.execute();statement,signature,identity=self.sign(s)
        original=self.signers.read_bytes();self.signers.write_bytes(original+b'\n')
        with self.assertRaisesRegex(OrchestratorError,'configuration changed'):approve(self.root,s['id'],statement,signature,identity)
        self.signers.write_bytes(original)
        (Path(s['worktree'])/'value.txt').write_text('changed after signing')
        with self.assertRaisesRegex(OrchestratorError,'stale'):approve(self.root,s['id'],statement,signature,identity)

    def test_managed_agent_cannot_execute_human_gate(self):
        self.critical();s=self.execute();args=self.sign(s)
        with patch.dict(os.environ,{'CO_MANAGED':'1'}),self.assertRaisesRegex(OrchestratorError,'cannot grant'):approve(self.root,s['id'],*args)

    def test_all_policy_also_requires_signature_for_low_risk(self):
        self.signer();self.cfg['verification']['require_human']='all';self.flush();s=self.execute()
        self.assertEqual(s['status'],'awaiting_human');self.assertEqual(s['tasks']['T1']['tier'],'luna')


class SchedulerTests(unittest.TestCase):
    def task(self,ident,scope,reads=(),deps=(),critical=False):
        return {'id':ident,'scope':scope,'reads':list(reads),'depends_on':list(deps),'status':'pending','risk':{'critical':critical}}

    def test_conflicting_writes_and_read_write_dependencies_serialize(self):
        a=self.task('A',['src/']);b=self.task('B',['src/a']);c=self.task('C',['other'],['src/'])
        self.assertEqual([t['id'] for t in ready_batch([a,b,c],set(),3,3)],['A'])

    def test_critical_tasks_are_never_in_parallel_batch(self):
        a=self.task('A',['a']);b=self.task('B',['b'],critical=True)
        self.assertEqual([t['id'] for t in ready_batch([a,b],set(),4,4)],['A'])
        self.assertEqual([t['id'] for t in ready_batch([b,a],set(),4,4)],['B'])

    def test_batch_respects_checkpoint_boundary_and_dependencies(self):
        tasks=[self.task('A',['a']),self.task('B',['b']),self.task('C',['c'],deps=['B'])]
        self.assertEqual(len(ready_batch(tasks,set(),4,1)),1)
        self.assertEqual(len(ready_batch(tasks,set(),4,4)),2)

    def test_handoff_size_bound_without_private_transcripts(self):
        task={'id':'T','attempts':{'luna':2},'feedback':'problem '*3000,'history':[{'role':'luna','attempt':i,'outcome':'failed','detail':'x'*2000,'private_reasoning':'not forwarded'} for i in range(30)]}
        summary=handoff(task,800)
        self.assertLessEqual(len(canonical(summary)),800)
        self.assertNotIn('private_reasoning',json.dumps(summary))
        self.assertEqual(summary['attempts']['luna'],2)
