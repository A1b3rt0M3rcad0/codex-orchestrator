"""Local subprocess double. Never import this as a production backend."""
import json
import os
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("codex-fake 1.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("--json --ephemeral --sandbox --output-schema --output-last-message")
    raise SystemExit(0)
request = json.load(sys.stdin)
mode, task, role = request["mode"], request["task"], request["role"]
scenario = os.environ.get("CO_TEST_SCENARIO", "ok")
record_file = os.environ.get("CO_TEST_RECORD")
if record_file:
    with open(record_file, "a") as record:
        record.write(json.dumps({"mode": mode, "role": role, "id": task["id"], "argv": sys.argv}) + "\n")
if scenario == "timeout":
    time.sleep(30)
if scenario == "fail-review" and mode == "review":
    print("Simulated service error", file=sys.stderr)
    raise SystemExit(42)
if mode == "implement":
    result = {"status": "implemented", "summary": "Implemented the fixture.", "children": []}
    if scenario == "delegate" and not task.get("children") and role != "luna":
        tier = "terra" if role == "sol" else "luna"
        result = {"status": "delegate", "summary": "Delegate a bounded child before edits.",
                  "children": [{"id": "child", "tier": tier, "objective": "Write valid value",
                                "scope": ["value.txt"], "acceptance": ["Value is valid"], "checks": ["unit"]}]}
    elif scenario == "escalate" and role == "luna":
        result = {"status": "blocked", "summary": "Need a stronger implementer", "children": []}
    else:
        value = "bad" if scenario == "test-fix" and role == "luna" and task["attempts"]["luna"] == 1 else "ok"
        Path("value.txt").write_text(value)
        if scenario == "scope":
            Path("unauthorized.txt").write_text("not allowed")
        if scenario == "endless-failure":
            Path("value.txt").write_text("bad")
else:
    reject = scenario == "reject-once" and mode == "review" and not task.get("reviews")
    reject = reject or (scenario == "global-fix" and mode == "global" and not any(x["task_id"].startswith("repair-") for x in task["approved_tasks"]))
    result = {"decision": "changes_requested" if reject else "approve", "snapshot": request["snapshot"],
              "summary": "Correction needed" if reject else "Reviewed source and evidence",
              "findings": ([{"task_id": task["id"] if mode == "review" else "T1",
                             "file": "value.txt", "detail": "Inspect and correct the fixture"}] if reject else []),
              "acceptance": [{"criterion": c, "passed": not reject, "evidence": "value.txt; unit exit 0"}
                             for c in task["acceptance"]]}
    if scenario == "tamper-review" and mode == "review":
        Path("value.txt").write_text("tampered")
    if scenario == "stale-review" and mode == "review":
        result["snapshot"] = "wrong"
    if scenario == "false-approve" and mode == "review":
        result["acceptance"][0]["passed"] = False
if scenario == "invalid-output":
    result = {"unrecognized": True}
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(result))
print(json.dumps({"type": "thread.started", "thread_id": "fake-test-thread"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                                       "output_tokens": 10, "reasoning_output_tokens": 4}}))
