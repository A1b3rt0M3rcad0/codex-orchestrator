"""Compact handoffs, snapshot-bound review capsules and externally signed human attestations."""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path
from .contracts import OrchestratorError, canonical, digest, in_scope, read_json, validate_review
from .policy import guard
from .system import atomic_json, command, file_hash, git, snapshot, tracked

SPEC_KEYS = ("id", "objective", "scope", "reads", "acceptance", "checks", "test_contract", "adversarial_checks")

def contract(task: dict) -> dict:
    return {key: task[key] for key in SPEC_KEYS if key in task}

def handoff(task: dict, byte_limit: int) -> dict:
    """Factual state only. No model transcripts or private reasoning are propagated."""
    history = [{k: row[k] for k in ("role", "attempt", "outcome", "detail", "artifact") if k in row}
               for row in task.get("history", [])[-6:]]
    for row in history:
        if "detail" in row:
            row["detail"] = str(row["detail"])[:600]
    result = {"task_id": task["id"], "attempts": task.get("attempts", {}),
              "last_attempts": history, "unresolved": task.get("feedback", "")[:1600],
              "patch_base": task.get("base"), "current_patch": task.get("patch_ref"),
              "history_ref": task.get("history_ref"), "instructions": "Inspect preserved diff; do not repeat failed attempts blindly."}
    while len(canonical(result)) > byte_limit and result["last_attempts"]:
        result["last_attempts"].pop(0)
    while len(canonical(result)) > byte_limit and result["unresolved"]:
        result["unresolved"] = result["unresolved"][:len(result["unresolved"]) // 2]
    if len(canonical(result)) > byte_limit:
        raise OrchestratorError("Handoff exceeds configured byte limit")
    return result

def file_map(work: Path, paths: list[str]) -> dict:
    names = set(p for p in tracked(work) if in_scope(p, paths))
    names.update(p for p in paths if not p.endswith("/"))
    result = {}
    for name in sorted(names):
        path = work / name
        if path.is_symlink() or not path.resolve().is_relative_to(work.resolve()):
            raise OrchestratorError("Symlink in evidence scope")
        result[name] = {"sha256": file_hash(path), "mode": path.stat().st_mode & 0o777} if path.is_file() else None
    return result

def artifact(path: Path) -> dict:
    return {"path": str(path), "sha256": file_hash(path)}

def verify_artifact(reference: dict):
    path = Path(reference["path"])
    if not path.is_file() or path.is_symlink() or file_hash(path) != reference["sha256"]:
        raise OrchestratorError(f"Evidence artifact changed or missing: {path}")
    return path

def check_facts(checks: list[dict]) -> list[dict]:
    return [{k: row[k] for k in ("name", "argv", "exit_code", "snapshot", "stdout", "stderr")} for row in checks]

def verify_checks(checks: list[dict], require_pass=True) -> None:
    if not checks:
        raise OrchestratorError("Missing check evidence")
    for row in checks:
        if require_pass and row["exit_code"] != 0:
            raise OrchestratorError("Failed check is not approval evidence")
        verify_artifact(row["stdout"])
        verify_artifact(row["stderr"])

def save_capsule(directory: Path, task: dict, work: Path, review: dict, checks: list[dict],
                 model: dict, manifest: dict, response_ref: dict, phase="review") -> dict:
    verify_checks(checks)
    capsule = {"version": 1, "task_id": task["id"], "phase": phase,
               "spec_sha256": digest(contract(task)), "oracle_sha256": digest(manifest),
               "base": task["base"], "commit": git(work, "rev-parse", "HEAD"),
               "tree": git(work, "rev-parse", "HEAD^{tree}"), "reviewed_snapshot": review["snapshot"],
               "scope": task["scope"], "reads": task["reads"],
               "files": file_map(work, task["scope"] + task["reads"]), "risk": task["risk"],
               "model": model, "review": review, "checks": checks, "response": response_ref,
               "adversarial": task.get("challenge_reviews", [])[-1:],
               "mutation_proof": task.get("mutation_proof"),
               "independence": "isolated_context_not_statistical_independence"}
    path = directory / "capsules" / f"{task['id']}-{digest(capsule)[:20]}.json"
    atomic_json(path, capsule)
    return artifact(path)

def capsule_fresh(reference: dict, task: dict, work: Path, manifest: dict) -> bool:
    capsule = read_json(verify_artifact(reference))
    verify_checks(capsule["checks"])
    response = read_json(verify_artifact(capsule["response"]))
    if response != capsule["review"]:
        raise OrchestratorError("Capsule differs from its actual review response")
    validate_review(response, task["acceptance"], capsule["reviewed_snapshot"])
    if response["decision"] != "approve" or any(c["snapshot"] != response["snapshot"] for c in capsule["checks"]):
        raise OrchestratorError("Capsule checks/review are not bound to the same approved snapshot")
    if task["risk"]["critical"] and not capsule.get("adversarial"):
        raise OrchestratorError("Missing independent adversarial review evidence")
    for ref in capsule.get("adversarial", []):
        challenge = read_json(verify_artifact(ref))
        validate_review(challenge, task["acceptance"], capsule["reviewed_snapshot"])
        if challenge["decision"] != "approve":
            raise OrchestratorError("Adversarial review did not approve this snapshot")
    from .quality import verify_mutations
    verify_mutations(capsule.get("mutation_proof"), task, capsule["reviewed_snapshot"])
    return (capsule["spec_sha256"] == digest(contract(task)) and
            capsule["oracle_sha256"] == digest(manifest) and capsule["risk"] == task["risk"] and
            capsule["files"] == file_map(work, task["scope"] + task["reads"]))

def compact_capsule(reference: dict) -> dict:
    capsule = read_json(verify_artifact(reference))
    return {"task_id": capsule["task_id"], "summary": capsule["review"]["summary"][:500],
            "acceptance": capsule["review"]["acceptance"], "risk": capsule["risk"]["tags"],
            "commit": capsule["commit"], "spec_sha256": capsule["spec_sha256"], "artifact": reference}

def approval_payload(state: dict) -> dict:
    return {"version": 1, "namespace": "codex-orchestrator", "decision": "approve",
            "run_id": state["id"], "snapshot": state["approved_snapshot"], "plan_sha256": state["plan_hash"],
            "evidence_sha256": digest({"global": state["global_proof"],
                "tasks": {k: t["proof"] for k, t in sorted(state["tasks"].items())},
                "checks": state["global_checks"]}),
            "config_sha256": digest(state["config"]), "nonce": state["approval_nonce"]}

def verify_human(state: dict, statement: Path, signature: Path, identity: str) -> dict:
    if not identity or len(identity) > 256 or "\n" in identity:
        raise OrchestratorError("Invalid human signer identity")
    signers = Path(state["config"]["verification"]["trusted_signers"])
    if not signers.is_file() or file_hash(signers) != state.get("signers_hash"):
        raise OrchestratorError("Trusted signer configuration changed or missing")
    if statement.stat().st_size > 65536 or signature.stat().st_size > 65536:
        raise OrchestratorError("Oversized attestation")
    data = statement.read_bytes()
    if data != canonical(approval_payload(state)):
        raise OrchestratorError("Human attestation is stale or belongs to another run/evidence snapshot")
    allowlist = read_json(signers)
    if (not isinstance(allowlist, dict) or set(allowlist) != {"version", "identities"}
        or allowlist["version"] != 1 or not isinstance(allowlist["identities"], dict)):
        raise OrchestratorError("Invalid trusted signer allowlist")
    public = allowlist["identities"].get(identity)
    if (not isinstance(public, str) or len(public) > 8192 or
        not public.startswith("-----BEGIN PUBLIC KEY-----") or "PRIVATE KEY" in public):
        raise OrchestratorError("Signer identity has no authorized public key")
    # Only public data is materialized; the controller never reads a private key.
    with tempfile.TemporaryDirectory(prefix="co-verify-") as temp:
        key = Path(temp) / "public.pem"
        key.write_text(public, encoding="ascii")
        der = command(["openssl", "pkey", "-pubin", "-in", str(key), "-outform", "DER"], Path(temp))
        # RFC 8410 SubjectPublicKeyInfo prefix for an Ed25519 key (32-byte key).
        if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b6570032100")):
            raise OrchestratorError("Human approval requires an Ed25519 public key")
        command(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key),
                 "-rawin", "-in", str(statement.resolve()), "-sigfile", str(signature.resolve())], Path(temp))
    return {"identity": identity, "statement": artifact(statement), "signature": artifact(signature)}

