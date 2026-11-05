"""Conservative installation and explicit v1 configuration migration."""
from __future__ import annotations
import hashlib
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path
from .contracts import DEFAULT_MODELS, OrchestratorError
from .system import atomic_json, exclusive, repo_root, store_root

ASSETS = Path(__file__).parent / "assets"
AGENTS_BLOCK = """<!-- BEGIN codex-orchestrator -->
When `$codex-orchestrator` is requested, read its SKILL.md and use the controller.
Prepare independent acceptance tests before implementation. Do not bypass risk
floors, sealed tests, budgets, checkpoints, independent review or human signatures.
Managed workers must not recursively launch controllers. The original checkout
must remain unchanged. Report completion only when `codex-orchestrator gate` passes.
Use applicable existing project skills and standards; never replace them.
<!-- END codex-orchestrator -->"""


def default_config() -> str:
    text = '# v2: explicit policy and bounded work. Configure real checks before use.\nversion = 2\n\n'
    for role, settings in DEFAULT_MODELS.items():
        text += f"[models.{role}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in settings.items()) + "\n"
    return text + '''[runtime]
command = ["codex"]
parallel_workers = 1
checkpoint_every = 2
checkpoint_repairs = 1
max_calls = 80
max_tasks = 64
max_global_rounds = 2
model_timeout = 900
check_timeout = 300

[attempts]
luna = 2
terra = 2
sol = 2

[budgets]
mode = "observed" # CLI telemetry boundaries, NOT a hard provider invoice cap
run_tokens = 2000000
task_tokens = 500000
call_tokens = 100000
task_calls = 24
# run_cost_usd = "10.00" # requires [prices.ROLE] for all four roles
# task_cost_usd = "3.00"

[policy]
minimum_tier = "luna"
extra_patterns = [] # additive critical path globs; built-in rules cannot be removed

[verification]
require_human = "critical" # or "all"; a model cannot approve this gate
mutation_policy = "critical" # critical (default), all, optional
trusted_signers = "" # absolute JSON Ed25519 public-key allowlist outside the repository

[checks]
# REQUIRED: configure real checks for YOUR project; do not invent passing commands.
# unit = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
# smoke = ["npm", "run", "test:integration"]
# e2e = ["npm", "run", "test:e2e"]
# adversarial = ["npm", "run", "test:negative"]
'''


def local_path(root: Path, name: str) -> Path:
    path = root / name
    for item in [path, *path.parents]:
        if item == root:
            break
        if item.is_symlink():
            raise OrchestratorError(f"Refusing symlinked installation path: {name}")
    if not path.resolve().is_relative_to(root):
        raise OrchestratorError(f"Refusing escaped installation path: {name}")
    return path


