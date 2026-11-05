from __future__ import annotations
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tomllib
import unittest
from unittest.mock import patch
from codex_orchestrator.cli import main
from codex_orchestrator.contracts import OrchestratorError
from codex_orchestrator.evidence import capsule_fresh, read_json, verify_artifact
from codex_orchestrator.hooks import handle
from codex_orchestrator.install import default_config, install, migrate_config
from codex_orchestrator.runtime import Engine, gate, recover_intents, run, session_file
from codex_orchestrator.system import atomic_json, git, load_config, snapshot, store_root
from helpers import ProjectCase


class EvidenceTests(ProjectCase):
    def test_tampered_actual_review_or_log_invalidates_gate(self):
        s=self.execute();t=s['tasks']['T1'];capsule=read_json(Path(t['proof']['path']))
        paths=[Path(capsule['response']['path']),Path(capsule['checks'][0]['stdout']['path']),Path(t['proof']['path'])]
        for path in paths:
            original=path.read_bytes();path.write_bytes(original+b'alteration')
            with self.subTest(path=path),self.assertRaisesRegex(OrchestratorError,'artifact changed'):gate(self.root,s['id'])
            path.write_bytes(original)
        self.assertTrue(gate(self.root,s['id'])['approved'])

    def test_global_check_snapshot_cannot_be_replaced(self):
        s=self.execute();file=store_root(self.root)/'runs'/s['id']/'state.json'
        s['global_checks'][0]['snapshot']='different';atomic_json(file,s)
        with self.assertRaisesRegex(OrchestratorError,'Global checks'):gate(self.root,s['id'])

    def test_contract_review_evidence_remains_required_at_final_gate(self):
        s=self.execute();path=Path(s['contract_proof']['path']);path.write_text('{}')
        with self.assertRaisesRegex(OrchestratorError,'artifact changed'):gate(self.root,s['id'])

    def test_review_capsule_freshness_depends_on_source_and_reads(self):
        s=self.execute();t=s['tasks']['T1'];work=Path(s['worktree'])
        self.assertTrue(capsule_fresh(t['proof'],t,work,s['sealed']))
        (work/'value.txt').write_text('ok but different')
        self.assertFalse(capsule_fresh(t['proof'],t,work,s['sealed']))

    def test_identical_review_reuses_result_without_new_model_call(self):
        s=self.execute();directory=store_root(self.root)/'runs'/s['id'];engine=Engine(self.root,s,directory)
        t=s['tasks']['T1'];work=Path(s['worktree']);_,checks=engine.check(work,['unit'],'reuse-test')
        first,ref1=engine.review('refresh','terra',t,work,checks,diff_base=t['base'])
        count=len(self.calls())
        second,ref2=engine.review('refresh','terra',t,work,checks,diff_base=t['base'])
        self.assertEqual(first,second);self.assertEqual(ref1,ref2);self.assertEqual(len(self.calls()),count)
        self.assertEqual(s['events'][-1]['event'],'review_reused')

    def test_committed_patch_changed_by_git_hook_never_approved(self):
        hook=self.root/'.git/hooks/pre-commit';hook.write_text('#!/bin/sh\nprintf changed > value.txt\ngit add value.txt\n')
        hook.chmod(0o755)
        self.blocked('ok','Commit hooks changed')

    def test_recover_completed_integration_intent_without_replay(self):
        s=self.execute();t=s['tasks']['T1'];head=git(Path(s['worktree']),'rev-parse','HEAD')
        t['status']='local_approved';t['integration_intent']={'target':s['worktree'],'before':s['base'],'candidate':head}
        recover_intents(s)
        self.assertEqual(t['status'],'integrated');self.assertNotIn('integration_intent',t)
        self.assertTrue(s['checkpoint_due'])

    def test_ambiguous_interrupted_integration_preserves_worktree(self):
        s=self.execute();t=s['tasks']['T1'];t['integration_intent']={'target':s['worktree'],'before':'bad','candidate':'other'}
        with self.assertRaisesRegex(OrchestratorError,'Ambiguous'):recover_intents(s)
        self.assertTrue(Path(s['worktree']).is_dir())

    def test_scope_conflict_during_integration_never_overwrites_target(self):
        s=self.execute();directory=store_root(self.root)/'runs'/s['id'];engine=Engine(self.root,s,directory)
        t=s['tasks']['T1'];t['status']='local_approved'
        target=Path(self.temp.name)/'conflicting-target'
        git(self.root,'worktree','add','--detach',str(target),s['base'])
        (target/'value.txt').write_text('conflicting edit')
        git(target,'add','value.txt');git(target,'commit','-m','external integration')
        with self.assertRaisesRegex(OrchestratorError,'Integration conflict'):engine.integrate('T1',target)
        self.assertEqual((target/'value.txt').read_text(),'conflicting edit')
        self.assertTrue(Path(t['worktree']).is_dir())


