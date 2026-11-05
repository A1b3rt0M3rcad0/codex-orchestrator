"""Persistent controller: direct routing, task worktrees, bounded reviews and gates."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .budget import Ledger
from .contracts import (OrchestratorError, ROLES, REVIEWER, canonical, digest, in_scope,
                        overlaps, read_json, safe_id, validate, CHILD_SCHEMA, validate_plan,
                        validate_review, validate_task)
from .evidence import (approval_payload, artifact, capsule_fresh, compact_capsule, contract,
                       handoff, save_capsule, validate_delivery, verify_artifact, verify_checks,
                       verify_human)
from .model import Model
from .quality import probe_mutations
from .policy import assess, guard, require_verification, route, seal
from .system import (argv_expand, atomic_json, changed, command, commit, diff_bytes,
                     enforce_scope, exclusive, file_hash, git, load_config, repo_root,
                     run_process, snapshot, store_root, tail)


def now():
    return datetime.now(timezone.utc).isoformat()

def session_file(store: Path, session_id: str) -> Path:
    return store / "sessions" / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")


def ready_batch(tasks: list[dict], completed: set[str], workers: int, remaining: int) -> list[dict]:
    batch = []
    for task in tasks:
        if task["status"] == "integrated" or not set(task["depends_on"]) <= completed:
            continue
        critical = task["risk"]["critical"]
        if critical:
            if not batch:
                return [task]
            break
        if any(overlaps(task["scope"], t["scope"] + t["reads"]) or overlaps(task["reads"], t["scope"]) for t in batch):
            continue
        batch.append(task)
        if len(batch) >= min(workers, remaining):
            break
    return batch

class Engine:
    def __init__(self, root: Path, state: dict, directory: Path):
        self.root, self.state, self.directory = root, state, directory
        self.work, self.config = Path(state["worktree"]), state["config"]
        self.lock, self.cancel = threading.RLock(), threading.Event()
        self.ledger = Ledger(self.config, state["calls"], self.lock, self.persist)
        self.model = Model(self.config, directory, self.ledger, self.lock, self.persist, self.cancel)

    def persist(self):
        with self.lock:
            self.state["updated_at"] = now()
            atomic_json(self.directory / "state.json", self.state)

    def event(self, event, **details):
        with self.lock:
            self.state["events"].append({"at": now(), "event": event, **details})
            self.persist()

    def save_task(self, task, event=None):
        if task.get("worktree"):
            task["checkpoint"] = snapshot(Path(task["worktree"]))
        path = self.directory / "history" / f"{task['id']}.json"
        atomic_json(path, task.get("history", []))
        task["history_ref"] = str(path)
        with self.lock:
            self.state["tasks"][task["id"]] = copy.deepcopy(task)
            if event:
                self.event(event, task=task["id"], tier=task["tier"])
            else:
                self.persist()

    def workspace(self, label: str, base: str) -> Path:
        label = safe_id(label if len(label) <= 80 else label[:58] + "-" + digest(label)[:20])
        path = Path(self.state["workspace_root"]) / label
        branch = f"orchestrator-task/{self.state['id']}/{label}"
        with self.lock:
            if path.exists():
                raise OrchestratorError(f"Unexpected preexisting task worktree: {path}")
            git(self.root, "worktree", "add", "-b", branch, str(path), base)
        return path

    def check(self, work: Path, names: list[str], label: str) -> tuple[bool, list[dict]]:
        guard(work, self.state["sealed"])
        before = snapshot(work)
        folder = self.directory / "checks" / f"{safe_id(label)}-{uuid.uuid4().hex[:12]}"
        folder.mkdir(parents=True)
        evidence = []
        for index, name in enumerate(dict.fromkeys(names)):
            out, err = folder / f"{index}.stdout", folder / f"{index}.stderr"
            env = {k: v for k, v in os.environ.items()
                   if k not in {"OPENAI_API_KEY", "CODEX_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK"}}
            argv = argv_expand(self.config["checks"][name])
            try:
                rc = run_process(argv, work, out, err, self.config["runtime"]["check_timeout"], env=env,
                                 cancel=self.cancel, max_log_bytes=self.config["runtime"]["max_log_bytes"])
            except subprocess.TimeoutExpired:
                rc = 124
            row = {"name": name, "argv": argv, "exit_code": rc, "stdout": artifact(out),
                   "stderr": artifact(err), "snapshot": before}
            if rc:
                row["failure_tail"] = (tail(out) + tail(err))[-2400:]
            evidence.append(row)
            guard(work, self.state["sealed"])
            if snapshot(work) != before:
                raise OrchestratorError(f"Check {name} changed source/index; use a non-mutating check")
        atomic_json(folder / "evidence.json", evidence)
        self.event("checks_finished", label=label, passed=all(x["exit_code"] == 0 for x in evidence))
        return all(x["exit_code"] == 0 for x in evidence), evidence

    def review(self, phase: str, role: str, task: dict, work: Path, checks: list[dict], **context):
        before = snapshot(work)
        verify_checks(checks, require_pass=False)
        key = digest({"phase": phase, "contract": contract(task), "risk": task.get("risk"),
                      "snapshot": before, "model": self.config["models"][role],
                      "sealed": self.state["sealed"], "context": context,
                      "checks": [{"name": x["name"], "exit_code": x["exit_code"],
                                  "stdout": x["stdout"]["sha256"], "stderr": x["stderr"]["sha256"]} for x in checks]})
        with self.lock:
            cached = self.state["review_cache"].get(key)
        if cached:
            result = read_json(verify_artifact(cached))
            validate_review(result, task["acceptance"], before)
            self.event("review_reused", task=task["id"], phase=phase, capsule=cached)
            return result, cached
        # No implementation summary, transcript or history is shown to the reviewer.
        result, path = self.model.call(phase, role, contract(task), work, task.get("ancestors", [task["id"]]),
                                      snapshot=before, checks=checks, risk=task.get("risk"), **context)
        guard(work, self.state["sealed"])
        if snapshot(work) != before:
            raise OrchestratorError("Reviewer changed source; approval rejected")
        validate_review(result, task["acceptance"], before)
        reference = artifact(path)
        if result["decision"] == "approve":
            with self.lock:
                self.state["review_cache"][key] = reference
                self.persist()
        return result, reference

    def preflight(self):
        if self.state.get("contract_proof"):
            verify_artifact(self.state["contract_proof"])
            return
        evidence = []
        for task in self.state["tasks"].values():
            _, rows = self.check(self.work, task["checks"], f"baseline-{task['id']}")
            expected = {x["check"]: x["baseline"] for x in task["test_contract"]["cases"]}
            for row in rows:
                actual = "pass" if row["exit_code"] == 0 else "fail"
                if actual != expected.get(row["name"], "pass") or row["exit_code"] in {124, 126, 127}:
                    raise OrchestratorError(f"Unexpected baseline for {task['id']}/{row['name']}; inspect the test oracle")
            evidence.extend(rows)
        task = {"id": "contract", "objective": "Validate the independent acceptance-test contracts BEFORE implementation.",
                "acceptance": [f"{t['id']}: test cases faithfully cover every acceptance criterion and explain baseline results."
                               for t in self.state["tasks"].values()], "ancestors": ["contract"]}
        result, ref = self.review("contract", "astra", task, self.work, evidence,
                                  plan=artifact(self.directory / "plan.json"), sealed=self.state["sealed"])
        if result["decision"] != "approve":
            raise OrchestratorError("Test/spec contract was not approved; replan before implementation")
        self.state["contract_proof"] = ref
        self.state["baseline_evidence"] = evidence
        self.event("contract_approved")

    def escalate(self, task, reason):
        index = ROLES.index(task["tier"])
        if index == len(ROLES) - 1:
            raise OrchestratorError(f"{task['id']}: Sol attempt limit reached; Astra/human must replan")
        task["tier"] = ROLES[index + 1]
        task["status"] = "pending"
        task["feedback"] = reason
        self.save_task(task, "escalated")

    def attempt(self, task):
        with self.lock:
            counts = self.state["attempt_totals"].setdefault(task["attempt_key"], {})
            while counts.get(task["tier"], 0) >= self.config["attempts"][task["tier"]]:
                self.escalate(task, "Attempt ceiling reached; inspect prior failures in handoff.")
            counts[task["tier"]] = counts.get(task["tier"], 0) + 1
            task["attempts"] = counts.copy()
            self.persist()

    def history(self, task, outcome, detail="", ref=None):
        task["history"].append({"role": task["tier"], "attempt": task["attempts"].get(task["tier"], 0),
                                "outcome": outcome, "detail": str(detail)[:1600], "artifact": ref})

    def delegate(self, task, children):
        work = Path(task["worktree"])
        if task["tier"] == "luna" or task.get("children") or task.get("repair") or not children or changed(work):
            raise OrchestratorError("Invalid/repeated delegation or delegation after edits")
        prepared = []
        for child in children:
            validate(child, CHILD_SCHEMA)
            if ROLES.index(child["tier"]) >= ROLES.index(task["tier"]):
                raise OrchestratorError("Delegation must target a lower tier")
            if any(not in_scope(p.rstrip("/"), task["scope"]) or (p.endswith("/") and not any(
                    q.endswith("/") and p.startswith(q) for q in task["scope"])) for p in child["scope"]):
                raise OrchestratorError("Child scope exceeds parent")
            if not set(child["acceptance"]) <= set(task["acceptance"]) or not set(child["checks"]) <= set(task["checks"]):
                raise OrchestratorError("Child cannot invent/replace the reviewed acceptance oracle")
            cases = [c for c in task["test_contract"]["cases"] if c["criterion"] in child["acceptance"]]
            if not {c["check"] for c in cases} <= set(child["checks"]):
                raise OrchestratorError("Child omitted required acceptance checks")
            mutations = [m for m in task["test_contract"].get("mutations", [])
                         if in_scope(m["file"], child["scope"]) and m["check"] in child["checks"]]
            child.update(id=safe_id(task["id"] + "." + child["id"]),
                         test_contract={"files": task["test_contract"]["files"], "cases": cases,
                                        **({"mutations": mutations} if mutations else {})},
                         adversarial_checks=task["adversarial_checks"], depends_on=[], parent_id=task["id"])
            validate_task(child, self.config["checks"])
            child = new_task(child, task["ancestors"])
            route(child, assess(child, self.config, work, inherited=(task["risk"]["tags"] if task["risk"]["critical"] else ())))
            if ROLES.index(child["tier"]) >= ROLES.index(task["tier"]):
                raise OrchestratorError("Risk floor forbids lower-tier delegation; implement at the required tier")
            require_verification(child, self.config)
            prepared.append(child)
        with self.lock:
            if len(self.state["tasks"]) + len(prepared) > self.config["runtime"]["max_tasks"]:
                raise OrchestratorError("Task budget exceeded")
            for child in prepared:
                if child["id"] in self.state["tasks"]:
                    raise OrchestratorError("Duplicate child id")
                child["start_base"] = git(work, "rev-parse", "HEAD")
                self.state["tasks"][child["id"]] = child
            task["children"] = [t["id"] for t in prepared]
            self.state["attempt_totals"][task["attempt_key"]][task["tier"]] -= 1
            task["status"] = "delegated"
            self.save_task(task, "delegated")

    def process(self, ident: str) -> dict:
        with self.lock:
            task = copy.deepcopy(self.state["tasks"][ident])
        if task["status"] in {"integrated", "local_approved"}:
            return task
        if not task.get("worktree"):
            task["base"] = task["start_base"]
            task["worktree"] = str(self.workspace("task-" + task["id"], task["base"]))
            self.save_task(task)
        work = Path(task["worktree"])
        guard(work, self.state["sealed"])
        if task["status"] == "implementing":
            task["status"] = "pending"
            task["feedback"] = "Interrupted attempt; inspect the existing diff and recorded attempts."
        try:
            while task["status"] != "local_approved":
                if self.cancel.is_set():
                    raise OrchestratorError("Execution cancelled")
                if task["status"] == "delegated":
                    for child in task["children"]:
                        self.process(child)
                        self.integrate(child, work)
                    task["status"] = "pending"
                    task["feedback"] = "Children approved and integrated. Integrate interactions; do not reimplement them."
                    self.save_task(task)
                if task["status"] == "pending":
                    route(task, assess(task, self.config, work, task["base"]))
                    require_verification(task, self.config)
                    self.attempt(task)
                    task["status"] = "implementing"
                    head = git(work, "rev-parse", "HEAD")
                    task["implementation_head"] = head
                    self.save_task(task, "implementing")
                    result, ref = self.model.call("implement", task["tier"], contract(task), work, task["ancestors"],
                        effective_tier=task["tier"], handoff=handoff(task, self.config["runtime"]["handoff_bytes"]),
                        risk=task["risk"], children=[self.state["tasks"][c].get("proof") for c in task.get("children", [])])
                    guard(work, self.state["sealed"])
                    enforce_scope(work, task["scope"], head)
                    patch = self.directory / "patches" / f"{task['id']}.patch"
                    patch.parent.mkdir(exist_ok=True)
                    patch.write_bytes(command(["git", "diff", "--binary", "--no-ext-diff", task["base"]], work))
                    task["patch_ref"] = str(patch)
                    self.history(task, result["status"], result["summary"], artifact(ref))
                    if route(task, assess(task, self.config, work, task["base"])):
                        require_verification(task, self.config)
                        task["status"] = "pending"
                        task["feedback"] = "Diff triggered a higher mandatory risk floor; reassess the preserved implementation."
                        self.save_task(task, "risk_escalated")
                        continue
                    if result["status"] == "delegate":
                        self.delegate(task, result["children"])
                        continue
                    if result["children"]:
                        raise OrchestratorError("Only delegation may return children")
                    if result["status"] == "blocked":
                        self.escalate(task, result["summary"])
                        continue
                    task["status"] = "testing"
                    self.save_task(task, "implemented")
                if task["status"] == "testing":
                    names = list(dict.fromkeys(task["checks"] + task["adversarial_checks"]))
                    ok, checks = self.check(work, names, task["id"])
                    task["checks_evidence"] = checks
                    if not ok:
                        task["feedback"] = "\n".join(x.get("failure_tail", "") for x in checks if x["exit_code"])
                        self.history(task, "checks_failed", task["feedback"])
                        task["status"] = "pending"
                        self.save_task(task, "tests_failed")
                        continue
                    task["candidate_snapshot"] = snapshot(work)
                    task["mutation_proof"] = probe_mutations(self, task, work)
                    task["status"] = "reviewing"
                    self.save_task(task)
                if task["status"] == "reviewing":
                    if snapshot(work) != task["candidate_snapshot"]:
                        raise OrchestratorError("Candidate changed since checks; refusing stale evidence")
                    reviewer = REVIEWER[task["tier"]]
                    phase = task.get("review_phase", "review")
                    result, ref = self.review(phase, reviewer, task, work, task["checks_evidence"],
                                              diff_base=task["base"], mutation_proof=task.get("mutation_proof"))
                    task.setdefault("reviews", []).append(ref)
                    if result["decision"] == "blocked":
                        raise OrchestratorError(f"Review blocked: {result['summary']}")
                    if result["decision"] == "changes_requested":
                        task["feedback"] = json.dumps(result["findings"], ensure_ascii=False)[:2400]
                        self.history(task, "changes_requested", task["feedback"], ref)
                        task["status"] = "pending"
                        self.save_task(task)
                        continue
                    if task["risk"]["critical"]:
                        challenge, challenge_ref = self.review("adversarial", reviewer, task, work,
                            task["checks_evidence"], diff_base=task["base"], strategy="Falsify the patch using negative and boundary cases; do not seek confirmation.")
                        task.setdefault("challenge_reviews", []).append(challenge_ref)
                        if challenge["decision"] != "approve":
                            if challenge["decision"] == "blocked":
                                raise OrchestratorError("Adversarial review blocked")
                            task["feedback"] = json.dumps(challenge["findings"], ensure_ascii=False)[:2400]
                            self.history(task, "adversarial_rejection", task["feedback"], challenge_ref)
                            task["status"] = "pending"
                            self.save_task(task)
                            continue
                    commit(work, f"orchestrator: {ident} ({task['tier']} reviewed by {reviewer})")
                    guard(work, self.state["sealed"])
                    task["proof"] = save_capsule(self.directory, task, work, result, task["checks_evidence"],
                                                self.config["models"][reviewer], self.state["sealed"], ref, phase)
                    task["status"] = "local_approved"
                    self.save_task(task, "local_approved")
            return task
        except BaseException:
            self.save_task(task, "task_stopped")
            raise

    def integrate(self, ident: str, target: Path):
        task = copy.deepcopy(self.state["tasks"][ident])
        if task["status"] == "integrated":
            return
        if task["status"] != "local_approved":
            raise OrchestratorError("Only locally approved tasks may be integrated")
        source = Path(task["worktree"])
        if not capsule_fresh(task["proof"], task, source, self.state["sealed"]):
            raise OrchestratorError("Local review capsule is stale")
        if changed(target) or changed(source):
            raise OrchestratorError("Integration requires clean source and destination worktrees")
        before = git(target, "rev-parse", "HEAD")
        if before != task["base"]:
            candidate = self.workspace("rebase-" + ident + "-" + uuid.uuid4().hex[:6], before)
            patch = diff_bytes(source, task["base"])
            if patch:
                try:
                    command(["git", "apply", "--check", "--index", "-"], candidate, data=patch)
                    command(["git", "apply", "--index", "-"], candidate, data=patch)
                except OrchestratorError as exc:
                    raise OrchestratorError(f"Integration conflict for {ident}; both task/candidate worktrees preserved: {candidate}") from exc
            guard(candidate, self.state["sealed"])
            enforce_scope(candidate, task["scope"], before)
            task.setdefault("rebases", []).append({"from": task["base"], "to": before, "previous_proof": task["proof"]})
            task.update(base=before, worktree=str(candidate), status="testing", review_phase="integration_review")
            self.save_task(task, "base_changed_revalidation_required")
            self.process(ident)  # checks AND independent review on the new base, before integration
            return self.integrate(ident, target)
        candidate_head = git(source, "rev-parse", "HEAD")
        task["integration_intent"] = {"target": str(target), "before": before, "candidate": candidate_head}
        self.save_task(task, "integration_intent")
        git(target, "merge", "--ff-only", candidate_head)
        guard(target, self.state["sealed"])
        if changed(target) or git(target, "rev-parse", "HEAD") != candidate_head:
            raise OrchestratorError("Integration hooks changed the reviewed candidate")
        task["status"] = "integrated"
        task["integrated_commit"] = candidate_head
        task["integration_target"] = str(target)
        task.pop("integration_intent", None)
        with self.lock:
            if target == self.work:
                self.state["since_checkpoint"] += 1
                self.state["integration_checkpoint"] = snapshot(target)
                if task["risk"]["critical"] or self.state["since_checkpoint"] >= self.config["runtime"]["checkpoint_every"]:
                    self.state["checkpoint_due"] = True
            else:
                for parent in self.state["tasks"].values():
                    if parent.get("worktree") == str(target):
                        parent["checkpoint"] = snapshot(target)
            self.save_task(task, "integrated")

    def repairs(self, review: dict, label: str) -> list[str]:
        targets = sorted({f["task_id"] for f in review["findings"]})
        if not targets or not set(targets) <= set(self.state["tasks"]):
            raise OrchestratorError("Findings require new/global scope; explicit replanning required")
        ids = []
        for index, target in enumerate(targets):
            old = self.state["tasks"][target]
            ident = safe_id(f"repair-{label}-{index + 1}-{uuid.uuid4().hex[:5]}")
            task = {**contract(old), "id": ident, "tier": old["tier"], "risk_tags": old["risk"]["tags"], "depends_on": []}
            task = new_task(task, old["ancestors"])
            task.update(repair=True, repairs=target, attempt_key=old["attempt_key"], risk=old["risk"],
                        feedback=json.dumps([f for f in review["findings"] if f["task_id"] == target], ensure_ascii=False)[:2400],
                        start_base=git(self.work, "rev-parse", "HEAD"))
            with self.lock:
                if len(self.state["tasks"]) >= self.config["runtime"]["max_tasks"]:
                    raise OrchestratorError("Repair task budget exceeded")
                self.state["tasks"][ident] = task
                self.persist()
            ids.append(ident)
        return ids

    def checkpoint(self):
        index = len(self.state["checkpoints"]) + 1
        # Persisted repair counts prevent resume from resetting this budget.
        counter = str(index)
        while True:
            ok, evidence = self.check(self.work, self.state["plan"]["checkpoint_checks"], f"checkpoint-{index}")
            self.state["last_checkpoint_checks"] = evidence
            if ok:
                self.state["checkpoints"].append({"snapshot": snapshot(self.work), "checks": evidence})
                self.state["since_checkpoint"], self.state["checkpoint_due"] = 0, False
                self.event("checkpoint_passed", checkpoint=index)
                return
            count = self.state["checkpoint_repairs"].get(counter, 0)
            if count >= self.config["runtime"]["checkpoint_repairs"]:
                raise OrchestratorError("Checkpoint repair budget exhausted; dependent tasks were not started")
            self.state["checkpoint_repairs"][counter] = count + 1
            task = {"id": "global", "objective": "Diagnose the failed integration checkpoint without expanding scope.",
                    "acceptance": ["Integration checkpoint checks pass and assembled behavior remains correct."],
                    "ancestors": ["global"]}
            result, _ = self.review("checkpoint", "astra", task, self.work, evidence,
                tasks=[{"id": t["id"], "scope": t["scope"]} for t in self.state["tasks"].values() if t["status"] == "integrated"],
                checks_passed=False)
            if result["decision"] != "changes_requested":
                raise OrchestratorError("Failing checkpoint requires actionable findings, not approval")
            for ident in self.repairs(result, f"cp{index}-{count + 1}"):
                self.process(ident)
                self.integrate(ident, self.work)

    def worker(self, ident):
        try:
            return self.process(ident)
        except BaseException:
            self.cancel.set()
            raise

    def dispatch(self):
        while True:
            if self.state["checkpoint_due"]:
                self.checkpoint()
            with self.lock:
                tasks = [copy.deepcopy(t) for t in self.state["tasks"].values() if not t.get("parent_id")]
                done = {t["id"] for t in tasks if t["status"] == "integrated"}
            if len(done) == len(tasks):
                if self.state["since_checkpoint"]:
                    self.checkpoint()
                return
            remaining = max(1, self.config["runtime"]["checkpoint_every"] - self.state["since_checkpoint"])
            batch = ready_batch(tasks, done, self.config["runtime"]["parallel_workers"], remaining)
            if not batch:
                raise OrchestratorError("No runnable task; dependencies unresolved")
            base = git(self.work, "rev-parse", "HEAD")
            with self.lock:
                for task in batch:
                    self.state["tasks"][task["id"]].setdefault("start_base", base)
                self.persist()
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = [executor.submit(self.worker, task["id"]) for task in batch]
                # Resolve in plan order, not completion order. Integrate only after writers stop.
                for future in futures:
                    future.result()
            for task in batch:
                self.integrate(task["id"], self.work)
                if self.state["checkpoint_due"]:
                    self.checkpoint()

    def refresh(self) -> dict | None:
        # Later edits can obsolete earlier capsules even with sequential execution.
        for ident in list(self.state["tasks"]):
            task = copy.deepcopy(self.state["tasks"][ident])
            if capsule_fresh(task["proof"], task, self.work, self.state["sealed"]):
                continue
            ok, checks = self.check(self.work, task["checks"] + task["adversarial_checks"], "refresh-" + ident)
            if ok:
                task["mutation_proof"] = probe_mutations(self, task, self.work)
            result, ref = self.review("refresh", REVIEWER[task["tier"]], task, self.work, checks,
                                      previous_capsule=task["proof"], diff_base=task["base"], checks_passed=ok,
                                      mutation_proof=task.get("mutation_proof"))
            if result["decision"] != "approve":
                return result
            if not ok:
                raise OrchestratorError("Review cannot approve failed refresh checks")
            if task["risk"]["critical"]:
                challenge, cref = self.review("adversarial", REVIEWER[task["tier"]], task, self.work, checks,
                                             diff_base=task["base"], strategy="Falsify the current integrated candidate.")
                if challenge["decision"] != "approve":
                    return challenge
                task.setdefault("challenge_reviews", []).append(cref)
            task["proof"] = save_capsule(self.directory, task, self.work, result, checks,
                self.config["models"][REVIEWER[task["tier"]]], self.state["sealed"], ref, "refresh")
            self.save_task(task, "capsule_refreshed")
        return None

    def execute(self):
        self.state["status"] = "running"
        self.state.pop("error", None)
        self.persist()
        try:
            if self.config["budgets"]["mode"] == "strict":
                raise OrchestratorError("Strict billing cap unavailable in Codex CLI; no inference started")
            if any(r["status"] in {"unknown", "started", "reserved"} for r in self.state["calls"]):
                raise OrchestratorError("Unresolved/in-flight token telemetry; inspect artifacts, do not reset budget")
            self.ledger.check()
            self.preflight()
            while True:
                self.dispatch()
                if self.state["global_round"] >= self.config["runtime"]["max_global_rounds"]:
                    raise OrchestratorError("Global review limit reached; explicit replanning required")
                self.state["global_round"] += 1
                self.persist()
                result = self.refresh()
                if result is not None:
                    if result["decision"] != "changes_requested":
                        raise OrchestratorError("Refresh review blocked")
                    self.repairs(result, f"refresh{self.state['global_round']}")
                    continue
                names = list(self.state["plan"]["integration_checks"])
                for task in self.state["tasks"].values():
                    names.extend(task["checks"] + task["adversarial_checks"])
                ok, evidence = self.check(self.work, names, "global")
                self.state["global_checks"] = evidence
                task = {"id": "global", "objective": self.state["plan"]["objective"],
                        "acceptance": self.state["plan"]["acceptance"], "ancestors": ["global"]}
                capsules = [compact_capsule(t["proof"]) for t in self.state["tasks"].values()]
                manifest = self.directory / "review-manifest.json"
                atomic_json(manifest, capsules)
                result, ref = self.review("global", "astra", task, self.work, evidence,
                    review_manifest=artifact(manifest), diff_base=self.state["base"], checks_passed=ok,
                    reuse_rule="Capsules were validated against current file hashes; focus on interfaces, deltas and end-to-end behavior.")
                self.state.setdefault("global_reviews", []).append(ref)
                if result["decision"] == "approve":
                    if not ok:
                        raise OrchestratorError("Global approval rejected: integration checks failed")
                    self.state.update(global_proof=ref, approved_snapshot=snapshot(self.work),
                                      head=git(self.work, "rev-parse", "HEAD"))
                    self.state["human_required"] = self.config["verification"]["require_human"] == "all" or any(
                        t["risk"]["critical"] for t in self.state["tasks"].values())
                    self.state["status"] = "awaiting_human" if self.state["human_required"] else "approved"
                    validate_delivery(self.state, require_signature=False)
                    if self.state["human_required"]:
                        (self.directory / "approval-request.json").write_bytes(canonical(approval_payload(self.state)))
                    self.event("machine_validation_passed", status=self.state["status"])
                    return self.state
                if result["decision"] == "blocked":
                    raise OrchestratorError("Global validation blocked")
                if self.state["global_round"] >= self.config["runtime"]["max_global_rounds"]:
                    raise OrchestratorError("Global review limit reached; no unvalidated repairs started")
                self.repairs(result, f"global{self.state['global_round']}")
        except BaseException as exc:
            self.cancel.set()
            self.state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "blocked"
            self.state["error"] = str(exc) or type(exc).__name__
            self.state["integration_checkpoint"] = snapshot(self.work)
            self.event("run_stopped", reason=self.state["error"])
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise OrchestratorError(f"Run {self.state['id']} blocked: {exc}. Artifacts: {self.directory}") from exc


def new_task(spec: dict, ancestors=()) -> dict:
    return {**copy.deepcopy(spec), "requested_tier": spec["tier"], "status": "pending", "attempts": {},
            "attempt_key": spec["id"], "ancestors": list(dict.fromkeys([*ancestors, spec["id"]])),
            "history": [], "feedback": ""}


def recover_intents(state: dict):
    for task in state["tasks"].values():
        intent = task.get("integration_intent")
        if not intent:
            continue
        target = Path(intent["target"])
        current = git(target, "rev-parse", "HEAD")
        if changed(target) or current not in {intent["before"], intent["candidate"]}:
            raise OrchestratorError("Ambiguous interrupted integration; worktrees preserved for inspection")
        if current == intent["candidate"]:
            task.update(status="integrated", integrated_commit=current, integration_target=str(target))
            task.pop("integration_intent")
            if str(target) == state["worktree"]:
                state["integration_checkpoint"] = snapshot(target)
                state["checkpoint_due"] = True
                state["since_checkpoint"] += 1
            else:
                for parent in state["tasks"].values():
                    if parent.get("worktree") == str(target):
                        parent["checkpoint"] = snapshot(target)


def run(root: Path, plan_path: Path | None = None, run_id: str | None = None, session_id: str | None = None):
    root = repo_root(root)
    store = store_root(root)
    with exclusive(store / "controller.lock"):
        if run_id:
            directory = store / "runs" / safe_id(run_id)
            state = read_json(directory / "state.json")
            if state.get("version") != 2:
                raise OrchestratorError("v1 runs cannot be resumed with v2; finish using v0.1 or create a reviewed v2 plan")
            if digest(state["plan"]) != state["plan_hash"]:
                raise OrchestratorError("Persisted plan changed")
            if state["status"] in {"approved", "awaiting_human"}:
                validate_delivery(state, require_signature=state["status"] == "approved")
                return state
            recover_intents(state)
            if snapshot(Path(state["worktree"])) != state["integration_checkpoint"]:
                raise OrchestratorError("Integration worktree changed outside controller")
            for task in state["tasks"].values():
                if task.get("worktree") and snapshot(Path(task["worktree"])) != task["checkpoint"]:
                    raise OrchestratorError(f"Task worktree changed outside controller: {task['id']}")
        else:
            cfg = load_config(root)
            plan = read_json(plan_path)
            validate_plan(plan, cfg["checks"])
            if len(plan["tasks"]) > cfg["runtime"]["max_tasks"]:
                raise OrchestratorError("Initial plan exceeds task budget")
            if changed(root):
                raise OrchestratorError("Commit your configuration, independent tests and source before starting a run")
            sealed = seal(root, plan, cfg)
            tasks = {t["id"]: new_task(t) for t in plan["tasks"]}
            for task in tasks.values():
                route(task, assess(task, cfg, root))
                require_verification(task, cfg)
            if (any(t["risk"]["critical"] for t in tasks.values()) or cfg["verification"]["require_human"] == "all") and not shutil.which("openssl"):
                raise OrchestratorError("Critical approval requires OpenSSL 3+ before execution")
            if cfg["verification"]["require_human"] == "all" and not cfg["verification"]["trusted_signers"]:
                raise OrchestratorError("Human approval policy requires trusted_signers")
            ident = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            directory = store / "runs" / ident
            directory.mkdir(parents=True)
            location = Path(cfg["runtime"]["worktree_root"]).expanduser() if cfg["runtime"]["worktree_root"] else root.parent / ".codex-orchestrator-worktrees"
            if not location.is_absolute():
                location = root.parent / location
            location = location.resolve()
            if location.is_relative_to(root) or location.is_relative_to(store.parent):
                raise OrchestratorError("worktree_root must be outside project and Git metadata")
            location = location / hashlib.sha256(str(store).encode()).hexdigest()[:12] / ident
            location.mkdir(parents=True)
            base, branch = git(root, "rev-parse", "HEAD"), "orchestrator/" + ident
            work = location / "integration"
            git(root, "worktree", "add", "-b", branch, str(work), base)
            state = {"version": 2, "id": ident, "root": str(root), "worktree": str(work), "workspace_root": str(location),
                     "branch": branch, "base": base, "status": "pending", "created_at": now(), "config": cfg,
                     "plan": plan, "plan_hash": digest(plan), "sealed": sealed, "tasks": tasks,
                     "calls": [], "events": [], "review_cache": {}, "attempt_totals": {}, "global_round": 0,
                     "checkpoints": [], "checkpoint_repairs": {}, "since_checkpoint": 0, "checkpoint_due": False,
                     "integration_checkpoint": snapshot(work), "approval_nonce": uuid.uuid4().hex,
                     "signers_hash": file_hash(Path(cfg["verification"]["trusted_signers"])) if cfg["verification"]["trusted_signers"] else None}
            atomic_json(directory / "plan.json", plan)
        if session_id:
            if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
                raise OrchestratorError("Invalid session id")
            atomic_json(session_file(store, session_id), {"run_id": state["id"]})
        return Engine(root, state, directory).execute()


def gate(root: Path, run_id: str) -> dict:
    state = read_json(store_root(repo_root(root)) / "runs" / safe_id(run_id) / "state.json")
    if state["status"] != "approved":
        raise OrchestratorError(f"Run {run_id} is {state['status']}, not approved")
    validate_delivery(state)
    return {"run_id": run_id, "approved": True, "branch": state["branch"], "head": state["head"]}


def approve(root: Path, run_id: str, statement: Path, signature: Path, identity: str) -> dict:
    if os.environ.get("CO_MANAGED") == "1":
        raise OrchestratorError("Managed model processes cannot grant human approval")
    root = repo_root(root)
    with exclusive(store_root(root) / "controller.lock"):
        directory = store_root(root) / "runs" / safe_id(run_id)
        state = read_json(directory / "state.json")
        if state["status"] != "awaiting_human":
            raise OrchestratorError("Run is not awaiting human approval")
        validate_delivery(state, require_signature=False)
        verify_human(state, statement, signature, identity)
        # Preserve signed bytes for future gate verification, without a private key.
        saved, sig = directory / "human-approval.json", directory / "human-approval.sig"
        saved.write_bytes(statement.read_bytes())
        sig.write_bytes(signature.read_bytes())
        state["human_approval"] = verify_human(state, saved, sig, identity)
        state["status"] = "approved"
        atomic_json(directory / "state.json", state)
        return gate(root, run_id)


def report(state: dict) -> dict:
    totals = {}
    for call in state["calls"]:
        key = f"{call['role']}/{call['mode']}"
        row = totals.setdefault(key, {"calls": 0, "usage_missing": 0, "tokens": {}, "cost_usd": "0"})
        row["calls"] += 1
        if not call.get("usage_complete"):
            row["usage_missing"] += 1
        if call.get("usage"):
            for name, count in call["usage"].items():
                row["tokens"][name] = row["tokens"].get(name, 0) + count
        if call.get("cost_usd") is None:
            row["cost_usd"] = None
        elif row["cost_usd"] is not None:
            from decimal import Decimal
            row["cost_usd"] = str(Decimal(row["cost_usd"]) + Decimal(call["cost_usd"]))
    return {"run_id": state["id"], "status": state["status"], "error": state.get("error"),
            "branch": state["branch"], "worktree": state["worktree"],
            "tasks": {k: {"status": t["status"], "tier": t["tier"], "risk": t["risk"]["tags"],
                          "worktree": t.get("worktree")} for k, t in state["tasks"].items()},
            "checkpoints_passed": len(state["checkpoints"]), "usage_by_role_and_stage": totals,
            "budget_mode": "observed: admission ceilings, not guaranteed provider billing caps"}
