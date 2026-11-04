from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from codex_orchestrator.cli import main
from codex_orchestrator.contracts import (OrchestratorError, PLAN_SCHEMA, safe_id, safe_path,
                                           validate, validate_plan)
from codex_orchestrator.hooks import handle
from codex_orchestrator.install import install
from codex_orchestrator.runtime import gate, report, run, session_file
from codex_orchestrator.system import (atomic_json, changed, exclusive, git, load_config,
                                       repo_root, snapshot, store_root)

FAKE = Path(__file__).with_name("fake_codex.py").resolve()


def plan(tier="luna"):
    return {"version": 1, "objective": "Deliver a valid value", "acceptance": ["All behavior works"],
            "integration_checks": ["unit"], "tasks": [{"id": "T1", "tier": tier,
            "objective": "Write a valid value", "scope": ["value.txt"],
            "acceptance": ["Value is valid"], "checks": ["unit"], "depends_on": []}]}


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Test User")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (self.root / "value.txt").write_text("initial")
        self.config = ('version = 1\n[runtime]\ncommand = ' + json.dumps([sys.executable, str(FAKE)]) +
                       '\n[checks]\nunit = ' + json.dumps([sys.executable, "-c",
                       "from pathlib import Path; assert Path('value.txt').read_text() == 'ok'"]) + '\n')
        (self.root / "orchestrator.toml").write_text(self.config)
        self.plan = Path(self.temp.name) / "plan.json"
        atomic_json(self.plan, plan())
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "initial")
        self.env = patch.dict(os.environ, {"CO_TEST_SCENARIO": "ok", "CO_TEST_RECORD": str(Path(self.temp.name) / "calls.jsonl")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def configure(self, **settings):
        text = self.config.replace("[checks]", "".join(f"{k} = {v}\n" for k, v in settings.items()) + "[checks]")
        (self.root / "orchestrator.toml").write_text(text)
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "configure")

    def states(self):
        return [json.loads(p.read_text()) for p in (store_root(self.root) / "runs").glob("*/state.json")]

    def calls(self):
        file = Path(os.environ["CO_TEST_RECORD"])
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def scenario(self, name):
        os.environ["CO_TEST_SCENARIO"] = name

    def assert_blocked(self, scenario, match=None):
        self.scenario(scenario)
        with self.assertRaises(OrchestratorError) as error:
            run(self.root, self.plan)
        if match:
            self.assertIn(match, str(error.exception))
        state = self.states()[-1]
        self.assertEqual(state["status"], "blocked")
        with self.assertRaises(OrchestratorError):
            gate(self.root, state["id"])
        return state


class RuntimeTests(ProjectCase):
    def test_luna_end_to_end_direct_route_and_global_validation(self):
        state = run(self.root, self.plan)
        self.assertEqual([(x["role"], x["mode"]) for x in self.calls()],
                         [("luna", "implement"), ("terra", "review"), ("astra", "global")])
        self.assertTrue(gate(self.root, state["id"])["approved"])
        self.assertEqual((self.root / "value.txt").read_text(), "initial")
        self.assertEqual((Path(state["worktree"]) / "value.txt").read_text(), "ok")
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual(changed(self.root), [])
        for call in self.calls():
            self.assertIn("agents.enabled=false", call["argv"])
            mode = call["argv"][call["argv"].index("--sandbox") + 1]
            self.assertEqual(mode, "workspace-write" if call["mode"] == "implement" else "read-only")

    def test_terra_flow(self):
        atomic_json(self.plan, plan("terra"))
        run(self.root, self.plan)
        self.assertEqual([x["role"] for x in self.calls()], ["terra", "sol", "astra"])

    def test_sol_flow(self):
        atomic_json(self.plan, plan("sol"))
        run(self.root, self.plan)
        self.assertEqual([x["role"] for x in self.calls()], ["sol", "astra", "astra"])

    def test_test_failure_corrected_before_paid_review(self):
        self.scenario("test-fix")
        state = run(self.root, self.plan)
        self.assertEqual([x["role"] for x in self.calls()], ["luna", "luna", "terra", "astra"])
        self.assertEqual(state["tasks"]["T1"]["attempts"]["luna"], 2)

    def test_rejected_review_returns_to_same_executor(self):
        self.scenario("reject-once")
        state = run(self.root, self.plan)
        self.assertEqual([x["role"] for x in self.calls()], ["luna", "terra", "luna", "terra", "astra"])
        self.assertEqual(len(state["tasks"]["T1"]["reviews"]), 2)

    def test_escalation_changes_reviewer(self):
        self.scenario("escalate")
        state = run(self.root, self.plan)
        self.assertEqual([x["role"] for x in self.calls()], ["luna", "terra", "sol", "astra"])
        self.assertEqual(state["tasks"]["T1"]["proof"]["reviewer"], "sol")

    def test_nested_delegation_and_integration(self):
        self.scenario("delegate")
        atomic_json(self.plan, plan("sol"))
        state = run(self.root, self.plan)
        self.assertEqual(set(state["tasks"]), {"T1", "T1.child", "T1.child.child"})
        self.assertEqual([x["role"] for x in self.calls()],
                         ["sol", "terra", "luna", "terra", "terra", "sol", "sol", "astra", "astra"])
        self.assertTrue(all(t["status"] == "approved" for t in state["tasks"].values()))

    def test_global_findings_become_targeted_repairs(self):
        self.scenario("global-fix")
        state = run(self.root, self.plan)
        self.assertIn("repair-1-1", state["tasks"])
        self.assertEqual(state["global_round"], 2)
        self.assertTrue(gate(self.root, state["id"])["approved"])

    def test_scope_violation_stops_before_review(self):
        self.assert_blocked("scope", "Out-of-scope")
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_response_not_treated_as_success(self):
        self.assert_blocked("invalid-output", "fields must be")

    def test_mutating_reviewer_is_rejected(self):
        self.assert_blocked("tamper-review", "Reviewer changed")

    def test_wrong_snapshot_is_rejected(self):
        self.assert_blocked("stale-review", "different source snapshot")

    def test_approval_with_failed_criteria_is_rejected(self):
        self.assert_blocked("false-approve", "Approval conflicts")

    def test_attempt_limits_stop_infinite_rework(self):
        self.configure(attempts_per_tier=1)
        self.assert_blocked("endless-failure", "Sol attempt limit")
        self.assertEqual([x["role"] for x in self.calls()], ["luna", "terra", "sol"])

    def test_call_budget_includes_reviews_and_global(self):
        self.configure(max_calls=2)
        self.assert_blocked("ok", "Model call budget")
        self.assertEqual(len(self.calls()), 2)

    def test_timeout_blocks_without_retrying_blindly(self):
        self.configure(model_timeout=1)
        self.assert_blocked("timeout", "timeout")
        self.assertEqual(len(self.calls()), 1)

    def test_resume_retries_review_not_implementation(self):
        state = self.assert_blocked("fail-review", "Codex failed")
        self.scenario("ok")
        completed = run(self.root, run_id=state["id"])
        self.assertEqual(completed["status"], "approved")
        self.assertEqual([x["mode"] for x in self.calls()], ["implement", "review", "review", "global"])

    def test_gate_rejects_edits_after_approval(self):
        state = run(self.root, self.plan)
        (Path(state["worktree"]) / "value.txt").write_text("unreviewed")
        with self.assertRaisesRegex(OrchestratorError, "stale"):
            gate(self.root, state["id"])

    def test_resume_rejects_external_edits(self):
        state = self.assert_blocked("fail-review")
        (Path(state["worktree"]) / "value.txt").write_text("changed externally")
        with self.assertRaisesRegex(OrchestratorError, "outside controller"):
            run(self.root, run_id=state["id"])

    def test_completed_resume_is_idempotent(self):
        state = run(self.root, self.plan)
        run(self.root, run_id=state["id"])
        self.assertEqual(len(self.calls()), 3)

    def test_usage_keeps_reasoning_separate_not_double_counted(self):
        state = run(self.root, self.plan)
        totals = report(state)["usage_by_role_and_stage"]
        self.assertEqual(totals["luna/implement"]["tokens"]["output_tokens"], 10)
        self.assertEqual(totals["luna/implement"]["tokens"]["reasoning_output_tokens"], 4)

    def test_dirty_original_checkout_is_refused(self):
        (self.root / "value.txt").write_text("user work")
        with self.assertRaisesRegex(OrchestratorError, "Commit your"):
            run(self.root, self.plan)
        self.assertEqual((self.root / "value.txt").read_text(), "user work")
        self.assertEqual(self.calls(), [])

    def test_concurrent_controller_is_refused(self):
        with exclusive(store_root(self.root) / "controller.lock"):
            with self.assertRaisesRegex(OrchestratorError, "Another controller"):
                run(self.root, self.plan)

    def test_task_dependency_order(self):
        p = plan()
        child = {**p["tasks"][0], "id": "T2", "tier": "terra", "depends_on": ["T1"]}
        p["tasks"].insert(0, child)
        atomic_json(self.plan, p)
        run(self.root, self.plan)
        self.assertEqual([x["id"] for x in self.calls() if x["mode"] == "implement"], ["T1", "T2"])

    def test_check_that_changes_source_is_rejected(self):
        text = self.config.replace("assert Path('value.txt').read_text() == 'ok'", "Path('value.txt').write_text('mutated')")
        (self.root / "orchestrator.toml").write_text(text)
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "bad check")
        self.assert_blocked("ok", "changed source/index")

    def test_no_global_approval_with_failing_integration(self):
        text = self.config + 'integration = ["{python}", "-c", "raise SystemExit(1)"]\n'
        (self.root / "orchestrator.toml").write_text(text)
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "integration check")
        p = plan(); p["integration_checks"] = ["integration"]
        atomic_json(self.plan, p)
        self.assert_blocked("ok", "integration checks failed")


    def test_worktree_outside_git_metadata_and_original_checkout(self):
        state = run(self.root, self.plan)
        work = Path(state["worktree"])
        self.assertFalse(work.is_relative_to(self.root))
        self.assertFalse(work.is_relative_to(store_root(self.root).parent))
        self.assertEqual(changed(self.root), [])

    def test_initial_plan_respects_task_budget(self):
        self.configure(max_tasks=1)
        p = plan(); p["tasks"].append({**p["tasks"][0], "id": "T2"})
        atomic_json(self.plan, p)
        with self.assertRaisesRegex(OrchestratorError, "Initial plan exceeds"):
            run(self.root, self.plan)
        self.assertEqual(self.calls(), [])

    def test_worktree_inside_git_directory_is_refused(self):
        self.configure(worktree_root=json.dumps(str(self.root / ".git/worktrees-private")))
        with self.assertRaisesRegex(OrchestratorError, "outside the project"):
            run(self.root, self.plan)
        self.assertEqual(self.calls(), [])

    def test_mutating_commit_hook_invalidates_approval(self):
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf hacked > value.txt\ngit add value.txt\n")
        hook.chmod(0o755)
        self.assert_blocked("ok", "Commit hooks changed reviewed source")

    def test_delegation_cannot_expand_scope_or_keep_same_tier(self):
        from codex_orchestrator.runtime import Engine
        cfg = load_config(self.root)
        parent = {**plan("terra")["tasks"][0], "attempts": {"terra": 1}}
        child = {k: v for k, v in plan()["tasks"][0].items() if k != "depends_on"}
        engine = Engine(self.root, {"worktree": str(self.root), "config": cfg,
                                  "tasks": {"T1": parent}}, Path(self.temp.name) / "state")
        for invalid in ({**child, "scope": ["other.txt"]}, {**child, "tier": "terra"},
                        {**child, "scope": [".CoDeX/config.toml"]}):
            with self.assertRaises(OrchestratorError):
                engine.add_children(parent, [invalid])