class InstallHookTests(ProjectCase):
    def test_installer_preserves_user_config_instructions_hooks_and_is_idempotent(self):
        (self.root/'.codex').mkdir()
        (self.root/'AGENTS.md').write_text('Existing domain rules.\n')
        (self.root/'.codex/config.toml').write_text('model = "my-model"\n')
        custom={'type':'command','command':'echo user','statusMessage':'user-owned'}
        (self.root/'.codex/hooks.json').write_text(json.dumps({'hooks':{'Stop':[{'hooks':[custom]}]}}))
        original=(self.root/'orchestrator.toml').read_text();changed=install(self.root)
        self.assertTrue(changed);self.assertIn('Existing domain rules.',(self.root/'AGENTS.md').read_text())
        self.assertEqual((self.root/'orchestrator.toml').read_text(),original)
        parsed=tomllib.loads((self.root/'.codex/config.toml').read_text());self.assertEqual(parsed['model'],'my-model')
        hooks=json.loads((self.root/'.codex/hooks.json').read_text())['hooks']
        self.assertIn(custom,[h for g in hooks['Stop'] for h in g['hooks']])
        self.assertIn('PostModelRequest',hooks);self.assertEqual(install(self.root),[])

    def test_manually_edited_skill_not_overwritten(self):
        install(self.root);p=self.root/'.agents/skills/codex-orchestrator/SKILL.md';p.write_text('custom')
        with self.assertRaisesRegex(OrchestratorError,'Managed file was edited'):install(self.root)
        self.assertEqual(p.read_text(),'custom')

    def test_install_symlink_rejected_before_write(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir();(self.root/'.agents').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(OrchestratorError,'symlinked'):install(self.root)
        self.assertEqual(list(outside.iterdir()),[])

    def test_bad_existing_hooks_abort_install(self):
        (self.root/'.codex').mkdir();(self.root/'.codex/hooks.json').write_text('{"hooks":{"Stop":"bad"}}')
        with self.assertRaisesRegex(OrchestratorError,'Invalid existing hook'):install(self.root)
        self.assertFalse((self.root/'.agents/skills/codex-orchestrator/SKILL.md').exists())

    def test_v1_config_explicit_migration_preserves_backup_and_commands(self):
        original='version = 1\n[runtime]\nattempts_per_tier = 3\n[checks]\nunit = ["python", "tests.py"]\n'
        (self.root/'orchestrator.toml').write_text(original);install(self.root)
        self.assertEqual((self.root/'orchestrator.toml').read_text(),original)
        backup=Path(migrate_config(self.root));self.assertEqual(backup.read_text(),original)
        cfg=load_config(self.root);self.assertEqual(cfg['attempts'],{'luna':3,'terra':3,'sol':3})
        self.assertEqual(cfg['checks']['unit'],['python','tests.py'])
        self.assertEqual(migrate_config(self.root),'already-v2')

    def test_migration_refuses_overwriting_different_backup(self):
        original='version = 1\n[checks]\nunit = ["python", "tests.py"]\n'
        (self.root/'orchestrator.toml').write_text(original);(self.root/'orchestrator.toml.v1.bak').write_text('other')
        with self.assertRaisesRegex(OrchestratorError,'different contents'):migrate_config(self.root)
        self.assertEqual((self.root/'orchestrator.toml').read_text(),original)

    def test_default_config_contains_real_controls_but_no_fake_checks(self):
        cfg=tomllib.loads(default_config());self.assertEqual(cfg['version'],2)
        self.assertEqual(cfg['checks'],{});self.assertEqual(cfg['verification']['require_human'],'critical')
        self.assertEqual(cfg['budgets']['mode'],'observed')

    def test_unrelated_sessions_are_not_gated(self):
        payload={'hook_event_name':'Stop','session_id':'unrelated','cwd':str(self.root)}
        self.assertEqual(handle(payload),{})
        payload['hook_event_name']='SessionStart';self.assertIn('hookSpecificOutput',handle(payload))

    def test_managed_hook_only_records_usage_not_transcripts(self):
        directory=Path(self.temp.name)/'call';directory.mkdir();p=directory/'request-usage.jsonl'
        with patch.dict(os.environ,{'CO_MANAGED':'1','CO_USAGE_FILE':str(p)}):
            self.assertEqual(handle({'hook_event_name':'Stop'}),{})
            self.assertEqual(handle({'hook_event_name':'PostModelRequest','status':'completed','token_usage':{'input_tokens':1,'output_tokens':2},'private_transcript':'do not copy'}),{})
        text=p.read_text();self.assertNotIn('private_transcript',text);self.assertIn('input_tokens',text)

    def test_managed_telemetry_symlink_cannot_redirect_write(self):
        target=Path(self.temp.name)/'secret';target.write_text('untouched')
        p=Path(self.temp.name)/'request-usage.jsonl';p.symlink_to(target)
        with patch.dict(os.environ,{'CO_MANAGED':'1','CO_USAGE_FILE':str(p)}),self.assertRaises(OrchestratorError):handle({'hook_event_name':'PostModelRequest'})
        self.assertEqual(target.read_text(),'untouched')

    def test_stop_reports_blocked_and_does_not_retry_forever(self):
        s=self.blocked('invalid-output');store=store_root(self.root)
        atomic_json(session_file(store,'session'),{'run_id':s['id']})
        payload={'hook_event_name':'Stop','session_id':'session','cwd':str(self.root)}
        self.assertIn('NOT approved',handle(payload)['systemMessage'])
        s['status']='running';atomic_json(store/'runs'/s['id']/'state.json',s)
        self.assertEqual(handle(payload)['decision'],'block')
        payload['stop_hook_active']=True;self.assertFalse(handle(payload)['continue'])

    def test_stop_approved_session_checks_real_gate(self):
        s=run(self.root,self.plan_file,session_id='session')
        payload={'hook_event_name':'Stop','session_id':'session','cwd':str(self.root)}
        self.assertEqual(handle(payload),{})
        (Path(s['worktree'])/'value.txt').write_text('after approval')
        self.assertFalse(handle(payload)['continue'])

    def test_doctor_validate_do_not_run_inference(self):
        for command,args in [('doctor',[]),('validate',[str(self.plan_file)])]:
            with self.subTest(command=command),contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--repo',str(self.root),command,*args]),0)
                self.assertTrue(json.loads(out.getvalue()))
        self.assertEqual(self.calls(),[])

    def test_invalid_hook_input_returns_explicit_failure_json(self):
        with patch('sys.stdin',io.StringIO('not-json')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['hooks']),0)
        self.assertFalse(json.loads(out.getvalue())['continue'])

    def test_oversized_hook_input_rejected(self):
        with patch('sys.stdin',io.StringIO('x'*(1024*1024+1))),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['hooks']),0)
        self.assertFalse(json.loads(out.getvalue())['continue'])
