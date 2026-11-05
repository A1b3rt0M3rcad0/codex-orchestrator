"""Monotonic risk floors and immutable acceptance-test manifests."""
from __future__ import annotations
import fnmatch
import hashlib
import re
from pathlib import Path
from .contracts import OrchestratorError, ROLES, digest, in_scope, safe_path
from .system import changed, command, file_hash, tracked

PATTERNS = {
    "auth": r"\b(auth|authn|authz|authentication|authorization|authenticate\w*|authorize\w*|oauth\w*|jwt|tenant\w*|password\w*|permission\w*|rbac|acl|login|senha|autentica\w*|autoriz\w*)\b",
    "payments": r"\b(pay|payment\w*|payout\w*|payroll|payer\w*|payee\w*|billing|invoice\w*|stripe|pagamento\w*|cobran[cç]a\w*|pix|refund\w*)\b",
    "migrations": r"\b(migrat\w*|migra[cç]\w*|alembic|drop\s+table|alter\s+table|schema\s+change)\b",
    "concurrency": r"\b(concurr\w*|concorr\w*|thread\w*|mutex\w*|semaphore\w*|deadlock\w*|race\s+condition|asyncio|locking)\b",
    "secrets": r"\b(secret\w*|credential\w*|private[_ -]?key|api[_ -]?key|cryptograph\w*|encryption)\b",
    "infrastructure": r"\b(terraform|kubernetes|dockerfile|deployment|iam|firewall|production\s+config)\b",
}

def _changed_behavior(work: Path, base: str, scope: list[str]) -> dict[str, str]:
    """Only changed, non-comment lines can promote a path hint to critical."""
    result = {}
    untracked = set(command(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], work
    ).decode().strip("\0").split("\0"))
    for name in changed(work):
        if not in_scope(name, scope):
            continue
        if name in untracked:
            path = work / name
            data = path.read_bytes() if path.is_file() else b""
            lines = data.decode("utf-8", errors="replace").splitlines()
        else:
            patch = command(
                ["git", "diff", "--no-ext-diff", "--no-renames", "--unified=0", base, "--", name],
                work,
            ).decode("utf-8", errors="replace")
            lines = [line[1:] for line in patch.splitlines()
                     if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))]
        material = [line.strip() for line in lines
                    if line.strip() and not line.lstrip().startswith(
                        ("#", "//", "/*", "*", "--", "<!--"))]
        if material:
            result[name] = "\n".join(material)
    return result


def assess(task: dict, config: dict, work: Path | None = None, base: str | None = None,
           inherited=()) -> dict:
    old = task.get("risk", {})
    explicit = set(task.get("risk_tags", [])) | set(inherited)
    tags = set(old.get("tags", [])) | explicit
    reasons = []
    scanned = list(task["scope"] + task.get("reads", []))
    if work is not None:
        size = 0
        for name in sorted(set(tracked(work)) | set(changed(work))):
            if not in_scope(name, scanned):
                continue
            scanned.append(name)
            path = work / name
            if path.is_symlink() or not path.resolve().is_relative_to(work.resolve()):
                raise OrchestratorError(f"Symlink in risk scan: {name}")
            if path.is_file():
                size += path.stat().st_size
                if size > 2 * 1024 * 1024 or b"\0" in path.read_bytes():
                    tags.add("large_or_opaque_change")
    hints = " ".join([task["objective"], *scanned]).replace("_", " ").replace("/", " ")
    for tag, pattern in PATTERNS.items():
        if re.search(pattern, hints, re.IGNORECASE):
            tags.add(tag)
            reasons.append(f"path_or_intent:{tag}")

    critical = bool(explicit or old.get("critical") or "large_or_opaque_change" in tags)
    if work is not None and base:
        for name, lines in _changed_behavior(work, base, task["scope"]).items():
            # Documentation changes are not operational changes. Unknown/binary
            # payloads remain conservative through the separate opaque-file rule.
            if Path(name).suffix.lower() in {".md", ".rst", ".adoc"}:
                continue
            label = name.replace("_", " ").replace("/", " ")
            for tag, pattern in PATTERNS.items():
                if re.search(pattern, label, re.IGNORECASE) or re.search(pattern, lines, re.IGNORECASE):
                    tags.add(tag)
                    critical = True
                    reasons.append(f"changed_behavior:{tag}:{name}")

    for pattern in config["policy"]["extra_patterns"]:
        if any(fnmatch.fnmatchcase(p.lower(), pattern.lower()) for p in scanned):
            tags.add("project_policy")
            critical = True
            reasons.append(f"project:{pattern}")

    # Hints alone have a Terra floor. Explicit policy, inherited critical risk,
    # changed sensitive behavior, and opaque changes retain the Sol floor.
    requested = config["policy"]["minimum_tier"]
    minimum = "sol" if critical else ("terra" if tags and requested == "luna" else requested)
    result = {"critical": critical, "tags": sorted(tags), "minimum_tier": minimum,
              "reasons": sorted(set(reasons) | {f"declared/inherited:{t}" for t in explicit})}
    result["digest"] = digest(result)
    return result

