"""Evidence-only experiment report for repeated, identical-spec model configurations.

No invented quality scores. A passing local gate measures process compliance,
not absence of defects. Single trials are explicitly inconclusive.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from math import sqrt
from pathlib import Path

from .contracts import OrchestratorError, digest, read_json, safe_id
from .runtime import gate
from .system import repo_root, store_root


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p = passed / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    radius = z * sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return max(0, mid - radius), min(1, mid + radius)


def benchmark(root: Path, identifiers: list[str]) -> dict:
    root = repo_root(root)
    if len(identifiers) < 2 or len(identifiers) != len(set(identifiers)):
        raise OrchestratorError("Benchmark needs at least two distinct runs")
    states = [read_json(store_root(root) / "runs" / safe_id(ident) / "state.json") for ident in identifiers]
    if len({(s["plan_hash"], s["base"]) for s in states}) != 1:
        raise OrchestratorError("Comparisons require the SAME plan hash and baseline commit")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for state in states:
        setting = {role: {"model": cfg["model"], "effort": cfg["effort"],
                          "review_effort": cfg["review_effort"], "family": cfg["family"]}
                   for role, cfg in state["config"]["models"].items()}
        key = digest(setting)
        grouped[key].append(state)
    results = []
    for key, runs in sorted(grouped.items()):
        successes, cost_total, priced_successes = 0, Decimal(0), 0
        accepted_cost, tokens, failed_checks, review_changes = Decimal(0), 0, 0, 0
        unresolved_cost = 0
        mutation_killed, mutation_total = 0, 0
        for state in runs:
            if state["status"] == "approved":
                try:
                    gate(root, state["id"])
                    successes += 1
                except (OrchestratorError, OSError, KeyError, ValueError):
                    pass
            for call in state["calls"]:
                usage = call.get("usage")
                if usage:
                    tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                if call.get("cost_usd") is None:
                    unresolved_cost += 1
                else:
                    cost_total += Decimal(call["cost_usd"])
            for task in state["tasks"].values():
                review_changes += sum(row.get("outcome") in {"changes_requested", "adversarial_rejection"}
                                      for row in task.get("history", []))
                failed_checks += sum(row.get("outcome") == "checks_failed" for row in task.get("history", []))
                muts = task.get("test_contract", {}).get("mutations", [])
                mutation_total += len(muts)
                if task.get("mutation_proof"):
                    try:
                        from .evidence import verify_artifact
                        proof = read_json(verify_artifact(task["mutation_proof"]))
                        if proof.get("candidate_snapshot") == task.get("candidate_snapshot"):
                            mutation_killed += proof["mutants_killed"]
                    except (OrchestratorError, OSError, ValueError, KeyError):
                        pass
        low, high = wilson(successes, len(runs))
        results.append({"variant": key, "models": {role: v["model"] for role, v in runs[0]["config"]["models"].items()},
                        "efforts": {role: [v["effort"], v["review_effort"]] for role, v in runs[0]["config"]["models"].items()},
                        "run_ids": [s["id"] for s in runs], "trials": len(runs),
                        "verified_approvals": successes, "verified_approval_rate": round(successes / len(runs), 4),
                        "wilson95": [round(low, 4), round(high, 4)],
                        "reported_tokens": tokens, "reported_cost_usd": str(cost_total) if not unresolved_cost else None,
                        "missing_cost_calls": unresolved_cost, "review_changes_requested": review_changes,
                        "failed_checks": failed_checks, "mutant_kills": mutation_killed,
                        "mutants_declared": mutation_total,
                        "mutation_sensitivity": round(mutation_killed / mutation_total, 4) if mutation_total else None,
                        "cost_per_verified_delivery_usd": str(cost_total / successes) if successes and not unresolved_cost else None,
                        "readiness": "exploratory: at least 10 independent trials required" if len(runs) < 10 else "eligible for statistical review"})
    return {"baseline_commit": states[0]["base"], "plan_sha256": states[0]["plan_hash"],
            "variants": results, "caveats": [
                "Acceptance is a tested workflow outcome, not a universal software-quality score",
                "A 95% Wilson interval only quantifies sampling uncertainty of observed approvals",
                "All compared runs must use independently verified tests and the same starting code/spec",
                "Token/cost values are observed, not a provider billing guarantee",
                "Transport/model family is declared in config; independent remote identity is not proven by local state"]}
