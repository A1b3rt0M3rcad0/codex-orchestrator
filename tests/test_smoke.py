from __future__ import annotations
import unittest
from helpers import ProjectCase
from codex_orchestrator.contracts import OrchestratorError
from codex_orchestrator.smoke import smoke_models
from codex_orchestrator.system import load_config


class ModelSmokeTests(ProjectCase):
    def test_requires_explicit_paid_acknowledgement(self):
        with self.assertRaisesRegex(OrchestratorError, "acknowledge"):
            smoke_models(self.root, load_config(self.root), ["luna"], False)
        self.assertEqual(self.calls(), [])

    def test_check_all_roles_with_fake_backend_only(self):
        results = smoke_models(self.root, load_config(self.root), ["luna", "terra", "sol", "astra"], True)
        self.assertTrue(results["passed"])
        self.assertEqual([x["role"] for x in results["roles"]], ["luna", "terra", "sol", "astra"])
        self.assertEqual([x["mode"] for x in self.calls()], ["smoke"] * 4)
        self.assertEqual([x["usage"]["output_tokens"] for x in results["roles"]], [10] * 4)
        self.assertIn("fake_codex.py", " ".join(results["transport"]))

    def test_invalid_model_output_does_not_validate(self):
        # In the fake backend this is simulated by an invalid scope-specific response.
        self.scenario("invalid-smoke")
        with self.assertRaises(OrchestratorError):
            smoke_models(self.root, load_config(self.root), ["luna"], True)

    def test_invalid_role_or_timeout_rejected_before_paid_call(self):
        for roles, timeout in [(["luna", "luna"], 120), (["invalid"], 120), (["luna"], 0)]:
            with self.assertRaises(OrchestratorError):
                smoke_models(self.root, load_config(self.root), roles, True, timeout)
        self.assertEqual(self.calls(), [])
