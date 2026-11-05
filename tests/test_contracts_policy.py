from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import unittest
from codex_orchestrator.config import DEFAULTS, load_config, money
from codex_orchestrator.contracts import (OrchestratorError, PLAN_SCHEMA, REVIEW_SCHEMA, overlaps,
    safe_id, safe_path, validate, validate_plan, validate_review, transport_schema)
from codex_orchestrator.policy import assess, guard, require_verification, route, seal
from codex_orchestrator.system import git
from helpers import ProjectCase, plan, toml


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.checks = {'unit': ['true'], 'global': ['true'], 'smoke': ['true']}

    def test_valid_v2_plan(self):
        validate_plan(self.plan, self.checks)

    def test_v1_unknown_missing_fields_rejected(self):
        variants = []
        for key in ('reads', 'test_contract', 'risk_tags'):
            p = copy.deepcopy(self.plan); del p['tasks'][0][key]; variants.append(p)
        p = copy.deepcopy(self.plan); p['version'] = 1; variants.append(p)
        p = copy.deepcopy(self.plan); p['tasks'][0]['allow_unsafe'] = True; variants.append(p)
        for p in variants:
            with self.subTest(plan=p), self.assertRaises(OrchestratorError):
                validate_plan(p, self.checks)

    def test_boolean_not_integer_and_blank_strings(self):
        self.plan['version'] = True
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)
        self.plan['version'] = 2; self.plan['objective'] = ' '
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)

    def test_each_criterion_needs_independent_case(self):
        self.plan['tasks'][0]['acceptance'].append('A distinct missing behavior')
        with self.assertRaisesRegex(OrchestratorError, 'Every acceptance'): validate_plan(self.plan, self.checks)

    def test_duplicate_and_invented_criteria_rejected(self):
        t = self.plan['tasks'][0]
        t['acceptance'] *= 2
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)
        t['acceptance'] = ['Different behavior']
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)

    def test_conflicting_test_baselines_rejected(self):
        t = self.plan['tasks'][0]
        c = copy.deepcopy(t['test_contract']['cases'][0]); c['baseline'] = 'pass'
        t['test_contract']['cases'].append(c)
        with self.assertRaisesRegex(OrchestratorError, 'Conflicting baseline'): validate_plan(self.plan, self.checks)

    def test_oracle_cannot_be_in_any_write_scope(self):
        t = self.plan['tasks'][0]
        t['scope'].append('oracle.py')
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)
        t['scope'] = ['value.txt']
        other = copy.deepcopy(t); other.update(id='T2', scope=['oracle.py']); other['test_contract']['files'] = ['other.py']
        self.plan['tasks'].append(other)
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)

    def test_dependencies_unknown_duplicate_and_cycles(self):
        t = self.plan['tasks'][0]
        for deps in [['missing'], ['T1']]:
            t['depends_on'] = deps
            with self.subTest(deps=deps), self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)
        t['depends_on'] = []; self.plan['tasks'].append(copy.deepcopy(t))
        with self.assertRaises(OrchestratorError): validate_plan(self.plan, self.checks)

    def test_unknown_checks_rejected(self):
        for field in ('integration_checks', 'checkpoint_checks'):
            p = copy.deepcopy(self.plan); p[field] = ['invented']
            with self.subTest(field=field), self.assertRaises(OrchestratorError): validate_plan(p, self.checks)

    def test_paths_and_reserved_ids(self):
        for path in ['../a', '/tmp/a', 'C:a', 'src/../a', 'a//b', 'a\\b', '*', '.git/config', 'src/AGENTS.md', '.codex/x']:
            with self.subTest(path=path), self.assertRaises(OrchestratorError): safe_path(path)
        for ident in ['global', 'contract', 'repair-x']:
            p = copy.deepcopy(self.plan); p['tasks'][0]['id'] = ident
            with self.subTest(ident=ident), self.assertRaises(OrchestratorError): validate_plan(p, self.checks)
        for ident in ['a/b', '', 'a'*81]:
            with self.subTest(ident=ident), self.assertRaises(OrchestratorError): safe_id(ident)

    def test_overlap_matches_directory_and_not_prefix_accident(self):
        self.assertTrue(overlaps(['src/'], ['src/a.py']))
        self.assertTrue(overlaps(['a.py'], ['a.py']))
        self.assertFalse(overlaps(['src/'], ['src2/a.py']))
        self.assertFalse(overlaps(['a.py'], ['ab.py']))

    def test_transport_omits_unsupported_length_hints_but_local_validation_keeps_them(self):
        wire=transport_schema(REVIEW_SCHEMA)
        self.assertNotIn('minLength',json.dumps(wire))
        self.assertFalse(wire['additionalProperties'])
        self.assertIn('required',wire)
        with self.assertRaises(OrchestratorError):validate('', {'type':'string','minLength':1})

    def test_review_has_exact_acceptance_and_snapshot(self):
        r = {'decision': 'approve', 'snapshot': 'abc', 'summary': 'Evidence', 'findings': [],
             'acceptance': [{'criterion': 'C', 'passed': True, 'evidence': 'Actual check'}]}
        validate_review(r, ['C'], 'abc')
        for field, value in [('snapshot','bad'), ('acceptance',[]), ('findings',[{'task_id':'T1','file':'a','detail':'bug'}])]:
            bad = copy.deepcopy(r); bad[field] = value
            with self.subTest(field=field), self.assertRaises(OrchestratorError): validate_review(bad, ['C'], 'abc')
        r['acceptance'][0]['passed'] = False
        with self.assertRaises(OrchestratorError): validate_review(r, ['C'], 'abc')


