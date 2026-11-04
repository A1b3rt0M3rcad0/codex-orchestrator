"""Conservative, idempotent installation into an existing Git project."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path

from .contracts import DEFAULT_MODELS, OrchestratorError
from .system import atomic_json, repo_root

ASSETS = Path(__file__).parent / "assets"
START, END = "# BEGIN codex-orchestrator", "# END codex-orchestrator"
AGENTS_BLOCK = """<!-- BEGIN codex-orchestrator -->
When `$codex-orchestrator` is requested, read its SKILL.md and use the deterministic
controller. Do not simulate approvals, launch recursive controllers, or bypass
failed checks. Implementation runs in an isolated worktree; the user's checkout
must remain unchanged. Only report success after `codex-orchestrator gate` passes.
Use existing project skills/standards where applicable; never replace them.
<!-- END codex-orchestrator -->"""


def default_config() -> str:
    text = '# Explicit models, bounded work and real project checks.\nversion = 1\n\n'
    for role, settings in DEFAULT_MODELS.items():
        text += f"[models.{role}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in settings.items()) + "\n"
    text += '''[runtime]
command = ["codex"]
# worktree_root = "/writable/location/outside/the/project"
attempts_per_tier = 2
max_calls = 40
max_tasks = 64
max_global_rounds = 2
model_timeout = 900
check_timeout = 300

[checks]
# REQUIRED: configure commands that validate YOUR project; no fake success checks.
# unit = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
# typecheck = ["npm", "run", "typecheck"]
# integration = ["npm", "run", "test:integration"]
'''
    return text


def install(target: Path) -> list[str]:
    if os.name == "nt":
        raise OrchestratorError("Native Windows hook installation is not validated. Install/run inside WSL.")
    root = repo_root(target)
    manifest = root / ".codex/orchestrator-install.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else {"managed": {}}
    writes: dict[str, str] = {}
    for file, destination in (("SKILL.md", ".agents/skills/codex-orchestrator/SKILL.md"),
                              ("WORKFLOW.md", ".agents/skills/codex-orchestrator/references/workflow.md")):
        writes[destination] = (ASSETS / file).read_text(encoding="utf-8")
    for role in ("luna", "terra", "sol", "astra"):
        modes = ("implement", "review") if role in ("terra", "sol") else (("review",) if role == "astra" else ("implement",))
        for mode in modes:
            name = f"co-{role}" if mode == "implement" else f"co-review-{role}"
            settings = DEFAULT_MODELS[role]
            instruction = (f"Act as the {role} {mode} role. Read the supplied contract and applicable AGENTS.md. "
                           "Native helper results are advisory, not controller approvals. "
                           "Do not launch codex-orchestrator or other Codex processes. "
                           + ("Do not edit files; report evidence and actionable findings."
                              if mode == "review" else "Only edit the assigned scope and report tests truthfully."))
            writes[f".codex/agents/{name}.toml"] = (
                f'name = "{name}"\ndescription = "{role.title()} {mode} helper; managed runs use the controller."\n'
                f'model = "{settings["model"]}"\nmodel_reasoning_effort = "{settings["effort" if mode == "implement" else "review_effort"]}"\n'
                f"developer_instructions = {json.dumps(instruction)}\n")
    # Preflight managed files before changing any existing user file.
    for name, content in writes.items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise OrchestratorError(f"Refusing symlinked installation path: {name}")
        if path.exists() and path.read_text(encoding="utf-8") != content:
            old = previous.get("managed", {}).get(name)
            if old != hashlib.sha256(path.read_bytes()).hexdigest():
                raise OrchestratorError(f"Managed file was edited or already belongs to the project: {name}")
    config_path = root / ".codex/config.toml"
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    parsed = tomllib.loads(config_text)
    if "codex-orchestrator" not in parsed.get("profiles", {}):
        config_text += (f'\n{START}\n[profiles.codex-orchestrator]\nmodel = "gpt-6-astra"\n'
                        f'model_reasoning_effort = "max"\n{END}\n')
    tomllib.loads(config_text)
    writes[".codex/config.toml"] = config_text
    hooks_path = root / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {"hooks": {}}
    if not isinstance(hooks, dict) or not isinstance(hooks.get("hooks"), dict):
        raise OrchestratorError("Existing hooks.json has an unsupported structure")
    command = shlex.join([sys.executable, "-m", "codex_orchestrator", "hooks"])
    for event in ("SessionStart", "Stop"):
        groups = hooks["hooks"].setdefault(event, [])
        if not isinstance(groups, list):
            raise OrchestratorError(f"Invalid existing hook groups: {event}")
        owned = [g for g in groups if any(h.get("statusMessage") == f"codex-orchestrator/{event}"
                                         for h in g.get("hooks", []))]
        handler = {"type": "command", "command": command, "timeout": 15,
                   "statusMessage": f"codex-orchestrator/{event}"}
        if event == "SessionStart":
            handler["additionalContextLimit"] = 300
        replacement = {"hooks": [handler]}
        if owned:
            # Replace only our handler, preserving any co-located user handlers.
            for group in owned:
                group["hooks"] = [h for h in group["hooks"]
                                  if h.get("statusMessage") != f"codex-orchestrator/{event}"]
            groups[:] = [g for g in groups if g.get("hooks")]
        groups.append(replacement)
    writes[".codex/hooks.json"] = json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"
    agents_path = root / "AGENTS.md"
    agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    if "<!-- BEGIN codex-orchestrator -->" not in agents:
        agents += "\n\n" + AGENTS_BLOCK + "\n"
    writes["AGENTS.md"] = agents
    if not (root / "orchestrator.toml").exists():
        writes["orchestrator.toml"] = default_config()
    # Catch symlinks in all destination ancestors, including pre-existing config.
    for name in writes:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise OrchestratorError(f"Refusing escaped installation path: {name}")
    changed = []
    for name, content in writes.items():
        path = root / name
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        changed.append(name)
    managed = {name: hashlib.sha256(content.encode()).hexdigest()
               for name, content in writes.items() if name.startswith((".agents/", ".codex/agents/"))}
    atomic_json(manifest, {"version": 1, "managed": managed})
    return changed