class ContractTests(unittest.TestCase):
    def test_valid_plan(self):
        validate_plan(plan(), {"unit": []})

    def test_missing_fields(self):
        p = plan(); del p["objective"]
        with self.assertRaises(OrchestratorError):
            validate(p, PLAN_SCHEMA)

    def test_unknown_fields(self):
        p = plan(); p["approval"] = True
        with self.assertRaises(OrchestratorError):
            validate(p, PLAN_SCHEMA)

    def test_bool_not_integer(self):
        p = plan(); p["version"] = True
        with self.assertRaises(OrchestratorError):
            validate(p, PLAN_SCHEMA)

    def test_cycle_and_missing_dependencies(self):
        for dep in ("T1", "missing"):
            p = plan(); p["tasks"][0]["depends_on"] = [dep]
            with self.assertRaises(OrchestratorError):
                validate_plan(p, {"unit": []})

    def test_duplicate_criteria(self):
        p = plan(); p["tasks"][0]["acceptance"] *= 2
        with self.assertRaises(OrchestratorError):
            validate_plan(p, {"unit": []})

    def test_invalid_paths(self):
        for path in ("../secret", "/tmp/file", ".git/config", "src/.git/config", ".codex/config.toml",
                     ".agents/skills/x", "orchestrator.toml", "src/*", "src/../file", "C:\\data", ".", "src//x"):
            with self.subTest(path=path), self.assertRaises(OrchestratorError):
                safe_path(path)

    def test_invalid_id(self):
        for value in ("../../etc", "-argument", "two words", "x" * 81):
            with self.assertRaises(OrchestratorError):
                safe_id(value)


