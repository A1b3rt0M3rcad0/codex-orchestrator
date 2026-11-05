"""Measured benchmark report: no inferred quality equivalence."""
from __future__ import annotations
import unittest
from helpers import ProjectCase
from codex_orchestrator.benchmark import benchmark, wilson
from codex_orchestrator.contracts import OrchestratorError
from codex_orchestrator.system import git

class BenchmarkTests(ProjectCase):
    def test_wilson_interval_is_wide_for_small_sample(self):
        lo, hi = wilson(1, 1)
        self.assertLess(lo, .3)
        self.assertEqual(hi, 1.0)
        self.assertEqual(wilson(0, 0), (0., 1.))

    def test_two_approved_repeated_trials_remain_exploratory(self):
        a = self.execute()
        b = self.execute()
        result = benchmark(self.root, [a["id"], b["id"]])
        self.assertEqual(result["plan_sha256"], a["plan_hash"])
        self.assertEqual(len(result["variants"]), 1)
        row = result["variants"][0]
        self.assertEqual(row["verified_approvals"], 2)
        self.assertEqual(row["trials"], 2)
        self.assertEqual(row["reported_cost_usd"], None)
        self.assertIn("exploratory", row["readiness"])
        self.assertIsNone(row["mutation_sensitivity"])
        self.assertLess(row["wilson95"][0], .7)

    def test_duplicate_runs_are_rejected(self):
        a = self.execute()
        with self.assertRaisesRegex(OrchestratorError, "two distinct"):
            benchmark(self.root, [a["id"], a["id"]])

    def test_different_starting_commits_rejected(self):
        a = self.execute()
        (self.root / "new.txt").write_text("changed baseline")
        git(self.root, "add", "new.txt")
        git(self.root, "commit", "-m", "different baseline")
        b = self.execute()
        with self.assertRaisesRegex(OrchestratorError, "SAME plan hash and baseline"):
            benchmark(self.root, [a["id"], b["id"]])
