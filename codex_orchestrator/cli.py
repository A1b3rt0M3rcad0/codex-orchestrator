"""Command-line interface; JSON stdout, actionable failures on stderr."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .contracts import OrchestratorError, read_json, safe_id, validate_plan
from .hooks import handle
from .install import install
from .runtime import gate, report, run
from .system import argv_expand, command, load_config, repo_root, store_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-orchestrator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="target Git repository")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("install", help="install skill/hooks without replacing project conventions")
    subs.add_parser("doctor", help="check local configuration/CLI flags, without inference")
    val = subs.add_parser("validate", help="validate a plan without running models")
    val.add_argument("plan", type=Path)
    start = subs.add_parser("run", help="start a new run in an isolated worktree")
    start.add_argument("plan", type=Path)
    start.add_argument("--session-id")
    for name in ("resume", "status", "gate"):
        sub = subs.add_parser(name)
        sub.add_argument("run_id")
        if name == "resume":
            sub.add_argument("--session-id")
    subs.add_parser("list", help="list persisted runs, including interrupted ones")
    subs.add_parser("hooks", help="internal: consume one Codex hook JSON object")
    args = parser.parse_args(argv)
    try:
        if args.command == "hooks":
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise OrchestratorError("Hook payload too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise OrchestratorError("Hook payload must be an object")
            result = handle(payload)
        elif args.command == "install":
            result = {"changed": install(args.repo), "next": "Configure checks; review /hooks; commit setup before run."}
        elif args.command == "doctor":
            root = repo_root(args.repo)
            cfg = load_config(root)
            binary = argv_expand(cfg["runtime"]["command"])
            version = command(binary + ["--version"], root).decode().strip()
            help_text = command(binary + ["exec", "--help"], root).decode()
            missing = [f for f in ("--json", "--ephemeral", "--sandbox", "--output-schema", "--output-last-message")
                       if f not in help_text]
            if missing:
                raise OrchestratorError(f"Installed Codex lacks required flags: {missing}")
            result = {"codex": version, "checks": list(cfg["checks"]), "models": cfg["models"],
                      "warnings": ["No inference performed: authentication/model access NOT verified.",
                                   "Review and trust project hooks with /hooks; doctor cannot grant trust."]}
        elif args.command == "validate":
            cfg = load_config(repo_root(args.repo))
            plan = read_json(args.plan)
            validate_plan(plan, cfg["checks"])
            result = {"valid": True, "tasks": len(plan["tasks"])}
        elif args.command in ("run", "resume"):
            state = run(args.repo, plan_path=args.plan if args.command == "run" else None,
                        run_id=args.run_id if args.command == "resume" else None, session_id=args.session_id)
            result = report(state)
        elif args.command == "gate":
            result = gate(args.repo, args.run_id)
        elif args.command == "status":
            result = report(read_json(store_root(repo_root(args.repo)) / "runs" / safe_id(args.run_id) / "state.json"))
        else:
            folder = store_root(repo_root(args.repo)) / "runs"
            result = [{k: state.get(k) for k in ("id", "status", "branch", "error")}
                      for file in sorted(folder.glob("*/state.json")) for state in [read_json(file)]]
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OrchestratorError, OSError, ValueError, KeyError, TypeError) as exc:
        if args.command == "hooks":
            # Valid explicit stop output, not a malformed/failed hook that might be ignored.
            print(json.dumps({"continue": False, "stopReason": "Orchestrator hook could not verify state.",
                              "systemMessage": f"codex-orchestrator: {exc}; no approval was granted."}))
            return 0
        print(f"codex-orchestrator: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("codex-orchestrator: interrupted; use list/status before resume", file=sys.stderr)
        return 130
