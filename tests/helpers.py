from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from codex_orchestrator.contracts import OrchestratorError
from codex_orchestrator.system import atomic_json, command, git, store_root
from codex_orchestrator.runtime import run

FAKE = Path(__file__).with_name("fake_codex.py").resolve()


def toml(data):
    lines = []
    def emit(table, prefix=""):
        if prefix:
            lines.append("[" + prefix + "]")
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(json.dumps(k) + " = " + json.dumps(v))
        lines.append("")
        for k, v in table.items():
            if isinstance(v, dict):
                emit(v, prefix + "." + json.dumps(k) if prefix else json.dumps(k))
    emit(data)
    return "\n".join(lines)


def plan(tier="luna", file="value.txt"):
    return {"version": 2, "objective": "Deliver valid values", "acceptance": ["The assembled behavior is correct"],
            "integration_checks": ["global"], "checkpoint_checks": ["smoke"], "tasks": [{
                "id": "T1", "tier": tier, "objective": "Produce the specified valid value", "scope": [file], "reads": [],
                "acceptance": ["Value starts with ok"], "checks": ["unit"], "depends_on": [], "risk_tags": [],
                "test_contract": {"files": ["oracle.py"], "cases": [{"criterion": "Value starts with ok", "check": "unit", "baseline": "fail", "rationale": "Initial value is not implemented"}]},
                "adversarial_checks": []}]}


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Fixture User")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (self.root / "value.txt").write_text("initial")
        (self.root / "oracle.py").write_text("from pathlib import Path\nimport sys, os\np = Path(sys.argv[1])\nassert p.read_text().startswith('ok'), 'expected valid value'\nif os.environ.get('CO_TEST_MUTATING_CHECK') == '1': p.write_text('mutated')\n")
        (self.root / "smoke.py").write_text("from pathlib import Path\nimport os\nassert Path('value.txt').exists()\nif os.environ.get('CO_TEST_SMOKE_STRICT') == '1': assert Path('value.txt').read_text() == 'ok2'\n")
        (self.root / "suite.py").write_text("from pathlib import Path\nimport os\nfor p in Path('.').glob('value*.txt'): assert p.read_text().startswith('ok')\nif os.environ.get('CO_TEST_GLOBAL_STRICT') == '1': assert Path('value.txt').read_text() == 'ok2'\n")
        self.cfg = {"version": 2, "runtime": {"command": [sys.executable, str(FAKE)]}, "checks": {
            "unit": [sys.executable, "oracle.py", "value.txt"], "smoke": [sys.executable, "smoke.py"],
            "global": [sys.executable, "suite.py"], "negative": [sys.executable, "oracle.py", "value.txt", "negative"]}}
        self.plan_file = Path(self.temp.name) / "plan.json"
        self.plan = plan()
        self.record = Path(self.temp.name) / "calls.jsonl"
        self.env = patch.dict(os.environ, {"CO_TEST_SCENARIO": "ok", "CO_TEST_RECORD": str(self.record),
                                          "CO_TEST_MUTATING_CHECK": "0", "CO_TEST_SMOKE_STRICT": "0", "CO_TEST_GLOBAL_STRICT": "0"})
        self.env.start()
        self.flush()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def flush(self):
        (self.root / "orchestrator.toml").write_text(toml(self.cfg))
        atomic_json(self.plan_file, self.plan)
        git(self.root, "add", ".")
        git(self.root, "commit", "--allow-empty", "-m", "fixture setup")

    def execute(self):
        return run(self.root, self.plan_file)

    def scenario(self, name):
        os.environ["CO_TEST_SCENARIO"] = name

    def calls(self):
        return [json.loads(x) for x in self.record.read_text().splitlines()] if self.record.exists() else []

    def states(self):
        return [json.loads(p.read_text()) for p in sorted((store_root(self.root) / "runs").glob("*/state.json"), key=lambda p: p.stat().st_mtime_ns)]

    def blocked(self, scenario, match=None):
        self.scenario(scenario)
        with self.assertRaises(OrchestratorError) as error:
            self.execute()
        if match:
            self.assertIn(match, str(error.exception))
        states = self.states()
        if states:
            self.assertEqual(states[-1]["status"], "blocked")
        return states[-1] if states else None

    def signer(self):
        self.key = Path(self.temp.name) / "review-key.pem"
        command(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)], self.root)
        self.signers = Path(self.temp.name) / "allowed_signers"
        public = command(["openssl", "pkey", "-in", str(self.key), "-pubout"], self.root).decode()
        atomic_json(self.signers, {"version": 1, "identities": {"reviewer@example.invalid": public}})
        self.cfg["verification"] = {"trusted_signers": str(self.signers)}
        self.flush()

    def critical(self):
        self.signer()
        self.plan["tasks"][0]["risk_tags"] = ["auth"]
        self.plan["tasks"][0]["adversarial_checks"] = ["negative"]
        self.plan["tasks"][0]["test_contract"]["mutations"] = [{
            "file": "value.txt", "find": "ok", "replace": "bad", "check": "unit",
            "failure_marker": "AssertionError"}]
        self.flush()

    def second_task(self, depends=False):
        (self.root / "value2.txt").write_text("initial")
        self.cfg["checks"]["unit2"] = [sys.executable, "oracle.py", "value2.txt"]
        task = copy.deepcopy(self.plan["tasks"][0])
        task.update(id="T2", scope=["value2.txt"], checks=["unit2"], depends_on=["T1"] if depends else [])
        task["test_contract"]["cases"][0]["check"] = "unit2"
        self.plan["tasks"].append(task)
        self.flush()