def validate_delivery(state: dict, require_signature=True) -> None:
    work = Path(state["worktree"])
    if snapshot(work) != state.get("approved_snapshot"):
        raise OrchestratorError("Approved snapshot is stale; source changed after validation")
    if git(work, "rev-parse", "HEAD") != state.get("head"):
        raise OrchestratorError("Approved HEAD differs from delivery record")
    if digest(state["plan"]) != state["plan_hash"]:
        raise OrchestratorError("Plan changed after approval")
    guard(work, state["sealed"])
    verify_checks(state["global_checks"])
    if any(c["snapshot"] != state["approved_snapshot"] for c in state["global_checks"]):
        raise OrchestratorError("Global checks belong to another snapshot")
    contract_review = read_json(verify_artifact(state["contract_proof"]))
    criteria = [f"{t['id']}: test cases faithfully cover every acceptance criterion and explain baseline results."
                for t in state["plan"]["tasks"]]
    baseline = state["baseline_evidence"]
    verify_checks(baseline, require_pass=False)
    if len({r["snapshot"] for r in baseline}) != 1:
        raise OrchestratorError("Baseline evidence contains inconsistent snapshots")
    validate_review(contract_review, criteria, baseline[0]["snapshot"])
    if contract_review["decision"] != "approve":
        raise OrchestratorError("Independent test contract was not approved")
    global_proof = read_json(verify_artifact(state["global_proof"]))
    validate_review(global_proof, state["plan"]["acceptance"], state["approved_snapshot"])
    if global_proof["decision"] != "approve" or global_proof["snapshot"] != state["approved_snapshot"]:
        raise OrchestratorError("Global approval does not match delivery")
    for task in state["tasks"].values():
        if task["status"] != "integrated" or not capsule_fresh(task["proof"], task, work, state["sealed"]):
            raise OrchestratorError(f"Stale or incomplete task proof: {task['id']}")
    if require_signature and state["human_required"]:
        attestation = state.get("human_approval")
        if not attestation:
            raise OrchestratorError("Independent human approval is required")
        verify_human(state, verify_artifact(attestation["statement"]),
                     verify_artifact(attestation["signature"]), attestation["identity"])
