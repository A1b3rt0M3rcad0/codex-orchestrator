"""Explicit opt-in, read-only Codex model availability/structured-output smoke check.

A successful smoke confirms only that the configured transport returned valid
structured answers with token telemetry at this moment. It does NOT test coding
quality, approval hooks or provide a spending guarantee.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .budget import validate_usage, tokens
from .contracts import OrchestratorError, REVIEW_SCHEMA, read_json, validate, validate_review, transport_schema
from .system import argv_expand, atomic_json, run_process, snapshot


def smoke_models(root: Path, config: dict, roles: list[str], acknowledge: bool,
                 timeout: int = 120, *, allow_simulated_backend: bool = False) -> dict:
    if not acknowledge:
        raise OrchestratorError("Smoke makes paid Codex requests; explicitly acknowledge paid model calls")
    if not 1 <= timeout <= 900:
        raise OrchestratorError("Smoke timeout must be between 1 and 900 seconds")
    if not roles or len(roles) != len(set(roles)) or any(r not in config["models"] for r in roles):
        raise OrchestratorError("Choose distinct configured model roles")
    simulated = any("fake_codex" in item or "codex-test" in item
                    for item in config["runtime"]["command"])
    if simulated and not allow_simulated_backend:
        raise OrchestratorError("Authenticated smoke refuses simulated/fake Codex backend")
    before = snapshot(root)
    checks = []
    with tempfile.TemporaryDirectory(prefix="co-live-smoke-") as folder:
        directory = Path(folder)
        for role in roles:
            stamp = directory / role
            stamp.mkdir()
            schema_file = stamp / "schema.json"
            result_file = stamp / "result.json"
            events_file = stamp / "events.jsonl"
            atomic_json(schema_file, transport_schema(REVIEW_SCHEMA))
            request = {"instructions": "MODEL ACCESS CHECK ONLY. No tools, no file reads, no edits. Return a structured approval for the given acceptance and snapshot.",
                       "mode": "smoke", "role": role,
                       "task": {"id": "smoke", "acceptance": ["Return a valid structured review object"]},
                       "worktree": str(root), "snapshot": "smoke"}
            prompt = json.dumps(request)
            settings = config["models"][role]
            argv = argv_expand(config["runtime"]["command"]) + [
                "exec", "--json", "--ephemeral", "--sandbox", "read-only", "-C", str(root),
                "-m", settings["model"], "-c", f'model_reasoning_effort={json.dumps(settings["review_effort"])}',
                "-c", 'approval_policy="never"', "-c", "agents.enabled=false",
                "--output-schema", str(schema_file), "-o", str(result_file), "-"]
            start = time.monotonic()
            env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"}}
            try:
                rc = run_process(argv, root, events_file, stamp / "stderr.log", timeout, prompt,
                                 env, max_log_bytes=1024 * 1024)
            except subprocess.TimeoutExpired as exc:
                raise OrchestratorError(f"Model {role} timed out during smoke") from exc
            if rc != 0:
                raise OrchestratorError(f"Model {role} failed live smoke (exit {rc}); check authentication/model access")
            result = read_json(result_file)
            validate_review(result, request["task"]["acceptance"], "smoke")
            if result["decision"] != "approve":
                raise OrchestratorError(f"Model {role} did not produce a valid approved smoke response")
            usage = {}
            count = 0
            with events_file.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "turn.completed":
                        u = validate_usage(event.get("usage"))
                        for key, value in u.items():
                            usage[key] = usage.get(key, 0) + value
                        count += 1
            if count < 1:
                raise OrchestratorError(f"Model {role} smoke missing authoritative token telemetry")
            checks.append({"role": role, "model": settings["model"], "valid": True,
                           "usage": usage, "total_tokens": tokens(usage),
                           "seconds": round(time.monotonic() - start, 3)})
            if snapshot(root) != before:
                raise OrchestratorError("Read-only smoke changed repository; results rejected")
    return {"passed": len(checks) == len(roles), "roles": checks,
            "transport": config["runtime"]["command"], "simulated_backend": simulated,
            "authenticated_remote_identity": "not independently attested by CLI",
            "claims": ["Structured-output/availability smoke only; not an end-to-end execution or quality benchmark",
                       "Transport identity is not cryptographically proven by the smoke",
                       "Provider costs may exceed local token observations on interrupted/in-flight calls"]}
