"""Monotonic risk floors and immutable acceptance-test manifests."""
from __future__ import annotations
import fnmatch
import hashlib
import re
from pathlib import Path
from .contracts import OrchestratorError, ROLES, digest, in_scope, safe_path
from .system import changed, command, file_hash, tracked

PATTERNS = {
    "auth": r"\b(auth|authn|authz|authentication|authorization|authenticate\w*|authorize\w*|oauth\w*|jwt|tenant\w*|password\w*|permission\w*|rbac|acl|login|senha|autentica\w*|autoriz\w*)\b",
    "payments": r"\b(pay|payment\w*|payout\w*|payroll|payer\w*|payee\w*|billing|invoice\w*|stripe|pagamento\w*|cobran[cç]a\w*|pix|refund\w*)\b",
    "migrations": r"\b(migrat\w*|migra[cç]\w*|alembic|drop\s+table|alter\s+table|schema\s+change)\b",
    "concurrency": r"\b(concurr\w*|concorr\w*|thread\w*|mutex\w*|semaphore\w*|deadlock\w*|race\s+condition|asyncio|locking)\b",
    "secrets": r"\b(secret\w*|credential\w*|private[_ -]?key|api[_ -]?key|cryptograph\w*|encryption)\b",
    "infrastructure": r"\b(terraform|kubernetes|dockerfile|deployment|iam|firewall|production\s+config)\b",
}

def assess(task: dict, config: dict, work: Path | None = None, base: str | None = None,
           inherited=()) -> dict:
    tags = set(inherited) | set(task.get("risk_tags", [])) | set(task.get("risk", {}).get("tags", []))
    text = "\n".join([task["objective"], *task["scope"], *task.get("reads", [])]).replace("_", " ").replace("/", " ")
    reasons = []
    scanned = list(task["scope"] + task.get("reads", []))
    if work is not None:
        files = sorted(set(tracked(work)) | set(changed(work)))
        size = 0
        for name in files:
            if not in_scope(name, task["scope"] + task.get("reads", [])):
                continue
            scanned.append(name)
            text += "\n" + name.replace("_", " ").replace("/", " ")
            path = work / name
            if path.is_symlink() or not path.resolve().is_relative_to(work.resolve()):
                raise OrchestratorError(f"Symlink in risk scan: {name}")
            if not path.is_file():
                continue
            size += path.stat().st_size
            if size > 2 * 1024 * 1024:
                tags.add("large_or_opaque_change")
                break
            data = path.read_bytes()
            if b"\0" in data:
                tags.add("large_or_opaque_change")
            text += "\n" + data.decode("utf-8", errors="replace")
        if base:
            # Include removed sensitive behavior, not only the new file contents.
            data = command(["git", "diff", "--no-ext-diff", "--no-renames", base, "--", *task["scope"]], work)
            if len(data) > 2 * 1024 * 1024:
                tags.add("large_or_opaque_change")
            text += "\n" + data[:2 * 1024 * 1024].decode("utf-8", errors="replace")
    for tag, pattern in PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            tags.add(tag)
            reasons.append(f"built-in:{tag}")
    for pattern in config["policy"]["extra_patterns"]:
        if any(fnmatch.fnmatchcase(p.lower(), pattern.lower()) for p in scanned):
            tags.add("project_policy")
            reasons.append(f"project:{pattern}")
    minimum = "sol" if tags else config["policy"]["minimum_tier"]
    result = {"critical": bool(tags), "tags": sorted(tags), "minimum_tier": minimum,
              "reasons": sorted(set(reasons) | {f"declared/inherited:{t}" for t in tags})}
    result["digest"] = digest(result)
    return result

def route(task: dict, risk: dict) -> bool:
    old = task["tier"]
    task["risk"] = risk
    task["tier"] = ROLES[max(ROLES.index(old), ROLES.index(risk["minimum_tier"]))]
    return old != task["tier"]

def seal(root: Path, plan: dict, config: dict) -> dict:
    available = set(tracked(root))
    files = {p for t in plan["tasks"] for p in t["test_contract"]["files"]}
    for name in available:
        if (name == "orchestrator.toml" or Path(name).name.lower() in {"agents.md", ".gitignore", ".gitattributes"}
            or name.startswith((".codex/", ".agents/"))):
            files.add(name)
    # Explicit script arguments to a configured check are part of its oracle.
    for argv in config["checks"].values():
        files.update(x for x in argv if x in available and Path(x).suffix in {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb"})
    result = {}
    for name in sorted(files):
        safe_path(name, protected=False)
        path = root / name
        if name not in available or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise OrchestratorError(f"Acceptance/config file must be committed, regular and in-repository: {name}")
        if any(in_scope(name, t["scope"]) for t in plan["tasks"]):
            raise OrchestratorError(f"Protected test/config file overlaps implementation scope: {name}")
        result[name] = {"sha256": file_hash(path), "mode": path.stat().st_mode & 0o777}
    return result

def guard(root: Path, manifest: dict) -> None:
    for name, expected in manifest.items():
        path = root / name
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or
            file_hash(path) != expected["sha256"] or path.stat().st_mode & 0o777 != expected["mode"]):
            raise OrchestratorError(f"Protected acceptance/config file changed: {name}")

def require_verification(task: dict, config: dict) -> None:
    if task["risk"]["critical"]:
        if not task.get("adversarial_checks"):
            raise OrchestratorError(f"{task['id']}: critical task requires independent adversarial_checks")
        if not config["verification"]["trusted_signers"]:
            raise OrchestratorError("Critical task requires trusted_signers for the external human approval gate")
    policy = config["verification"]["mutation_policy"]
    if (policy == "all" or (policy == "critical" and task["risk"]["critical"])) and not task["test_contract"].get("mutations"):
        raise OrchestratorError(f"{task['id']}: risk policy requires pre-approved mutation tests")
