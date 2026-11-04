"""Cheap lifecycle gates. Hooks NEVER call a model or read transcripts."""
from __future__ import annotations

import os
from pathlib import Path

from .contracts import OrchestratorError, read_json
from .runtime import gate, session_file
from .system import repo_root, store_root


def handle(payload: dict) -> dict:
    if os.environ.get("CO_MANAGED") == "1":
        return {}  # controller owns stage validation; no child/global deadlock
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "Stop"):
        return {}
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 256:
        return {"systemMessage": "codex-orchestrator: missing/invalid session id; no run enrolled."}
    root = repo_root(Path(payload.get("cwd", ".")))
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                f"codex-orchestrator available. This session id is {session}. When using the skill, "
                "pass --session-id with this id to enroll only this session. Unrelated conversations are not gated. "
                "A failed or interrupted run is not an approved delivery."}}
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
    if state["status"] in ("blocked", "interrupted"):
        return {"systemMessage": f"Run {ident} is {state['status']}, NOT approved. Report the blocker; "
                "do not claim completion or retry models indefinitely."}
    reason = (f"Run {ident} is incomplete. Inspect codex-orchestrator status {ident}; "
              "wait for the already-running command or report the blocker. Do not start a duplicate run.")
    if payload.get("stop_hook_active"):
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"decision": "block", "reason": reason}
