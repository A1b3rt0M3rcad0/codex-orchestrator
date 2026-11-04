"""Small, strict contracts. No third-party runtime dependencies."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

ROLES = ("luna", "terra", "sol")
REVIEWER = {"luna": "terra", "terra": "sol", "sol": "astra"}
DEFAULT_MODELS = {
    "astra": {"model": "gpt-6-astra", "effort": "max", "review_effort": "max"},
    "sol": {"model": "gpt-6.1-sol", "effort": "low", "review_effort": "high"},
    "terra": {"model": "gpt-6.1-terra", "effort": "medium", "review_effort": "high"},
    "luna": {"model": "gpt-6-luna", "effort": "max", "review_effort": "max"},
}
PROTECTED = (".git", ".codex", ".agents", "orchestrator.toml")


class OrchestratorError(Exception):
    """Expected, user-actionable failure; never interpreted as approval."""


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 8000}
STRINGS = {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 64}
TASK_FIELDS = {"id": TEXT, "tier": {"type": "string", "enum": list(ROLES)},
               "objective": TEXT, "scope": STRINGS, "acceptance": STRINGS,
               "checks": STRINGS}
CHILD_SCHEMA = obj(TASK_FIELDS)
TASK_SCHEMA = obj({**TASK_FIELDS, "depends_on": {"type": "array", "items": TEXT}})
PLAN_SCHEMA = obj({"version": {"type": "integer", "enum": [1]},
                   "objective": TEXT, "acceptance": STRINGS,
                   "integration_checks": STRINGS,
                   "tasks": {"type": "array", "items": TASK_SCHEMA,
                             "minItems": 1, "maxItems": 64}})
IMPLEMENT_SCHEMA = obj({"status": {"type": "string", "enum": ["implemented", "delegate", "blocked"]},
                        "summary": TEXT,
                        "children": {"type": "array", "items": CHILD_SCHEMA, "maxItems": 16}})
REVIEW_SCHEMA = obj({"decision": {"type": "string", "enum": ["approve", "changes_requested", "blocked"]},
                    "snapshot": TEXT, "summary": TEXT,
                    "findings": {"type": "array", "maxItems": 64, "items": obj({
                        "task_id": TEXT, "file": TEXT, "detail": TEXT})},
                    "acceptance": {"type": "array", "items": obj({
                        "criterion": TEXT, "passed": {"type": "boolean"}, "evidence": TEXT})}})


def validate(data, schema: dict, path: str = "$") -> None:
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if type(data) is not types[schema["type"]]:
        raise OrchestratorError(f"{path}: expected {schema['type']}")
    if "enum" in schema and data not in schema["enum"]:
        raise OrchestratorError(f"{path}: unsupported value {data!r}")
    if isinstance(data, dict):
        if set(data) != set(schema["properties"]):
            raise OrchestratorError(f"{path}: fields must be {list(schema['properties'])}")
        for key, value in data.items():
            validate(value, schema["properties"][key], f"{path}.{key}")
    if isinstance(data, list):
        if not schema.get("minItems", 0) <= len(data) <= schema.get("maxItems", 128):
            raise OrchestratorError(f"{path}: invalid array length")
        for index, value in enumerate(data):
            validate(value, schema["items"], f"{path}[{index}]")
    if isinstance(data, str):
        if not schema.get("minLength", 0) <= len(data) <= schema.get("maxLength", 8000):
            raise OrchestratorError(f"{path}: invalid string length")
        if schema.get("minLength", 0) and not data.strip():
            raise OrchestratorError(f"{path}: blank string")


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise OrchestratorError(f"Invalid identifier: {value!r}")
    return value


def safe_path(value: str) -> str:
    p = PurePosixPath(value.rstrip("/"))
    if not value or value.startswith("/") or "\\" in value or ":" in value:
        raise OrchestratorError(f"Use a repository-relative POSIX path: {value!r}")
    if any(c in value for c in "*?[]\n\r\0") or ".." in p.parts or str(p) in (".", ""):
        raise OrchestratorError(f"Invalid scope path: {value!r}")
    if any(part.casefold() == ".git" for part in p.parts):
        raise OrchestratorError("Git metadata is not task scope")
    if p.parts[0].casefold() in PROTECTED:
        raise OrchestratorError(f"Protected controller configuration: {value}")
    if str(p) != value.rstrip("/"):
        raise OrchestratorError(f"Non-canonical path: {value}")
    return value


def in_scope(path: str, scope: list[str]) -> bool:
    return any(path == item.rstrip("/") or (item.endswith("/") and path.startswith(item)) for item in scope)


def validate_task(task: dict, checks: dict) -> None:
    safe_id(task["id"])
    for item in task["scope"]:
        safe_path(item)
    if len(set(task["acceptance"])) != len(task["acceptance"]):
        raise OrchestratorError("Acceptance criteria must be unique")
    if not set(task["checks"]).issubset(checks):
        raise OrchestratorError(f"Unknown checks for {task['id']}")


def validate_plan(plan: dict, checks: dict) -> None:
    validate(plan, PLAN_SCHEMA)
    ids = [t["id"] for t in plan["tasks"]]
    if len(set(ids)) != len(ids) or "global" in ids:
        raise OrchestratorError("Task ids must be unique; 'global' is reserved")
    if len(set(plan["acceptance"])) != len(plan["acceptance"]):
        raise OrchestratorError("Global acceptance criteria must be unique")
    if not set(plan["integration_checks"]).issubset(checks):
        raise OrchestratorError("Unknown integration checks")
    remaining = {t["id"]: set(t["depends_on"]) for t in plan["tasks"]}
    for task in plan["tasks"]:
        validate_task(task, checks)
        if not set(task["depends_on"]).issubset(ids):
            raise OrchestratorError(f"Unknown dependency: {task['id']}")
    done: set[str] = set()
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
    returned = [x["criterion"] for x in result["acceptance"]]
    if sorted(returned) != sorted(criteria):
        raise OrchestratorError("Review must account for every acceptance criterion exactly once")
    if result["decision"] == "approve" and (result["findings"] or not all(x["passed"] for x in result["acceptance"])):
        raise OrchestratorError("Approval conflicts with findings/acceptance results")
    if result["decision"] == "changes_requested" and not result["findings"]:
        raise OrchestratorError("Changes requested without actionable findings")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestratorError(f"Cannot read JSON {path}: {exc}") from exc
