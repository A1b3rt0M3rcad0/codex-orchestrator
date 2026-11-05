"""Explicit test backend; never an implementation of the real Codex service."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("codex-test 0.2")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("--json --ephemeral --sandbox --output-schema --output-last-message")
    raise SystemExit(0)
req = json.load(sys.stdin)
mode, role, task = req["mode"], req["role"], req["task"]
scenario = os.environ.get("CO_TEST_SCENARIO", "ok")
record_file = Path(os.environ["CO_TEST_RECORD"])
old = [json.loads(x) for x in record_file.read_text().splitlines()] if record_file.exists() else []
row = {"mode": mode, "role": role, "task_id": task["id"], "worktree": req["worktree"],
       "argv": sys.argv[1:], "request": req, "started": time.monotonic()}
fd = os.open(record_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
os.write(fd, (json.dumps(row) + "\n").encode())
os.close(fd)
output = Path(sys.argv[sys.argv.index("-o") + 1])
work = Path(req["worktree"])
print(json.dumps({"type": "turn.started"}), flush=True)
usage = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 4}
if mode == "implement" and scenario == "timeout":
    time.sleep(5)
if mode == "implement" and scenario == "overspend":
    print(json.dumps({"type": "turn.completed", "usage": {**usage, "input_tokens": 200000}}), flush=True)
    time.sleep(3)
    (work / "not-allowed.txt").write_text("monitor failed to cancel")
if mode == "implement" and scenario == "request-overspend":
    Path(os.environ["CO_USAGE_FILE"]).write_text(json.dumps({"type": "request.usage", "usage": {**usage, "input_tokens": 200000}}) + "\n")
    time.sleep(3)
    (work / "not-allowed.txt").write_text("hook observer failed to cancel")
if scenario == "parallel-failure" and mode == "implement" and task["id"] == "T2":
    time.sleep(3)
    (work / "late.txt").write_text("cancellation failed")
if scenario == "parallel" and mode == "implement":
    time.sleep(0.25)

if mode == "implement":
    result = {"status": "implemented", "summary": "Simulated implementation for controller tests.", "children": []}
    if scenario in {"delegate", "delegate-twice", "delegate-escape", "delegate-oracle", "risk-launder", "delegate-skill-invent", "delegate-skill-drop"} and role in {"sol", "terra"} and (not req.get("children") or scenario == "delegate-twice"):
        child = {k: task[k] for k in ("objective", "scope", "reads", "acceptance", "checks")}
        child.update(id="child", tier="terra" if role == "sol" else "luna", risk_tags=[])
        if scenario == "delegate-escape":
            child["scope"] = ["outside.txt"]
        if scenario == "delegate-oracle":
            child["acceptance"] = ["A new unreviewed criterion"]
        if scenario == "delegate-skill-invent":
            child["skills"] = {"required": ["unapproved"], "recommended": []}
        if scenario == "delegate-skill-drop":
            child["skills"] = {"required": [], "recommended": []}
        result.update(status="delegate", children=[child])
    elif scenario == "escalate" and role == "luna":
        result.update(status="blocked", summary="Tried the specified approach; counterexample prevents completion.")
    else:
        path = work / task["scope"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        prior = sum(x["task_id"] == task["id"] and x["mode"] == "implement" for x in old)
        value = "bad" if scenario == "endless" or (scenario == "test-fix" and prior == 0) else "ok"
        if task["id"].startswith("repair-") and scenario != "checkpoint-never-fix":
            value = "ok2"
        if scenario == "risk-after":
            value = "ok # authentication"
        path.write_text(value)
        if scenario == "scope" or (scenario == "parallel-failure" and task["id"] == "T1"):
            (work / "outside.txt").write_text("outside contract")
        if scenario == "tamper-tests":
            (work / "oracle.py").write_text("pass\n")
        if scenario == "head-change":
            import subprocess
            subprocess.run(["git", "-c", "user.name=Bad", "-c", "user.email=bad@example.invalid", "commit", "--allow-empty", "-m", "bad"], cwd=work, check=True, stdout=sys.stderr)
else:
    result = {"decision": "approve", "snapshot": req["snapshot"], "summary": "Simulated review, not model-quality evidence.",
              "findings": [], "acceptance": [{"criterion": c, "passed": True, "evidence": "Fixture check logs and fixture source."} for c in task["acceptance"]]}
    reject = (mode == "review" and scenario == "reject-once" and not any(x["mode"] == "review" and x["task_id"] == task["id"] for x in old))
    reject |= mode == "contract" and scenario == "contract-reject"
    reject |= mode == "checkpoint" and scenario in {"checkpoint-fix", "checkpoint-never-fix"}
    reject |= mode == "global" and scenario == "global-fix" and not any(x["mode"] == "global" for x in old)
    reject |= mode == "adversarial" and scenario == "adversarial-reject" and not any(x["mode"] == "adversarial" for x in old)
    reject |= mode == "global" and scenario == "global-scope"
    if reject:
        result["decision"] = "changes_requested"
        result["findings"] = [{"task_id": "global" if scenario == "global-scope" else "T1", "file": "value.txt", "detail": "Fix the observed fixture counterexample."}]
        result["acceptance"][0]["passed"] = False
    if scenario == "invalid-smoke" and mode == "smoke":
        result = {"wrong": True}
    if scenario == "invalid-output" and mode == "review":
        result = {"wrong": True}
    if scenario == "tamper-review" and mode == "review":
        (work / "value.txt").write_text("reviewer changed source")
    if scenario == "stale-review" and mode == "review":
        result["snapshot"] = "different"
    if scenario == "false-approve" and mode == "review":
        result["acceptance"][0]["passed"] = False
    if scenario == "blocked-review" and mode == "review":
        result["decision"] = "blocked"
    if scenario == "omit-criterion" and mode == "review":
        result["acceptance"] = []
output.write_text(json.dumps(result))
if scenario == "hook-usage":
    Path(os.environ["CO_USAGE_FILE"]).write_text(json.dumps({"type": "request.usage", "usage": usage}) + "\n")
if not (scenario == "missing-usage" and mode == "implement"):
    if scenario == "negative-usage" and mode == "implement":
        usage["input_tokens"] = -1
    print(json.dumps({"type": "turn.completed", "usage": usage}), flush=True)