class PolicyTests(ProjectCase):
    def test_path_hints_use_terra_without_automatic_critical_gate(self):
        cfg = load_config(self.root)
        samples = {'auth':'src/auth/service.py', 'payments':'src/payments/charge.py', 'migrations':'db/migrations/001.sql',
                   'concurrency':'src/mutex.py', 'secrets':'src/api_key.py', 'infrastructure':'Dockerfile'}
        for category, scope in samples.items():
            t = copy.deepcopy(self.plan['tasks'][0]); t['scope'] = [scope]
            risk = assess(t, cfg)
            with self.subTest(category=category):
                self.assertIn(category, risk['tags']); self.assertFalse(risk['critical'])
                route(t, risk); self.assertEqual(t['tier'], 'terra')

    def test_sensitive_code_change_escalates_but_docs_only_do_not(self):
        (self.root/'src/auth').mkdir(parents=True)
        path = self.root/'src/auth/service.py'
        path.write_text('value = 1\n')
        (self.root/'docs').mkdir()
        docs = self.root/'docs/auth.md'
        docs.write_text('Original documentation\n')
        self.flush()
        base = git(self.root, 'rev-parse', 'HEAD')
        t = copy.deepcopy(self.plan['tasks'][0])
        t['scope'] = ['src/auth/service.py']
        cfg = load_config(self.root)
        self.assertFalse(assess(t,cfg,self.root,base)['critical'])
        path.write_text('value = 2\n')
        risk = assess(t,cfg,self.root,base)
        self.assertTrue(risk['critical'])
        self.assertEqual(risk['minimum_tier'], 'sol')
        docs.write_text('Updated documentation\n')
        t['scope'] = ['docs/auth.md']
        doc_risk = assess(t,cfg,self.root,base)
        self.assertFalse(doc_risk['critical'])
        self.assertEqual(doc_risk['minimum_tier'], 'terra')

    def test_comment_only_sensitive_change_is_not_critical(self):
        (self.root/'src/auth').mkdir(parents=True)
        path = self.root/'src/auth/service.py'
        path.write_text('value = 1\n')
        self.flush()
        base = git(self.root, 'rev-parse', 'HEAD')
        path.write_text('value = 1\n# Updated auth comment\n')
        t = copy.deepcopy(self.plan['tasks'][0])
        t['scope'] = ['src/auth/service.py']
        self.assertFalse(assess(t,load_config(self.root),self.root,base)['critical'])

    def test_test_runner_manifest_protects_dependencies_and_helpers(self):
        (self.root/'package.json').write_text('{"scripts":{"test":"vitest"}}')
        (self.root/'package-lock.json').write_text('{"lockfileVersion":3}')
        (self.root/'tests').mkdir()
        (self.root/'tests/helper.py').write_text('MODE = "strict"\n')
        self.flush()
        manifest = seal(self.root,self.plan,load_config(self.root))
        for name in ('package.json','package-lock.json','tests/helper.py'):
            self.assertIn(name,manifest)
            path=self.root/name
            before=path.read_bytes()
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(OrchestratorError,'Protected'):
                guard(self.root,manifest)
            path.write_bytes(before)

    def test_explicit_runner_support_paths_are_sealed(self):
        (self.root/'checks').mkdir()
        (self.root/'checks/helper.data').write_text('strict')
        self.cfg['verification']={'protected_paths':['checks/']}
        self.flush()
        manifest=seal(self.root,self.plan,load_config(self.root))
        self.assertIn('checks/helper.data',manifest)
        (self.root/'checks/helper.data').write_text('weak')
        with self.assertRaisesRegex(OrchestratorError,'Protected'):
            guard(self.root,manifest)

    def test_generic_payload_and_author_not_payments_or_auth(self):
        t = copy.deepcopy(self.plan['tasks'][0]); t['objective'] = 'Format the payload and author display name'
        self.assertFalse(assess(t, load_config(self.root))['critical'])

    def test_actual_paths_inside_broad_scope_and_custom_globs(self):
        (self.root/'src/auth').mkdir(parents=True)
        (self.root/'src/auth/logic.py').write_text('value = 1')
        self.flush()
        t=copy.deepcopy(self.plan['tasks'][0]); t['scope']=['src/']
        cfg=load_config(self.root);cfg['policy']['extra_patterns']=['src/auth/*']
        risk=assess(t,cfg,self.root)
        self.assertIn('auth',risk['tags']); self.assertIn('project_policy',risk['tags'])

    def test_removed_sensitive_source_is_still_critical(self):
        (self.root/'value.txt').write_text('authentication boundary')
        self.flush(); base=git(self.root,'rev-parse','HEAD')
        (self.root/'value.txt').write_text('nothing sensitive here')
        risk=assess(self.plan['tasks'][0],load_config(self.root),self.root,base)
        self.assertIn('auth',risk['tags'])

    def test_declared_inherited_and_existing_risks_cannot_lower(self):
        t=copy.deepcopy(self.plan['tasks'][0]); t['risk_tags']=['domain-critical']
        route(t,assess(t,load_config(self.root)));self.assertEqual(t['tier'],'sol')
        t['risk_tags']=[];route(t,assess(t,load_config(self.root)))
        self.assertIn('domain-critical',t['risk']['tags'])
        u=copy.deepcopy(self.plan['tasks'][0]);route(u,assess(u,load_config(self.root),inherited=['auth']))
        self.assertEqual(u['tier'],'sol')

    def test_minimum_tier_and_binary_conservatism(self):
        cfg=load_config(self.root);cfg['policy']['minimum_tier']='terra'
        t=copy.deepcopy(self.plan['tasks'][0]);route(t,assess(t,cfg));self.assertEqual(t['tier'],'terra')
        (self.root/'value.txt').write_bytes(b'\0binary')
        self.assertTrue(assess(t,cfg,self.root)['critical'])

    def test_sealed_oracle_config_and_check_scripts_reject_edits(self):
        manifest=seal(self.root,self.plan,load_config(self.root))
        for name in ['oracle.py','smoke.py','suite.py','orchestrator.toml']:
            path=self.root/name;before=path.read_bytes();path.write_bytes(before+b'\n# changed')
            with self.subTest(file=name), self.assertRaisesRegex(OrchestratorError,'Protected'):guard(self.root,manifest)
            path.write_bytes(before)
        guard(self.root,manifest)

    def test_missing_or_symlink_oracle_is_not_sealed(self):
        t=self.plan['tasks'][0]; t['test_contract']['files']=['missing.py']
        with self.assertRaises(OrchestratorError):seal(self.root,self.plan,load_config(self.root))
        t['test_contract']['files']=['oracle.py']; p=self.root/'oracle.py';p.unlink();p.symlink_to(self.root/'suite.py')
        with self.assertRaises(OrchestratorError):seal(self.root,self.plan,load_config(self.root))

    def test_critical_requires_adversarial_checks_and_external_signer(self):
        t=copy.deepcopy(self.plan['tasks'][0]);cfg=load_config(self.root);route(t,assess(t,cfg,inherited=['auth']))
        with self.assertRaisesRegex(OrchestratorError,'adversarial'):require_verification(t,cfg)
        t['adversarial_checks']=['negative']
        with self.assertRaisesRegex(OrchestratorError,'trusted_signers'):require_verification(t,cfg)


