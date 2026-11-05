"""Lifecycle gates and optional request telemetry. Hooks never call a model."""
from __future__ import annotations
import json
import os
from pathlib import Path
from .contracts import OrchestratorError, read_json
from .runtime import gate, session_file
from .system import repo_root, store_root


def handle(payload: dict) -> dict:
    event = payload.get("hook_event_name")
    if os.environ.get("CO_MANAGED") == "1":
        if event == "PostModelRequest" and os.environ.get("CO_USAGE_FILE"):
            target = Path(os.environ["CO_USAGE_FILE"])
            if (not target.is_absolute() or target.name != "request-usage.jsonl" or target.is_symlink()
                or not target.parent.is_dir()):
                raise OrchestratorError("Invalid managed telemetry destination")
            entry = {"type": "request.usage", "status": payload.get("status"),
                     "usage": payload.get("token_usage"), "model": payload.get("model")}
            data = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
            if len(data) > 8192:
                raise OrchestratorError("Oversized request usage")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        return {}  # the controller, not Stop hooks, controls managed stages
    if event not in {"SessionStart", "Stop"}:
        return {}
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 256:
        return {"systemMessage": "codex-orchestrator: invalid/missing session id; no run enrolled."}
    root = repo_root(Path(payload.get("cwd", ".")))
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
            f"codex-orchestrator v2 available. Session id: {session}. Pass --session-id to enroll only this session. "
            "Respect sealed test contracts, risk floors, budgets and signed human gates. A blocked run is not approval."}}
    binding = session_file(store_root(root), session)
    if not binding.exists():
        return {}
    ident = read_json(binding)["run_id"]
    state = read_json(store_root(root) / "runs" / ident / "state.json")
    if state["status"] == "approved":
        try:
            gate(root, ident)
            return {}
        except OrchestratorError as exc:
            return {"continue": False, "stopReason": str(exc), "systemMessage": str(exc)}
    if state["status"] in {"blocked", "interrupted", "awaiting_human"}:
        return {"systemMessage": f"Run {ident} is {state['status']}, NOT approved. Report the blocker or signature requirement; do not retry indefinitely."}
    reason = f"Run {ident} is incomplete. Inspect status, do not launch a duplicate controller or claim completion."
    if payload.get("stop_hook_active"):
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"decision": "block", "reason": reason}
