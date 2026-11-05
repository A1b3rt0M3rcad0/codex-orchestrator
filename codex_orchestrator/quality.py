"""Independent, deterministic acceptance-oracle sensitivity probes.

The implementation's acceptance tests must reject deliberately mutated behavior.
Mutants are authored in the pre-approved spec, never by the executing worker.
Every probe runs in its own temporary Git worktree; production sources stay intact.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .contracts import OrchestratorError, digest
from .evidence import artifact, verify_artifact
from .policy import guard
from .system import atomic_json, changed, command, git, snapshot


def probe_mutations(engine, task: dict, work: Path) -> dict | None:
    mutations = task["test_contract"].get("mutations", [])
    if not mutations:
        return None
    original = snapshot(work)
    base = git(work, "rev-parse", "HEAD")
    records = []
    for index, mutation in enumerate(mutations):
        with tempfile.TemporaryDirectory(prefix="co-mutant-") as dirname:
            checkout = Path(dirname) / "checkout"
            command(["git", "worktree", "add", "--detach", str(checkout), base], engine.root)
            try:
                # A candidate may not be committed yet. Copy *only* its tracked and
                # untracked diff into the throwaway worktree, never across the tree.
                for name in changed(work):
                    source, target = work / name, checkout / name
                    if source.is_symlink() or not source.resolve().is_relative_to(work.resolve()):
                        raise OrchestratorError("Unsafe mutation candidate source")
                    if source.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                    elif not source.exists() and target.exists():
                        target.unlink()
                    else:
                        raise OrchestratorError("Unsupported mutation candidate file")
                path = checkout / mutation["file"]
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(checkout.resolve()):
                    raise OrchestratorError("Mutation target is missing, symlinked or escapes worktree")
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeError, OSError) as exc:
                    raise OrchestratorError("Mutation target must be a UTF-8 source file") from exc
                if mutation.get("strategy") == "file_replacement":
                    # Pre-approved bad behavior is independent of the generated text.
                    mutant_text = mutation["content"]
                else:
                    if text.count(mutation["find"]) != 1:
                        raise OrchestratorError(f"Mutation {index} for {task['id']} does not match exactly once")
                    mutant_text = text.replace(mutation["find"], mutation["replace"], 1)
                path.write_text(mutant_text, encoding="utf-8")
                if path.read_bytes() == (work / mutation["file"]).read_bytes():
                    raise OrchestratorError("Mutation did not change behavior candidate")
                ok, checks = engine.check(checkout, [mutation["check"]], f"mutation-{task['id']}-{index}")
                row = checks[0]
                if ok or row["exit_code"] in (124, 126, 127):
                    raise OrchestratorError(f"Mutation {index} for {task['id']} survived or check could not run")
                stdout = verify_artifact(row["stdout"]).read_bytes()
                stderr = verify_artifact(row["stderr"]).read_bytes()
                marker = mutation["failure_marker"].encode("utf-8")
                if marker not in stdout and marker not in stderr:
                    raise OrchestratorError(f"Mutation {index} for {task['id']} failed for an unexpected reason")
                records.append({"mutation_index": index, "spec_sha256": digest(mutation),
                                "source_file": mutation["file"], "check": mutation["check"],
                                "exit_code": row["exit_code"], "stdout": row["stdout"],
                                "stderr": row["stderr"], "candidate_snapshot": original})
            finally:
                # Git manages the linked-worktree metadata. Do not leave the
                # temporary checkout registered even after failed tests.
                command(["git", "worktree", "remove", "--force", str(checkout)], engine.root)
        guard(work, engine.state["sealed"])
        if snapshot(work) != original:
            raise OrchestratorError("Mutation probe changed original candidate")
    path = engine.directory / "quality" / f"{task['id']}-{hashlib.sha256(original.encode()).hexdigest()[:16]}.json"
    atomic_json(path, {"version": 1, "task_id": task["id"], "candidate_snapshot": original,
                       "spec_sha256": digest(mutations), "mutants_killed": len(records),
                       "mutants_total": len(mutations), "cases": records})
    return artifact(path)


def verify_mutations(reference: dict, task: dict, candidate_snapshot: str) -> None:
    declared = task["test_contract"].get("mutations", [])
    if not declared:
        if reference is not None:
            raise OrchestratorError("Unexpected mutation evidence")
        return
    if reference is None:
        raise OrchestratorError("Missing independent mutation evidence")
    from .contracts import read_json
    payload = read_json(verify_artifact(reference))
    if (payload.get("version") != 1 or payload.get("task_id") != task["id"] or
        payload.get("candidate_snapshot") != candidate_snapshot or
        payload.get("spec_sha256") != digest(declared) or
        payload.get("mutants_killed") != len(declared) or
        payload.get("mutants_total") != len(declared) or len(payload.get("cases", [])) != len(declared)):
        raise OrchestratorError("Stale or incomplete mutation evidence")
    for i, case in enumerate(payload["cases"]):
        if (case.get("mutation_index") != i or case.get("spec_sha256") != digest(declared[i]) or
            case.get("candidate_snapshot") != candidate_snapshot or case.get("exit_code") in (0, 124, 126, 127)):
            raise OrchestratorError("Mutation evidence mismatch")
        output = verify_artifact(case["stdout"]).read_bytes() + verify_artifact(case["stderr"]).read_bytes()
        if declared[i]["failure_marker"].encode("utf-8") not in output:
            raise OrchestratorError("Mutation failure marker missing from evidence")
