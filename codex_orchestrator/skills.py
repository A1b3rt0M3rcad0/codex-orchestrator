"""Deterministic, repository-local skill discovery and stage-bound instruction delivery.

Only Git-tracked .agents/skills/<directory>/SKILL.md files are eligible.
Cataloguing does not execute skill scripts. Dispatch proves context delivery,
not that the remote model followed the instructions.
"""
from __future__ import annotations

import re
from pathlib import Path

from .contracts import OrchestratorError, digest, safe_id
from .system import changed, file_hash, tracked

SKILL_PATH = re.compile(r"^\.agents/skills/([^/]+)/SKILL\.md$")
MAX_SKILLS = 128
MAX_SKILL_BYTES = 64 * 1024
MAX_REQUIRED_BYTES = 40 * 1024
RESERVED = {"codex-orchestrator"}


def _frontmatter(text: str, path: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise OrchestratorError(f"Skill needs YAML frontmatter: {path}")
    try:
        end = next(i for i in range(1, min(len(lines), 128)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise OrchestratorError(f"Unclosed skill frontmatter: {path}") from exc

    values: dict[str, str] = {}
    active = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if active and active in {"name", "description"}:
                values[active] += " " + line.strip()
            continue
        match = re.match(r"^(name|description):\s*(.*)$", line)
        active = match.group(1) if match else None
        if match:
            value = match.group(2).strip()
            if value in {">", ">-", "|", "|-"}:
                value = ""
            values[active] = value.strip("'\" ")
    name, description = values.get("name", ""), values.get("description", "")
    if (not name or not description or len(description) > 2000 or
        name.startswith(" ") or name.endswith(" ")):
        raise OrchestratorError(f"Invalid skill name or description: {path}")
    safe_id(name)
    return name, description


def discover(root: Path) -> dict[str, dict]:
    """Build a small deterministic catalog of project skills; never scan HOME."""
    root = root.resolve()
    tracked_files = set(tracked(root))
    for path in changed(root):
        if SKILL_PATH.fullmatch(path) and path not in tracked_files:
            raise OrchestratorError(f"Uncommitted skill may not enter an execution: {path}")
    catalog: dict[str, dict] = {}
    for rel in sorted(tracked_files):
        match = SKILL_PATH.fullmatch(rel)
        if not match:
            continue
        if len(catalog) >= MAX_SKILLS:
            raise OrchestratorError("Skill catalog exceeds its size limit")
        path = root / rel
        for ancestor in (path, path.parent, path.parent.parent, path.parent.parent.parent):
            if ancestor.is_symlink():
                raise OrchestratorError(f"Symlink in project skill path: {rel}")
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise OrchestratorError(f"Tracked skill is missing or unsafe: {rel}")
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise OrchestratorError(f"Skill SKILL.md is too large: {rel}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise OrchestratorError(f"Skill is not UTF-8: {rel}") from exc
        name, description = _frontmatter(content, rel)
        if name in catalog:
            raise OrchestratorError(f"Duplicate project skill name: {name}")
        catalog[name] = {"name": name, "path": rel, "description": description,
                         "sha256": file_hash(path)}
    return dict(sorted(catalog.items()))


def validate_bindings(plan: dict, catalog: dict[str, dict]) -> None:
    for task in plan["tasks"]:
        binding = task.get("skills")
        if binding is None:
            continue
        for name in binding["required"] + binding["recommended"]:
            if name in RESERVED:
                raise OrchestratorError(f"{task['id']}: controller skill cannot be assigned to a worker: {name}")
            if name not in catalog:
                raise OrchestratorError(f"{task['id']}: required/recommended project skill is unavailable: {name}")


def verify_catalog(root: Path, expected: dict[str, dict]) -> None:
    actual = discover(root)
    if actual != expected:
        raise OrchestratorError("Project skill catalog changed after planning/sealing")


def stage_context(root: Path, catalog: dict[str, dict],
                  binding: dict | None, mode: str) -> dict | None:
    if not binding:
        return None
    required, recommended = [], []
    total_bytes = 0
    for field, output in (("required", required), ("recommended", recommended)):
        for name in binding[field]:
            if name not in catalog:
                raise OrchestratorError(f"Bound skill not available: {name}")
            item = dict(catalog[name])
            path = root / item["path"]
            if path.is_symlink() or not path.is_file() or file_hash(path) != item["sha256"]:
                raise OrchestratorError(f"Bound skill content changed: {name}")
            if field == "required" and mode == "implement":
                content = path.read_text(encoding="utf-8")
                total_bytes += len(content.encode("utf-8"))
                if total_bytes > MAX_REQUIRED_BYTES:
                    raise OrchestratorError("Required skill instructions exceed per-stage prompt allowance")
                item["instructions"] = content
            output.append(item)
    return {
        "required": required, "recommended": recommended,
        "delivery": ("required instructions embedded in implement-stage request"
                     if mode == "implement" else
                     "skill references available to independent reviewer; no use attested"),
        "policy": "Only required SKILL.md is embedded; consult project references on demand.",
    }


def verify_dispatches(state: dict) -> None:
    """Verify stored request bytes and bindings; no claim of model compliance."""
    from .contracts import read_json
    from .evidence import verify_artifact
    catalog = state.get("skill_catalog", {})
    if not catalog:
        return
    if state.get("skill_catalog_sha256") != digest(catalog):
        raise OrchestratorError("Skill catalog fingerprint mismatch")
    verify_catalog(Path(state["worktree"]), catalog)
    for task in state["tasks"].values():
        binding = task.get("skills")
        if not binding or not binding["required"]:
            continue
        implementations = [call for call in state["calls"]
                           if call["task_id"] == task["id"] and call["mode"] == "implement"]
        if task["status"] == "integrated" and not implementations:
            raise OrchestratorError(f"Required skills have no implementation dispatch: {task['id']}")
    for call in state["calls"]:
        task = state["tasks"].get(call["task_id"])
        if not task or not task.get("skills"):
            continue
        proof = call.get("skill_evidence")
        if not proof:
            raise OrchestratorError(f"Missing skill dispatch evidence for call {call['number']}")
        request = read_json(verify_artifact(proof["request"]))
        if request.get("mode") != call["mode"] or request.get("task", {}).get("id") != task["id"]:
            raise OrchestratorError("Skill dispatch does not match its model call")
        binding = task["skills"]
        skills = request.get("skill_context")
        if not isinstance(skills, dict):
            raise OrchestratorError("Model request omitted bound skills")
        for field in ("required", "recommended"):
            actual = skills.get(field)
            if not isinstance(actual, list) or [x.get("name") for x in actual] != binding[field]:
                raise OrchestratorError("Skill dispatch names differ from reviewed contract")
            for item in actual:
                source = catalog[item["name"]]
                if any(item.get(k) != source[k] for k in ("path", "sha256", "description")):
                    raise OrchestratorError("Skill dispatch fingerprint mismatch")
                if field == "required" and call["mode"] == "implement":
                    if not isinstance(item.get("instructions"), str):
                        raise OrchestratorError("Required skill instructions not embedded in implementation request")
                    if item["instructions"].encode("utf-8") != (
                        Path(state["worktree"]) / source["path"]).read_bytes():
                        raise OrchestratorError("Dispatched skill instructions mismatch protected source")
                elif "instructions" in item:
                    raise OrchestratorError("Non-required or review skill instructions should not be eagerly embedded")
        if proof.get("names") != {"required": binding["required"], "recommended": binding["recommended"]}:
            raise OrchestratorError("Recorded skill dispatch manifest mismatch")
