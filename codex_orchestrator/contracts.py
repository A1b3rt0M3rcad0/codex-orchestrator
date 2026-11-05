"""Versioned, strict contracts. Models cannot loosen controller policy."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

ROLES = ("luna", "terra", "sol")
REVIEWER = {"luna": "terra", "terra": "sol", "sol": "astra"}
DEFAULT_MODELS = {
    "astra": {"model": "gpt-6-astra", "effort": "max", "review_effort": "max", "family": "openai-gpt"},
    "sol": {"model": "gpt-6.1-sol", "effort": "low", "review_effort": "high", "family": "openai-gpt"},
    "terra": {"model": "gpt-6.1-terra", "effort": "medium", "review_effort": "high", "family": "openai-gpt"},
    "luna": {"model": "gpt-6-luna", "effort": "max", "review_effort": "max", "family": "openai-gpt"},
}
PROTECTED = (".git", ".codex", ".agents", "orchestrator.toml", "AGENTS.md")

class OrchestratorError(Exception):
    """Expected failure; never interpreted as approval."""

class BudgetError(OrchestratorError):
    pass

def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()

def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

def array(items: dict, minimum=0, maximum=64) -> dict:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}

TEXT = {"type": "string", "minLength": 1, "maxLength": 8000}
STRINGS = array(TEXT, 1)
TASK_FIELDS = {"id": TEXT, "tier": {"type": "string", "enum": list(ROLES)},
               "objective": TEXT, "scope": STRINGS, "reads": array(TEXT),
               "acceptance": STRINGS, "checks": STRINGS, "risk_tags": array(TEXT)}
CHILD_SCHEMA = obj(TASK_FIELDS)
TEST_CONTRACT = obj({"files": STRINGS, "cases": array(obj({"criterion": TEXT, "check": TEXT,
                    "baseline": {"type": "string", "enum": ["pass", "fail"]}, "rationale": TEXT}), 1)})
TASK_SCHEMA = obj({**TASK_FIELDS, "depends_on": array(TEXT), "test_contract": TEST_CONTRACT,
                   "adversarial_checks": array(TEXT)})
PLAN_SCHEMA = obj({"version": {"type": "integer", "enum": [2]}, "objective": TEXT,
                   "acceptance": STRINGS, "integration_checks": STRINGS, "checkpoint_checks": STRINGS,
                   "tasks": array(TASK_SCHEMA, 1)})
IMPLEMENT_SCHEMA = obj({"status": {"type": "string", "enum": ["implemented", "delegate", "blocked"]},
                        "summary": TEXT, "children": array(CHILD_SCHEMA, 0, 16)})
REVIEW_SCHEMA = obj({"decision": {"type": "string", "enum": ["approve", "changes_requested", "blocked"]},
                    "snapshot": TEXT, "summary": TEXT,
                    "findings": array(obj({"task_id": TEXT, "file": TEXT, "detail": TEXT})),
                    "acceptance": array(obj({"criterion": TEXT, "passed": {"type": "boolean"}, "evidence": TEXT}), 1)})

def validate(data, schema: dict, path="$", depth=0) -> None:
    if depth > 24:
        raise OrchestratorError("Contract nesting exceeds limit")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if type(data) is not types[schema["type"]]:
        raise OrchestratorError(f"{path}: expected {schema['type']}")
    if "enum" in schema and data not in schema["enum"]:
        raise OrchestratorError(f"{path}: unsupported value {data!r}; use plan/config v2")
    if isinstance(data, dict):
        if set(data) != set(schema["properties"]):
            raise OrchestratorError(f"{path}: fields must be {list(schema['properties'])}")
        for key, value in data.items():
            validate(value, schema["properties"][key], f"{path}.{key}", depth + 1)
    if isinstance(data, list):
        if not schema.get("minItems", 0) <= len(data) <= schema.get("maxItems", 128):
            raise OrchestratorError(f"{path}: invalid array length")
        for index, value in enumerate(data):
            validate(value, schema["items"], f"{path}[{index}]", depth + 1)
    if isinstance(data, str):
        if not schema.get("minLength", 0) <= len(data) <= schema.get("maxLength", 8000) or not data.strip():
            raise OrchestratorError(f"{path}: invalid/blank string")


def transport_schema(schema: dict) -> dict:
    """Keep a conservative generation subset; enforce all constraints locally."""
    if isinstance(schema, dict):
        return {key: transport_schema(value) for key, value in schema.items()
                if key not in {"minLength", "maxLength"}}
    if isinstance(schema, list):
        return [transport_schema(value) for value in schema]
    return schema


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise OrchestratorError(f"Invalid identifier: {value!r}")
    return value

def safe_path(value: str, protected=True) -> str:
    if not isinstance(value, str):
        raise OrchestratorError("Path must be a string")
    p = PurePosixPath(value.rstrip("/"))
    if (not value or value.startswith("/") or "\\" in value or ":" in value or
        any(c in value for c in "*?[]\n\r\0") or ".." in p.parts or str(p) in (".", "")):
        raise OrchestratorError(f"Invalid repository-relative path: {value!r}")
    if any(part.casefold() == ".git" for part in p.parts):
        raise OrchestratorError("Git metadata is not task scope")
    if protected and (p.parts[0].casefold() in {x.casefold() for x in PROTECTED}
                      or p.name.casefold() == "agents.md"):
        raise OrchestratorError(f"Protected controller configuration: {value}")
    if str(p) != value.rstrip("/"):
        raise OrchestratorError(f"Non-canonical path: {value}")
    return value

def in_scope(path: str, scope: list[str]) -> bool:
    return any(path == p.rstrip("/") or (p.endswith("/") and path.startswith(p)) for p in scope)

def overlaps(left: list[str], right: list[str]) -> bool:
    return any(in_scope(a.rstrip("/"), [b]) or in_scope(b.rstrip("/"), [a]) for a in left for b in right)

def validate_task(task: dict, checks: dict) -> None:
    safe_id(task["id"])
    if task["id"] in {"global", "contract"} or task["id"].startswith("repair-"):
        raise OrchestratorError("Reserved task id")
    for item in task["scope"]:
        safe_path(item)
    for item in task["reads"]:
        safe_path(item, protected=False)
    if len(set(task["acceptance"])) != len(task["acceptance"]):
        raise OrchestratorError("Acceptance criteria must be unique")
    if not set(task["checks"]).issubset(checks):
        raise OrchestratorError(f"Unknown checks for {task['id']}")
    if "test_contract" in task:
        contract = task["test_contract"]
        covered, expectations = set(), {}
        for file in contract["files"]:
            safe_path(file)
            if file.endswith("/") or in_scope(file, task["scope"]):
                raise OrchestratorError("Acceptance tests must be exact files outside implementation scope")
        for case in contract["cases"]:
            if case["criterion"] not in task["acceptance"] or case["check"] not in task["checks"]:
                raise OrchestratorError("Test case must map an acceptance criterion to a task check")
            if expectations.setdefault(case["check"], case["baseline"]) != case["baseline"]:
                raise OrchestratorError("Conflicting baseline expectations for check")
            covered.add(case["criterion"])
        if covered != set(task["acceptance"]):
            raise OrchestratorError("Every acceptance criterion needs an independent test case")
        if not set(task["adversarial_checks"]).issubset(checks):
            raise OrchestratorError("Unknown adversarial check")

def validate_plan(plan: dict, checks: dict) -> None:
    validate(plan, PLAN_SCHEMA)
    ids = [t["id"] for t in plan["tasks"]]
    if len(set(ids)) != len(ids):
        raise OrchestratorError("Task ids must be unique")
    if len(set(plan["acceptance"])) != len(plan["acceptance"]):
        raise OrchestratorError("Global acceptance criteria must be unique")
    for group in ("integration_checks", "checkpoint_checks"):
        if not set(plan[group]).issubset(checks):
            raise OrchestratorError(f"Unknown {group}")
    protected = {p for t in plan["tasks"] for p in t["test_contract"]["files"]}
    remaining = {t["id"]: set(t["depends_on"]) for t in plan["tasks"]}
    for task in plan["tasks"]:
        validate_task(task, checks)
        if any(in_scope(p, task["scope"]) for p in protected):
            raise OrchestratorError("A task may not edit another task's acceptance tests")
        if not set(task["depends_on"]).issubset(ids):
            raise OrchestratorError("Unknown dependency")
    done = set()
    while remaining:
        ready = [key for key, deps in remaining.items() if deps <= done]
        if not ready:
            raise OrchestratorError("Dependency cycle")
        for key in ready:
            done.add(key)
            del remaining[key]

def validate_review(result: dict, criteria: list[str], snapshot: str) -> None:
    validate(result, REVIEW_SCHEMA)
    if result["snapshot"] != snapshot:
        raise OrchestratorError("Review refers to a different source snapshot")
    if sorted(x["criterion"] for x in result["acceptance"]) != sorted(criteria):
        raise OrchestratorError("Review must account for every acceptance criterion exactly once")
    if result["decision"] == "approve" and (result["findings"] or not all(x["passed"] for x in result["acceptance"])):
        raise OrchestratorError("Approval conflicts with findings/acceptance results")
    if result["decision"] == "changes_requested" and not result["findings"]:
        raise OrchestratorError("Changes requested without actionable findings")

def read_json(path: Path):
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise OrchestratorError(f"Oversized JSON: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestratorError(f"Cannot read JSON {path}: {exc}") from exc