class ConfigTests(ProjectCase):
    def test_v1_rejected_instead_of_silent_unsafe_upgrade(self):
        self.cfg['version']=1;self.flush()
        with self.assertRaisesRegex(OrchestratorError,'version = 2'):load_config(self.root)

    def test_shell_checks_unknown_fields_and_invalid_limits_rejected(self):
        cases=[('checks',{'unit':'echo pass'}),('runtime',{'unknown':1}),('runtime',{'parallel_workers':17}),
               ('attempts',{'luna':0}),('budgets',{'call_tokens':3000000}),('verification',{'require_human':'never'})]
        base=copy.deepcopy(self.cfg)
        for section,value in cases:
            self.cfg=copy.deepcopy(base);self.cfg[section]=value;self.flush()
            with self.subTest(section=section,value=value),self.assertRaises(OrchestratorError):load_config(self.root)

    def test_money_finite_and_no_float_rounding(self):
        for value in ['NaN','Infinity','-1',True,1.1]:
            with self.subTest(value=value),self.assertRaises(OrchestratorError):money(value)
        self.assertEqual(str(money('0.001')),'0.001')

    def test_financial_cap_requires_all_model_rates(self):
        self.cfg['budgets']={'run_cost_usd':'10'};self.flush()
        with self.assertRaisesRegex(OrchestratorError,'explicit rates'):load_config(self.root)

    def test_signer_allowlist_must_be_external_regular_file(self):
        (self.root/'keys.json').write_text('{}');self.cfg['verification']={'trusted_signers':str(self.root/'keys.json')};self.flush()
        with self.assertRaisesRegex(OrchestratorError,'outside'):load_config(self.root)
