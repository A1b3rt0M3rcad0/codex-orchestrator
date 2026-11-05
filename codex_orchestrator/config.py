"""Conservative defaults and strict configuration validation."""
from __future__ import annotations
import copy
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tomllib
from .contracts import DEFAULT_MODELS, OrchestratorError

DEFAULTS = {
    "models": DEFAULT_MODELS, "checks": {},
    "runtime": {"command": ["codex"], "worktree_root": "", "max_calls": 80,
                "max_tasks": 64, "max_global_rounds": 2, "model_timeout": 900,
                "check_timeout": 300, "parallel_workers": 1, "checkpoint_every": 2,
                "checkpoint_repairs": 1, "max_prompt_bytes": 65536, "max_log_bytes": 16777216,
                "handoff_bytes": 6000},
    "attempts": {"luna": 2, "terra": 2, "sol": 2},
    "budgets": {"run_tokens": 2000000, "task_tokens": 500000, "call_tokens": 100000,
                "task_calls": 24, "run_cost_usd": "", "task_cost_usd": "",
                "mode": "observed", "model_tokens": {}},
    "prices": {},
    "policy": {"extra_patterns": [], "minimum_tier": "luna"},
    "verification": {"trusted_signers": "", "require_human": "critical", "mutation_policy": "critical",
                     "protected_paths": []},
}

def money(value, allow_empty=False) -> Decimal | None:
    if allow_empty and value == "":
        return None
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError()
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OrchestratorError(f"Invalid decimal monetary value: {value!r}") from exc

def argv(value, label):
    if (not isinstance(value, list) or not value or
        any(not isinstance(x, str) or not x or "\0" in x for x in value)):
        raise OrchestratorError(f"{label} must be an argv array, not a shell string")