class InstallAndHookTests(ProjectCase):
    def test_install_preserves_existing_instructions_config_and_hooks(self):
        (self.root / "AGENTS.md").write_text("Existing project rules.\n")
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_text('model = "user-model"\n')
        original_hook = {"hooks": [{"type": "command", "command": "echo existing"}]}
        atomic_json(self.root / ".codex/hooks.json", {"hooks": {"Stop": [original_hook]}})
        install(self.root)
        self.assertTrue((self.root / "AGENTS.md").read_text().startswith("Existing project rules."))
        config = tomllib.loads((self.root / ".codex/config.toml").read_text())
        self.assertEqual(config["model"], "user-model")
        hooks = json.loads((self.root / ".codex/hooks.json").read_text())
        self.assertIn(original_hook, hooks["hooks"]["Stop"])
        self.assertEqual(install(self.root), [])
        for file in (self.root / ".codex/agents").glob("*.toml"):
            data = tomllib.loads(file.read_text())
            self.assertTrue({"name", "description", "model", "developer_instructions"} <= data.keys())

    def test_install_refuses_overwriting_modified_skill(self):
        install(self.root)
        file = self.root / ".agents/skills/codex-orchestrator/SKILL.md"
        file.write_text("User modification")
        with self.assertRaisesRegex(OrchestratorError, "was edited"):
            install(self.root)
        self.assertEqual(file.read_text(), "User modification")

    def test_install_rejects_escaped_symlink(self):
        outside = Path(self.temp.name) / "outside"; outside.mkdir()
        (self.root / ".agents").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(OrchestratorError, "symlinked"):
            install(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_hook_unrelated_session_is_noop(self):
        self.assertEqual(handle({"hook_event_name": "Stop", "session_id": "other", "cwd": str(self.root)}), {})

    def test_managed_worker_hooks_are_noop(self):
        with patch.dict(os.environ, {"CO_MANAGED": "1"}):
            self.assertEqual(handle({"hook_event_name": "Stop"}), {})

    def test_hook_approved_session_passes(self):
        state = run(self.root, self.plan, session_id="root-session")
        self.assertEqual(handle({"hook_event_name": "Stop", "session_id": "root-session", "cwd": str(self.root)}), {})
        self.assertTrue(session_file(store_root(self.root), "root-session").exists())

    def test_stop_hook_prevents_continuation_loop(self):
        state = run(self.root, self.plan, session_id="root-session")
        file = store_root(self.root) / "runs" / state["id"] / "state.json"
        state["status"] = "running"; atomic_json(file, state)
        event = {"hook_event_name": "Stop", "session_id": "root-session", "cwd": str(self.root)}
        self.assertEqual(handle(event)["decision"], "block")
        self.assertFalse(handle({**event, "stop_hook_active": True})["continue"])

    def test_blocked_run_can_be_reported_without_forced_retries(self):
        state = self.assert_blocked("fail-review")
        atomic_json(session_file(store_root(self.root), "root-session"), {"run_id": state["id"]})
        result = handle({"hook_event_name": "Stop", "session_id": "root-session", "cwd": str(self.root)})
        self.assertIn("NOT approved", result["systemMessage"])
        self.assertNotIn("decision", result)

    def test_cli_doctor_and_validate_do_not_invoke_models(self):
        for argv in (["--repo", str(self.root), "doctor"], ["--repo", str(self.root), "validate", str(self.plan)]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(argv), 0)
            self.assertIsInstance(json.loads(out.getvalue()), dict)
        self.assertEqual(self.calls(), [])

    def test_invalid_hook_input_returns_json_failure_not_traceback(self):
        with patch("sys.stdin", io.StringIO("[]")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["hooks"]), 0)
        self.assertFalse(json.loads(out.getvalue())["continue"])

    def test_config_rejects_shell_string(self):
        (self.root / "orchestrator.toml").write_text('version = 1\n[checks]\nunit = "echo success"\n')
        with self.assertRaisesRegex(OrchestratorError, "argv array"):
            load_config(self.root)


if __name__ == "__main__":
    unittest.main()
