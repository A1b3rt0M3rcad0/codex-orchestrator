"""Regression tests for pre-approved mutation probes and immutable evidence."""
from __future__ import annotations
import copy
from pathlib import Path
import unittest
from codex_orchestrator.contracts import OrchestratorError, validate_plan
from codex_orchestrator.runtime import gate
from helpers import ProjectCase

MUTANT = {"file": "value.txt", "find": "ok", "replace": "bad", "check": "unit",
          "failure_marker": "AssertionError"}

class MutationQualityTests(ProjectCase):
    def test_noncritical_mutation_catches_known_regression(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [copy.deepcopy(MUTANT)]
        self.flush()
        state = self.execute()
        self.assertEqual(state["status"], "approved")
        task = state["tasks"]["T1"]
        self.assertTrue(Path(task["mutation_proof"]["path"]).is_file())
        self.assertTrue(gate(self.root, state["id"])["approved"])

    def test_behavioral_file_replacement_rejects_bad_artifact(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [{
            "strategy": "file_replacement", "file": "value.txt",
            "content": "invalid result", "check": "unit",
            "failure_marker": "AssertionError",
        }]
        self.flush()
        state = self.execute()
        self.assertEqual(state["status"], "approved")
        self.assertTrue(gate(self.root, state["id"])["approved"])

    def test_behavioral_file_replacement_detects_surviving_mutant(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [{
            "strategy": "file_replacement", "file": "value.txt",
            "content": "okay", "check": "unit",
            "failure_marker": "AssertionError",
        }]
        self.flush()
        self.blocked("ok", "survived")

    def test_behavioral_mutation_schema_prevents_mixed_strategies(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [{
            "strategy": "file_replacement", "file": "value.txt",
            "find": "ok", "content": "bad", "check": "unit",
            "failure_marker": "AssertionError",
        }]
        with self.assertRaisesRegex(OrchestratorError, "forbids find/replace"):
            validate_plan(self.plan, self.cfg["checks"])

    def test_critical_requires_preapproved_mutant_before_model_call(self):
        self.signer()
        self.plan["tasks"][0].update(risk_tags=["auth"], adversarial_checks=["negative"])
        self.flush()
        self.blocked("ok", "requires pre-approved mutation tests")
        self.assertEqual(self.calls(), [])

    def test_surviving_mutant_does_not_become_approval(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [
            {**MUTANT, "replace": "okay"}]
        # Our oracle only checks startswith('ok'), so this mutant must survive.
        self.flush()
        self.blocked("ok", "survived")

    def test_unexpected_test_error_is_not_counted_as_mutant_kill(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [
            {**MUTANT, "failure_marker": "definitely-not-a-real-assertion"}]
        self.flush()
        self.blocked("ok", "unexpected reason")

    def test_mutant_cannot_edit_protected_test(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [
            {**MUTANT, "file": "oracle.py"}]
        self.flush()
        with self.assertRaisesRegex(OrchestratorError, "Mutation must target"):
            validate_plan(self.plan, self.cfg["checks"])

    def test_mutation_artifact_tampering_blocks_final_gate(self):
        self.plan["tasks"][0]["test_contract"]["mutations"] = [copy.deepcopy(MUTANT)]
        self.flush()
        state = self.execute()
        proof = Path(state["tasks"]["T1"]["mutation_proof"]["path"])
        proof.write_bytes(proof.read_bytes() + b" ")
        with self.assertRaises(OrchestratorError):
            gate(self.root, state["id"])

    def test_critical_mutant_includes_real_independent_proof(self):
        self.critical()
        state = self.execute()
        self.assertEqual(state["status"], "awaiting_human")
        self.assertIsNotNone(state["tasks"]["T1"]["mutation_proof"])