def load_config(root: Path) -> dict:
    try:
        raw = tomllib.loads((root / "orchestrator.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestratorError(f"Invalid orchestrator.toml: {exc}") from exc
    if type(raw.get("version")) is not int or raw.get("version") != 2 or set(raw) - ({"version"} | set(DEFAULTS)):
        raise OrchestratorError("Use orchestrator.toml version = 2; see migration guide")
    cfg = copy.deepcopy(DEFAULTS)
    for section in DEFAULTS:
        value = raw.get(section, {})
        if not isinstance(value, dict):
            raise OrchestratorError(f"{section} must be a table")
        if section in {"checks", "prices"}:
            cfg[section] = value
        elif section == "models":
            for role, settings in value.items():
                if role not in cfg[section] or not isinstance(settings, dict) or set(settings) - set(cfg[section][role]):
                    raise OrchestratorError(f"Invalid model configuration: {role}")
                cfg[section][role].update(settings)
        else:
            if set(value) - set(cfg[section]):
                raise OrchestratorError(f"Unknown {section} fields: {set(value) - set(cfg[section])}")
            cfg[section].update(value)
    for role, settings in cfg["models"].items():
        for field in ("model", "family"):
            if not isinstance(settings[field], str) or not settings[field].strip():
                raise OrchestratorError(f"Empty {role}.{field}")
        for field in ("effort", "review_effort"):
            if settings[field] not in {"none", "low", "medium", "high", "xhigh", "max"}:
                raise OrchestratorError(f"Unsupported {role}.{field}")
    r = cfg["runtime"]
    argv(r["command"], "runtime.command")
    if not isinstance(r["worktree_root"], str) or "\0" in r["worktree_root"]:
        raise OrchestratorError("Invalid worktree_root")
    for key in set(r) - {"command", "worktree_root"}:
        if type(r[key]) is not int or not 1 <= r[key] <= 100000000:
            raise OrchestratorError(f"Invalid runtime.{key}")
    if r["parallel_workers"] > 16 or r["handoff_bytes"] < 512:
        raise OrchestratorError("parallel_workers <= 16 and handoff_bytes >= 512 required")
    if not cfg["checks"]:
        raise OrchestratorError("Configure at least one real check in [checks]")
    for name, command in cfg["checks"].items():
        argv(command, f"checks.{name}")
    for role, limit in cfg["attempts"].items():
        if type(limit) is not int or not 1 <= limit <= 10:
            raise OrchestratorError(f"Invalid attempts.{role}")
    b = cfg["budgets"]
    for key in ("run_tokens", "task_tokens", "call_tokens", "task_calls"):
        if type(b[key]) is not int or b[key] <= 0:
            raise OrchestratorError(f"Invalid budgets.{key}")
    if b["call_tokens"] > min(b["task_tokens"], b["run_tokens"]):
        raise OrchestratorError("call_tokens must fit task_tokens and run_tokens")
    if b["mode"] not in {"observed", "strict"}:
        raise OrchestratorError("budgets.mode must be observed or strict")
    if not isinstance(b["model_tokens"], dict):
        raise OrchestratorError("budgets.model_tokens must be a table")
    for role, limit in b["model_tokens"].items():
        if role not in cfg["models"] or type(limit) is not int or limit < b["call_tokens"]:
            raise OrchestratorError("Invalid per-model token budget")
    for key in ("run_cost_usd", "task_cost_usd"):
        limit = money(b[key], allow_empty=True)
        if limit is not None and limit <= 0:
            raise OrchestratorError("Cost ceilings must be positive")
    for role, rates in cfg["prices"].items():
        if role not in cfg["models"] or not isinstance(rates, dict) or set(rates) != {"input", "cached_input", "output"}:
            raise OrchestratorError("Prices require input, cached_input and output per million tokens, by role")
        for value in rates.values():
            money(value)
        if money(rates["cached_input"]) > money(rates["input"]):
            raise OrchestratorError("Cached input price must not exceed input price")
    if (b["run_cost_usd"] or b["task_cost_usd"]) and set(cfg["prices"]) != set(cfg["models"]):
        raise OrchestratorError("Financial ceilings require explicit rates for every model role")
    p = cfg["policy"]
    if p["minimum_tier"] not in {"luna", "terra", "sol"}:
        raise OrchestratorError("Invalid policy.minimum_tier")
    if not isinstance(p["extra_patterns"], list) or any(not isinstance(x, str) or not x for x in p["extra_patterns"]):
        raise OrchestratorError("policy.extra_patterns must contain non-empty glob strings")
    v = cfg["verification"]
    from .contracts import safe_path
    if not isinstance(v["protected_paths"], list):
        raise OrchestratorError("verification.protected_paths must be an array")
    for entry in v["protected_paths"]:
        safe_path(entry, protected=False)
    if v["mutation_policy"] not in {"optional", "critical", "all"}:
        raise OrchestratorError("verification.mutation_policy must be optional, critical or all")
    if v["require_human"] not in {"critical", "all"}:
        raise OrchestratorError("Human verification cannot be disabled for critical changes")
    if not isinstance(v["trusted_signers"], str) or "\0" in v["trusted_signers"]:
        raise OrchestratorError("Invalid verification.trusted_signers")
    if v["trusted_signers"]:
        path = Path(v["trusted_signers"]).expanduser()
        if not path.is_absolute() or path.is_symlink() or path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise OrchestratorError("trusted_signers must be an existing absolute file outside the repository")
        if path.stat().st_size > 65536:
            raise OrchestratorError("Oversized trusted signer allowlist")
        try:
            allowlist = json.loads(path.read_text(encoding="utf-8"))
            identities = allowlist["identities"]
            if (set(allowlist) != {"version", "identities"} or allowlist["version"] != 1 or
                not isinstance(identities, dict) or not identities or
                any(not isinstance(k, str) or not k or len(k) > 256 or "\n" in k or
                    not isinstance(pem, str) or len(pem) > 8192 or
                    not pem.startswith("-----BEGIN PUBLIC KEY-----") or "PRIVATE KEY" in pem
                    for k, pem in identities.items())):
                raise ValueError("invalid allowlist schema")
        except (ValueError, KeyError, TypeError) as exc:
            raise OrchestratorError("trusted_signers must be a v1 JSON identity/public-key allowlist") from exc
        v["trusted_signers"] = str(path.resolve())
    return cfg
