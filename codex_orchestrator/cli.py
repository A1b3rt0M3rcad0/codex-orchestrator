"""CLI: structured reports, explicit approval and no implicit fallback models."""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path
from . import __version__
from .contracts import OrchestratorError, canonical, read_json, safe_id, validate_plan
from .evidence import approval_payload, validate_delivery
from .hooks import handle
from .install import install, migrate_config
from .policy import assess, require_verification, route, seal
from .runtime import approve, gate, report, run
from .smoke import smoke_models
from .benchmark import benchmark
from .system import argv_expand, command, load_config, repo_root, store_root


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="codex-orchestrator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "doctor", "list", "hooks", "migrate-config"):
        subs.add_parser(name)
    validate = subs.add_parser("validate")
    validate.add_argument("plan", type=Path)
    start = subs.add_parser("run")
    start.add_argument("plan", type=Path)
    start.add_argument("--session-id")
    for name in ("resume", "status", "gate", "approval-request"):
        sub = subs.add_parser(name)
        sub.add_argument("run_id")
        if name == "resume":
            sub.add_argument("--session-id")
    bench = subs.add_parser("benchmark", help="Evidence-only comparison of identical-spec runs")
    bench.add_argument("run_ids", nargs="+", help="At least two persisted run IDs")
    live = subs.add_parser("smoke-models", help="Explicit paid, read-only model/schema access check")
    live.add_argument("--acknowledge-paid-requests", action="store_true")
    live.add_argument("--roles", nargs="+", choices=["luna", "terra", "sol", "astra"],
                      default=["luna", "terra", "sol", "astra"])
    live.add_argument("--timeout", type=int, default=120)
    human = subs.add_parser("approve")
    human.add_argument("run_id")
    human.add_argument("--statement", type=Path, required=True)
    human.add_argument("--signature", type=Path, required=True)
    human.add_argument("--identity", required=True)
    args = parser.parse_args(argv)
    status = 0
    try:
        if args.command == "hooks":
            text = sys.stdin.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise OrchestratorError("Hook payload too large")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise OrchestratorError("Hook payload must be an object")
            result = handle(payload)
        elif args.command == "install":
            result = {"changed": install(args.repo), "next": "Configure checks/signers, prepare v2 plan and independent tests, trust /hooks, commit setup."}
        elif args.command == "migrate-config":
            result = {"backup": migrate_config(args.repo), "next": "Review new policy, then prepare a v2 test contract; plans are not guessed automatically."}
        else:
            root = repo_root(args.repo)
            if args.command == "benchmark":
                result = benchmark(root, args.run_ids)
            elif args.command == "smoke-models":
                result = smoke_models(root, load_config(root), args.roles,
                                      args.acknowledge_paid_requests, args.timeout)
            elif args.command == "doctor":
                cfg = load_config(root)
                binary = argv_expand(cfg["runtime"]["command"])
                version = command(binary + ["--version"], root).decode().strip()
                help_text = command(binary + ["exec", "--help"], root).decode()
                missing = [flag for flag in ("--json", "--ephemeral", "--sandbox", "--output-schema", "--output-last-message") if flag not in help_text]
                if missing:
                    raise OrchestratorError(f"Installed Codex lacks required flags: {missing}")
                result = {"codex": version, "models": cfg["models"], "checks": list(cfg["checks"]),
                          "openssl": bool(shutil.which("openssl")), "budget_mode": cfg["budgets"]["mode"],
                          "warnings": ["No inference: model availability/authentication NOT verified.",
                                       "Trust hooks with /hooks; hook telemetry is optional, not a request-blocking primitive.",
                                       "Observed CLI budgets are not absolute provider invoice caps."]}
            elif args.command == "validate":
                cfg, plan = load_config(root), read_json(args.plan)
                validate_plan(plan, cfg["checks"])
                seal(root, plan, cfg)
                tasks = []
                for task in plan["tasks"]:
                    route(task, assess(task, cfg, root))
                    require_verification(task, cfg)
                    tasks.append({"id": task["id"], "tier": task["tier"], "risk": task["risk"]})
                result = {"valid": True, "tasks": tasks}
            elif args.command in {"run", "resume"}:
                state = run(root, plan_path=args.plan if args.command == "run" else None,
                            run_id=args.run_id if args.command == "resume" else None, session_id=args.session_id)
                result, status = report(state), 0 if state["status"] == "approved" else 2
            elif args.command == "gate":
                result = gate(root, args.run_id)
            elif args.command == "approve":
                result = approve(root, args.run_id, args.statement, args.signature, args.identity)
            elif args.command in {"status", "approval-request"}:
                state = read_json(store_root(root) / "runs" / safe_id(args.run_id) / "state.json")
                if args.command == "approval-request":
                    if state["status"] != "awaiting_human":
                        raise OrchestratorError("Run is not awaiting human approval")
                    validate_delivery(state, require_signature=False)
                    sys.stdout.write(canonical(approval_payload(state)).decode())
                    return 0
                result = report(state)
            else:
                result = [{k: state.get(k) for k in ("id", "status", "branch", "error")}
                          for path in sorted((store_root(root) / "runs").glob("*/state.json"))
                          for state in [read_json(path)]]
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return status
    except (OrchestratorError, OSError, ValueError, KeyError, TypeError) as exc:
        if args.command == "hooks":
            print(json.dumps({"continue": False, "stopReason": "Orchestrator hook could not verify state.",
                              "systemMessage": f"codex-orchestrator: {exc}; no approval granted."}))
            return 0
        print(f"codex-orchestrator: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("codex-orchestrator: interrupted; inspect list/status before resume", file=sys.stderr)
        return 130
