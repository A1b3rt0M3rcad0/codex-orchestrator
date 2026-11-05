"""Evidence-only experiment report for repeated, identical-spec model configurations.

No invented quality scores. A passing local gate measures process compliance,
not absence of defects. Single trials are explicitly inconclusive.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from math import isfinite
from .config import money
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


def direct_baseline_report(path: Path, states: list[dict]) -> dict:
    """Validate externally collected direct-Sol trials; never pretend to attest them."""
    data = read_json(path)
    if (set(data) != {"version", "baseline_commit", "plan_sha256", "trials", "source"} or
        data["version"] != 1 or data["baseline_commit"] != states[0]["base"] or
        data["plan_sha256"] != states[0]["plan_hash"] or
        data["source"] != "direct-sol" or not isinstance(data["trials"], list) or
        len(data["trials"]) < 2):
        raise OrchestratorError("Direct baseline must contain >=2 direct-sol trials for the SAME plan and commit")
    successful, total_tokens, total_seconds, total_cost, missing = 0, 0, 0.0, Decimal(0), 0
    for trial in data["trials"]:
        if (not isinstance(trial, dict) or
            set(trial) != {"approved", "tokens", "seconds", "cost_usd", "evidence"} or
            type(trial["approved"]) is not bool or type(trial["tokens"]) is not int or
            trial["tokens"] < 0 or type(trial["seconds"]) not in {float, int} or
            not isfinite(trial["seconds"]) or trial["seconds"] < 0 or
            not isinstance(trial["evidence"], str) or not trial["evidence"].strip()):
            raise OrchestratorError("Malformed direct-Sol trial; require observed metrics and an evidence reference")
        successful += int(trial["approved"])
        total_tokens += trial["tokens"]
        total_seconds += trial["seconds"]
        if trial["cost_usd"] is None:
            missing += 1
        else:
            if not isinstance(trial["cost_usd"], str):
                raise OrchestratorError("Direct baseline monetary values must be decimal strings or null")
            total_cost += money(trial["cost_usd"])
    cost_per_delivery = (total_cost / successful if successful and not missing else None)
    return {"source": data["source"], "provenance": "external self-reported; not verified by controller",
            "trials": len(data["trials"]), "approvals_reported": successful,
            "approval_rate_reported": successful / len(data["trials"]),
            "reported_tokens": total_tokens, "reported_seconds": round(total_seconds, 3),
            "reported_cost_usd": str(total_cost) if not missing else None,
            "missing_cost_trials": missing,
            "cost_per_reported_delivery_usd": str(cost_per_delivery) if cost_per_delivery is not None else None}


def benchmark(root: Path, identifiers: list[str], direct_baseline: Path | None = None) -> dict:
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
        successes, cost_total = 0, Decimal(0)
        tokens, failed_checks, review_changes = 0, 0, 0
        unresolved_cost = 0
        mutation_killed, mutation_total = 0, 0
        phase_tokens, phase_calls = defaultdict(int), defaultdict(int)
        phase_cost: dict[str, Decimal] = defaultdict(Decimal)
        phase_missing_cost = defaultdict(int)
        call_seconds, wall_seconds = 0.0, 0.0
        for state in runs:
            try:
                wall_seconds += max(0., (datetime.fromisoformat(state["updated_at"]) -
                                         datetime.fromisoformat(state["created_at"])).total_seconds())
            except (KeyError, ValueError):
                pass
            if state["status"] == "approved":
                try:
                    gate(root, state["id"])
                    successes += 1
                except (OrchestratorError, OSError, KeyError, ValueError):
                    pass
            for call in state["calls"]:
                phase = "implementation" if call["mode"] == "implement" else "verification"
                phase_calls[phase] += 1
                call_seconds += float(call.get("seconds", 0))
                usage = call.get("usage")
                if usage:
                    consumed = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    tokens += consumed
                    phase_tokens[phase] += consumed
                if call.get("cost_usd") is None:
                    unresolved_cost += 1
                    phase_missing_cost[phase] += 1
                else:
                    observed = Decimal(call["cost_usd"])
                    cost_total += observed
                    phase_cost[phase] += observed
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
        transport = runs[0]["config"]["runtime"]["command"]
        results.append({"variant": key, "transport": transport,
                        "simulated_backend_detected": any("fake_codex" in x or "codex-test" in x for x in transport),
                        "remote_model_identity": "not cryptographically attested",
                        "models": {role: v["model"] for role, v in runs[0]["config"]["models"].items()},
                        "efforts": {role: [v["effort"], v["review_effort"]] for role, v in runs[0]["config"]["models"].items()},
                        "run_ids": [s["id"] for s in runs], "trials": len(runs),
                        "verified_approvals": successes, "verified_approval_rate": round(successes / len(runs), 4),
                        "wilson95": [round(low, 4), round(high, 4)],
                        "reported_tokens": tokens, "reported_cost_usd": str(cost_total) if not unresolved_cost else None,
                        "reported_wall_seconds": round(wall_seconds, 3),
                        "reported_model_seconds": round(call_seconds, 3),
                        "phase_metrics": {phase: {"calls": phase_calls[phase], "tokens": phase_tokens[phase],
                            "reported_cost_usd": str(phase_cost[phase]) if not phase_missing_cost[phase] else None}
                            for phase in ("implementation", "verification")},
                        "verification_token_share": round(phase_tokens["verification"] / tokens, 4) if tokens else None,
                        "verification_cost_share": (round(float(phase_cost["verification"] / cost_total), 4)
                            if cost_total and not unresolved_cost else None),
                        "missing_cost_calls": unresolved_cost, "review_changes_requested": review_changes,
                        "failed_checks": failed_checks, "mutant_kills": mutation_killed,
                        "mutants_declared": mutation_total,
                        "mutation_sensitivity": round(mutation_killed / mutation_total, 4) if mutation_total else None,
                        "cost_per_verified_delivery_usd": str(cost_total / successes) if successes and not unresolved_cost else None,
                        "readiness": "exploratory: at least 10 independent trials required" if len(runs) < 10 else "eligible for statistical review"})
    external = direct_baseline_report(direct_baseline, states) if direct_baseline else None
    for variant in results:
        variant["economic_advantage"] = "not demonstrated: comparable direct baseline and independent quality review required"
        variant["relative_cost_per_delivery"] = None
        if (external and external["cost_per_reported_delivery_usd"] is not None and
            variant["cost_per_verified_delivery_usd"] is not None):
            direct_cost = Decimal(external["cost_per_reported_delivery_usd"])
            if direct_cost > 0:
                variant["relative_cost_per_delivery"] = round(
                    float(Decimal(variant["cost_per_verified_delivery_usd"]) / direct_cost), 4)
    return {"baseline_commit": states[0]["base"], "plan_sha256": states[0]["plan_hash"],
            "direct_baseline": external,
            "variants": results, "caveats": [
                "Acceptance is a tested workflow outcome, not a universal software-quality score",
                "A 95% Wilson interval only quantifies sampling uncertainty of observed approvals",
                "All compared runs must use independently verified tests and the same starting code/spec",
                "Token/cost values are observed, not a provider billing guarantee",
                "A direct baseline file is self-reported; attest trial evidence and evaluate defects externally",
                "Verification overhead is measured separately from implementation, not assumed to be a saving",
                "Transport/model family is declared in config; independent remote identity is not proven by local state"]}