def install(target: Path) -> list[str]:
    if os.name == "nt":
        raise OrchestratorError("Native Windows hook installation is not validated; use WSL")
    root = repo_root(target)
    with exclusive(store_root(root) / "controller.lock"):
        manifest = local_path(root, ".codex/orchestrator-install.json")
        previous = json.loads(manifest.read_text()) if manifest.exists() else {"managed": {}}
        writes = {}
        for source, destination in (("SKILL.md", ".agents/skills/codex-orchestrator/SKILL.md"),
                                    ("WORKFLOW.md", ".agents/skills/codex-orchestrator/references/workflow.md")):
            writes[destination] = (ASSETS / source).read_text(encoding="utf-8")
        for name, content in writes.items():
            path = local_path(root, name)
            if path.exists() and path.read_text(encoding="utf-8") != content:
                if previous.get("managed", {}).get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise OrchestratorError(f"Managed file was edited: {name}")
        path = local_path(root, ".codex/config.toml")
        config_text = path.read_text(encoding="utf-8") if path.exists() else ""
        parsed = tomllib.loads(config_text)
        if "codex-orchestrator" not in parsed.get("profiles", {}):
            config_text += '\n# BEGIN codex-orchestrator\n[profiles.codex-orchestrator]\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n# END codex-orchestrator\n'
        writes[".codex/config.toml"] = config_text
        path = local_path(root, ".codex/hooks.json")
        hooks = json.loads(path.read_text()) if path.exists() else {"hooks": {}}
        if not isinstance(hooks, dict) or not isinstance(hooks.get("hooks"), dict):
            raise OrchestratorError("Unsupported existing hooks.json")
        command = shlex.join([sys.executable, "-m", "codex_orchestrator", "hooks"])
        for event in ("SessionStart", "Stop", "PostModelRequest"):
            groups = hooks["hooks"].setdefault(event, [])
            if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list) for g in groups):
                raise OrchestratorError(f"Invalid existing hook groups: {event}")
            marker = f"codex-orchestrator/{event}"
            for group in groups:
                if any(not isinstance(h, dict) for h in group["hooks"]):
                    raise OrchestratorError("Invalid existing hook handler")
                group["hooks"] = [h for h in group["hooks"] if h.get("statusMessage") != marker]
            groups[:] = [g for g in groups if g["hooks"]]
            handler = {"type": "command", "command": command, "timeout": 15, "statusMessage": marker}
            if event == "SessionStart":
                handler["additionalContextLimit"] = 300
            groups.append({"hooks": [handler]})
        writes[".codex/hooks.json"] = json.dumps(hooks, indent=2, ensure_ascii=False) + "\n"
        path = local_path(root, "AGENTS.md")
        agents = path.read_text() if path.exists() else ""
        if "<!-- BEGIN codex-orchestrator -->" not in agents:
            agents += "\n\n" + AGENTS_BLOCK + "\n"
        writes["AGENTS.md"] = agents
        path = local_path(root, "orchestrator.toml")
        if not path.exists():
            writes["orchestrator.toml"] = default_config()
        elif tomllib.loads(path.read_text()).get("version") == 1:
            writes[".codex/orchestrator-v2.example.toml"] = default_config()
        for name in writes:
            local_path(root, name)
        changed = []
        for name, content in writes.items():
            path = root / name
            if path.exists() and path.read_text(encoding="utf-8") == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed.append(name)
        managed = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in writes.items() if name.startswith(".agents/")}
        atomic_json(manifest, {"version": 2, "managed": managed})
        return changed


def migrate_config(target: Path) -> str:
    root = repo_root(target)
    with exclusive(store_root(root) / "controller.lock"):
        path = local_path(root, "orchestrator.toml")
        original = path.read_text()
        old = tomllib.loads(original)
        if old.get("version") == 2:
            return "already-v2"
        if old.get("version") != 1 or set(old) - {"version", "models", "checks", "runtime"}:
            raise OrchestratorError("Unsupported v1 configuration; migrate explicitly")
        runtime = old.get("runtime", {}).copy()
        attempts = runtime.pop("attempts_per_tier", 2)
        allowed = {"command", "worktree_root", "max_calls", "max_tasks", "max_global_rounds", "model_timeout", "check_timeout"}
        if set(runtime) - allowed:
            raise OrchestratorError("Unrecognized v1 runtime fields")
        backup = local_path(root, "orchestrator.toml.v1.bak")
        if backup.exists() and backup.read_text() != original:
            raise OrchestratorError("Migration backup already exists with different contents")
        text = "# Migrated from v1; original preserved in orchestrator.toml.v1.bak\nversion = 2\n"
        for section, values in [("runtime", runtime), ("attempts", {x: attempts for x in ("luna", "terra", "sol")}),
                                ("checks", old.get("checks", {}))]:
            text += f"\n[{section}]\n" + "".join(f"{json.dumps(k)} = {json.dumps(v)}\n" for k, v in values.items())
        for role, settings in old.get("models", {}).items():
            if role not in DEFAULT_MODELS or set(settings) - {"model", "effort", "review_effort"}:
                raise OrchestratorError("Unrecognized v1 model fields")
            text += f"\n[models.{role}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in settings.items())
        tomllib.loads(text)
        backup.write_text(original)
        path.write_text(text)
        return str(backup)
