"""End-to-end fake CLI coverage for deterministic skill binding and evidence."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path

from codex_orchestrator.cli import main
from codex_orchestrator.contracts import OrchestratorError, validate_plan
from codex_orchestrator.runtime import gate
from codex_orchestrator.skills import discover, stage_context, validate_bindings
from helpers import ProjectCase


class SkillBindingTests(ProjectCase):
    def add_skill(self, name="tdd", description="Write behavior-driven tests first.",
                  body="Use test-first design and do not weaken existing tests."):
        path = self.root / ".agents" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# Rules\n\n{body}\n",
                        encoding="utf-8")
        self.flush()
        return path

    def bind(self, required=None, recommended=None):
        self.plan["tasks"][0]["skills"] = {
            "required": list(required or []), "recommended": list(recommended or [])}
        self.flush()

    def test_catalog_lists_only_committed_project_skills_with_hashes(self):
        p = self.add_skill()
        catalog = discover(self.root)
        self.assertEqual(list(catalog), ["tdd"])
        self.assertEqual(catalog["tdd"]["path"], ".agents/skills/tdd/SKILL.md")
        self.assertEqual(len(catalog["tdd"]["sha256"]), 64)
        self.assertIn("behavior-driven", catalog["tdd"]["description"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--repo", str(self.root), "skills"]), 0)
        self.assertEqual(json.loads(out.getvalue())["skills"][0]["name"], "tdd")
        p.write_text(p.read_text() + "\n# Modified after commit\n")
        self.assertNotEqual(discover(self.root)["tdd"]["sha256"], catalog["tdd"]["sha256"])

    def test_skills_require_commit_and_reject_symlinks(self):
        path = self.root / ".agents/skills/tdd/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: tdd\ndescription: Mandatory testing\n---\n")
        with self.assertRaisesRegex(OrchestratorError, "Uncommitted skill"):
            discover(self.root)
        path.unlink()
        outside = Path(self.temp.name) / "outside-skill"
        outside.write_text("---\nname: tdd\ndescription: Mandatory testing\n---\n")
        path.symlink_to(outside)
        self.flush()
        with self.assertRaisesRegex(OrchestratorError, "Symlink"):
            discover(self.root)

    def test_duplicated_metadata_name_is_rejected(self):
        self.add_skill("tdd")
        other = self.root / ".agents/skills/another/SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("---\nname: tdd\ndescription: Another test skill\n---\n")
        self.flush()
        with self.assertRaisesRegex(OrchestratorError, "Duplicate"):
            discover(self.root)

    def test_required_is_embedded_and_recommended_is_lazy(self):
        self.add_skill("tdd", body="ALWAYS obey the approved acceptance criteria.")
        self.add_skill("code-review", body="Look for release regressions.")
        self.bind(required=["tdd"], recommended=["code-review"])
        state = self.execute()
        self.assertEqual(state["status"], "approved")
        self.assertIn("tdd", state["skill_catalog"])
        calls = self.calls()
        implementation = next(c for c in calls if c["mode"] == "implement")
        review = next(c for c in calls if c["mode"] == "review")
        skill = implementation["request"]["skill_context"]
        self.assertIn("ALWAYS obey", skill["required"][0]["instructions"])
        self.assertNotIn("instructions", skill["recommended"][0])
        self.assertNotIn("instructions", review["request"]["skill_context"]["required"][0])
        contract = next(c for c in calls if c["mode"] == "contract")
        self.assertIn("tdd", contract["request"]["reviewed_skills"])
        self.assertEqual(contract["request"]["reviewed_skills"]["tdd"]["name"], "tdd")
        calls_with_skills = [c for c in state["calls"] if c["task_id"] == "T1"]
        self.assertTrue(all("skill_evidence" in c for c in calls_with_skills))
        self.assertTrue(gate(self.root, state["id"])["approved"])

    def test_unknown_skill_fails_validation_before_any_model_call(self):
        self.bind(required=["nonexistent"])
        with self.assertRaisesRegex(OrchestratorError, "unavailable"):
            self.execute()
        self.assertEqual(self.calls(), [])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--repo", str(self.root), "validate", str(self.plan_file)]), 1)

    def test_reserved_controller_skill_cannot_be_assigned(self):
        self.add_skill("codex-orchestrator", description="Runtime controller itself")
        self.bind(required=["codex-orchestrator"])
        with self.assertRaisesRegex(OrchestratorError, "cannot be assigned"):
            self.execute()

    def test_duplicate_and_overlapping_task_skill_names_are_rejected(self):
        self.add_skill()
        self.bind(required=["tdd"], recommended=["tdd"])
        with self.assertRaisesRegex(OrchestratorError, "unique"):
            validate_plan(self.plan, self.cfg["checks"])

    def test_skill_change_in_delivery_worktree_invalidates_gate(self):
        self.add_skill()
        self.bind(required=["tdd"])
        state = self.execute()
        skill = Path(state["worktree"]) / ".agents/skills/tdd/SKILL.md"
        skill.write_text("replaced")
        with self.assertRaises(OrchestratorError):
            gate(self.root, state["id"])

    def test_tampered_dispatched_request_invalidates_gate(self):
        self.add_skill()
        self.bind(required=["tdd"])
        state = self.execute()
        rec = next(c for c in state["calls"] if c["mode"] == "implement")
        path = Path(rec["skill_evidence"]["request"]["path"])
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(OrchestratorError, "artifact changed"):
            gate(self.root, state["id"])

    def test_delegated_child_inherits_mandatory_skills(self):
        self.add_skill()
        self.bind(required=["tdd"])
        self.plan["tasks"][0]["tier"] = "terra"
        self.flush()
        self.scenario("delegate")
        state = self.execute()
        child = state["tasks"]["T1.child"]
        self.assertEqual(child["skills"]["required"], ["tdd"])
        child_calls = [c for c in self.calls() if c["task_id"] == "T1.child" and c["mode"] == "implement"]
        self.assertTrue(child_calls)
        self.assertEqual(child_calls[0]["request"]["skill_context"]["required"][0]["name"], "tdd")
        self.assertTrue(gate(self.root, state["id"])["approved"])

    def test_child_cannot_invent_or_drop_required_skills(self):
        self.add_skill()
        self.bind(required=["tdd"])
        self.plan["tasks"][0]["tier"] = "terra"
        self.flush()
        self.blocked("delegate-skill-invent", "unreviewed skills")
        # Restart from the same committed fixture to test a distinct invalid
        # child declaration without relying on a resumed blocked run.
        self.blocked("delegate-skill-drop", "cannot drop required skills")

    def test_required_skill_larger_than_prompt_allowance_blocks(self):
        self.add_skill(body="Use tests.\n" * 5000)
        self.bind(required=["tdd"])
        self.blocked("ok", "Required skill instructions exceed")
        self.assertFalse(any(call["mode"] == "implement" for call in self.calls()))

    def test_frontmatter_block_description_supported(self):
        path = self.root / ".agents/skills/architecture/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: architecture\ndescription: >-\n  Apply a bounded architecture review.\n  Prefer domain invariants.\n---\n# Guidelines\n")
        self.flush()
        self.assertIn("domain invariants", discover(self.root)["architecture"]["description"])
