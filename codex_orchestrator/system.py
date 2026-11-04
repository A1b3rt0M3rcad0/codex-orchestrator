"""Filesystem, process, Git and configuration boundaries."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .contracts import DEFAULT_MODELS, OrchestratorError, in_scope, safe_path


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".co-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def exclusive(path: Path):
    """OS advisory lock; the OS releases it after a crash (no stale PID lock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OrchestratorError("Another controller is running for this repository") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def command(argv: list[str], cwd: Path, timeout: int = 30) -> bytes:
    try:
        result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OrchestratorError(f"Command failed: {argv[0]}: {exc}") from exc
    if result.returncode:
        raise OrchestratorError(f"Command failed ({result.returncode}): {argv[:3]}\n{result.stderr.decode(errors='replace')[-2000:]}")
    return result.stdout


def git(root: Path, *args: str) -> str:
    return command(["git", "-c", "core.quotepath=false", *args], root).decode("utf-8", errors="strict").strip()


def repo_root(path: Path) -> Path:
    return Path(git(path.resolve(), "rev-parse", "--show-toplevel")).resolve()


def store_root(root: Path) -> Path:
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    return (root / common).resolve() / "codex-orchestrator"


def changed(root: Path) -> list[str]:
    tracked = command(["git", "diff", "--no-renames", "--name-only", "-z", "HEAD"], root)
    staged = command(["git", "diff", "--cached", "--no-renames", "--name-only", "-z", "HEAD"], root)
    new = command(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    return sorted({p.decode("utf-8") for p in (tracked + staged + new).split(b"\0") if p})


def snapshot(root: Path) -> str:
    h = hashlib.sha256(git(root, "rev-parse", "HEAD").encode())
    for args in (("diff", "--binary", "--no-ext-diff", "--no-renames", "HEAD"),
                 ("diff", "--cached", "--binary", "--no-ext-diff", "--no-renames", "HEAD")):
        h.update(command(["git", *args], root))
    for path in changed(root):
        file = root / path
        h.update(path.encode())
        if file.is_symlink():
            h.update(b"symlink:" + os.readlink(file).encode())
        elif file.is_file():
            h.update(str(file.stat().st_mode & 0o777).encode())
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
        elif not file.exists():
            h.update(b"deleted")
        else:
            raise OrchestratorError(f"Unsupported changed file: {path}")
    return h.hexdigest()


def enforce_scope(root: Path, scope: list[str], base_head: str) -> list[str]:
    if git(root, "rev-parse", "HEAD") != base_head:
        raise OrchestratorError("Worker changed Git HEAD; automatic processing stopped")
    files = changed(root)
    for path in files:
        safe_path(path)
        if not in_scope(path, scope):
            raise OrchestratorError(f"Out-of-scope edit: {path}")
        file = root / path
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise OrchestratorError(f"Symlink/escaped edit: {path}")
    return files


def commit(root: Path, message: str) -> str:
    if changed(root):
        git(root, "add", "--all", "--", ".")
        expected_tree = git(root, "write-tree")
        git(root, "-c", "user.name=Codex Orchestrator", "-c", "user.email=codex-orchestrator@localhost",
            "-c", "commit.gpgsign=false", "commit", "-m", message)
        if git(root, "rev-parse", "HEAD^{tree}") != expected_tree or changed(root):
            raise OrchestratorError("Commit hooks changed reviewed source; approval rejected")
    return git(root, "rev-parse", "HEAD")


def load_config(root: Path) -> dict:
    try:
        raw = tomllib.loads((root / "orchestrator.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestratorError(f"Invalid orchestrator.toml: {exc}") from exc
    allowed = {"version", "models", "checks", "runtime"}
    if set(raw) - allowed or raw.get("version") != 1:
        raise OrchestratorError("Unknown config fields/version")
    cfg = {"models": json.loads(json.dumps(DEFAULT_MODELS)), "checks": raw.get("checks", {}),
           "runtime": {"command": ["codex"], "worktree_root": "", "attempts_per_tier": 2, "max_calls": 40,
                       "max_tasks": 64, "max_global_rounds": 2,
                       "model_timeout": 900, "check_timeout": 300}}
    if not isinstance(raw.get("models", {}), dict):
        raise OrchestratorError("models must be a table")
    for role, settings in raw.get("models", {}).items():
        if role not in cfg["models"] or not isinstance(settings, dict) or set(settings) - {"model", "effort", "review_effort"}:
            raise OrchestratorError(f"Invalid model configuration: {role}")
        cfg["models"][role].update(settings)
    for role, settings in cfg["models"].items():
        if not isinstance(settings["model"], str) or not settings["model"].strip():
            raise OrchestratorError(f"Empty model: {role}")
        for key in ("effort", "review_effort"):
            if settings[key] not in ("none", "low", "medium", "high", "xhigh", "max"):
                raise OrchestratorError(f"Unsupported {role}.{key}")
    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict) or set(runtime) - set(cfg["runtime"]):
        raise OrchestratorError("Unknown runtime configuration")
    cfg["runtime"].update(runtime)
    for key, value in cfg["runtime"].items():
        if key not in ("command", "worktree_root") and (type(value) is not int or value < 1 or value > 100000):
            raise OrchestratorError(f"Invalid runtime.{key}")
    if not isinstance(cfg["runtime"]["worktree_root"], str) or "\0" in cfg["runtime"]["worktree_root"]:
        raise OrchestratorError("runtime.worktree_root must be a path string")
    if not isinstance(cfg["checks"], dict) or not cfg["checks"]:
        raise OrchestratorError("Configure at least one real check in [checks]")
    arrays = {"runtime.command": cfg["runtime"]["command"], **cfg["checks"]}
    for key, value in arrays.items():
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x or "\0" in x for x in value):
            raise OrchestratorError(f"{key} must be an argv array, not a shell string")
    return cfg


def run_process(argv: list[str], cwd: Path, stdout: Path, stderr: Path,
                timeout: int, stdin: str | None = None, env: dict | None = None) -> int:
    """Log to files, not RAM; terminate the process group on timeout/interruption."""
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, env=env, start_new_session=os.name != "nt")
        except OSError as exc:
            raise OrchestratorError(f"Cannot start {argv[0]}: {exc}") from exc
        try:
            process.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            raise
        return process.returncode


def argv_expand(argv: list[str]) -> list[str]:
    return [sys.executable if item == "{python}" else item for item in argv]


def tail(path: Path, limit: int = 2000) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")