def route(task: dict, risk: dict) -> bool:
    old = task["tier"]
    task["risk"] = risk
    task["tier"] = ROLES[max(ROLES.index(old), ROLES.index(risk["minimum_tier"]))]
    return old != task["tier"]

# Test runners and transitive configuration are part of the trusted oracle.
RUNNER_FILES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lock", "bun.lockb", "pyproject.toml", "poetry.lock", "uv.lock",
    "pytest.ini", "tox.ini", "setup.cfg", "setup.py", "requirements.txt",
    "Makefile", "GNUmakefile", "justfile", "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum", "Gemfile", "Gemfile.lock", "composer.json",
    "composer.lock", "tsconfig.json", "vitest.config.ts", "vitest.config.js",
    "jest.config.js", "jest.config.ts", "playwright.config.ts",
}


def _runner_dependency(name: str) -> bool:
    path = Path(name)
    basename = path.name
    return (basename in RUNNER_FILES or basename.startswith((
                "requirements-", "pytest.", "jest.config.", "vitest.config.",
                "playwright.config.", "conftest.", "webpack.config.",
                "vite.config.", "babel.config.", "eslint.config."))
            or name.startswith(("tests/", "test/", "__tests__/", ".github/workflows/"))
            or "/tests/" in name or "/__tests__/" in name
            or basename.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")))


def seal(root: Path, plan: dict, config: dict) -> dict:
    available = set(tracked(root))
    files = {p for t in plan["tasks"] for p in t["test_contract"]["files"]}
    for name in available:
        if (name == "orchestrator.toml" or Path(name).name.lower() in {
                "agents.md", ".gitignore", ".gitattributes"}
                or name.startswith((".codex/", ".agents/"))
                or _runner_dependency(name)):
            files.add(name)
    # The user can protect custom test adapters/fixtures beyond detected runners.
    for entry in config["verification"]["protected_paths"]:
        matched = [name for name in available if in_scope(name, [entry])]
        if not matched:
            raise OrchestratorError(f"Protected runner path not committed: {entry}")
        files.update(matched)
    for argv in config["checks"].values():
        files.update(x for x in argv if x in available and Path(x).suffix in {
            ".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb"})
    result = {}
    for name in sorted(files):
        safe_path(name, protected=False)
        path = root / name
        if name not in available or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise OrchestratorError(f"Acceptance/config file must be committed, regular and in-repository: {name}")
        if any(in_scope(name, t["scope"]) for t in plan["tasks"]):
            raise OrchestratorError(f"Protected test/config file overlaps implementation scope: {name}")
        result[name] = {"sha256": file_hash(path), "mode": path.stat().st_mode & 0o777}
    # Keep policy in the sealed manifest so newly created protected files
    # cannot silently become new test implementations or runner adapters.
    result["$protected_paths"] = {"paths": list(config["verification"]["protected_paths"])}
    return result

def guard(root: Path, manifest: dict) -> None:
    protected = manifest.get("$protected_paths", {}).get("paths", [])
    for name in set(tracked(root)) | set(changed(root)):
        if (_runner_dependency(name) or in_scope(name, protected)) and name not in manifest:
            raise OrchestratorError(f"Protected runner path was added after sealing: {name}")
    for name, expected in manifest.items():
        if name == "$protected_paths":
            continue
        path = root / name
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or
            file_hash(path) != expected["sha256"] or path.stat().st_mode & 0o777 != expected["mode"]):
            raise OrchestratorError(f"Protected acceptance/config file changed: {name}")

def require_verification(task: dict, config: dict) -> None:
    if task["risk"]["critical"]:
        if not task.get("adversarial_checks"):
            raise OrchestratorError(f"{task['id']}: critical task requires independent adversarial_checks")
        if not config["verification"]["trusted_signers"]:
            raise OrchestratorError("Critical task requires trusted_signers for the external human approval gate")
    policy = config["verification"]["mutation_policy"]
    if (policy == "all" or (policy == "critical" and task["risk"]["critical"])) and not task["test_contract"].get("mutations"):
        raise OrchestratorError(f"{task['id']}: risk policy requires pre-approved mutation tests")
