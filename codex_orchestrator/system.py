"""Filesystem, process and Git boundaries; no shell evaluation."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from .config import load_config
from .contracts import OrchestratorError, in_scope, safe_path


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


def command(argv: list[str], cwd: Path, timeout=30, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(argv, cwd=cwd, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
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
    return (root / Path(git(root, "rev-parse", "--git-common-dir"))).resolve() / "codex-orchestrator"

def changed(root: Path) -> list[str]:
    data = command(["git", "diff", "--no-renames", "--name-only", "-z", "HEAD"], root)
    data += command(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    return sorted({p.decode("utf-8") for p in data.split(b"\0") if p})

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def snapshot(root: Path) -> str:
    h = hashlib.sha256(git(root, "rev-parse", "HEAD").encode())
    for args in (("diff", "--binary", "--no-ext-diff", "--no-renames", "HEAD"),
                 ("diff", "--cached", "--binary", "--no-ext-diff", "--no-renames", "HEAD")):
        h.update(command(["git", *args], root))
    for name in changed(root):
        path = root / name
        h.update(name.encode())
        if path.is_symlink():
            h.update(b"symlink:" + os.readlink(path).encode())
        elif path.is_file():
            h.update(str(path.stat().st_mode & 0o777).encode())
            h.update(file_hash(path).encode())
        elif not path.exists():
            h.update(b"deleted")
        else:
            raise OrchestratorError(f"Unsupported changed file: {name}")
    return h.hexdigest()

def enforce_scope(root: Path, scope: list[str], base_head: str) -> list[str]:
    if git(root, "rev-parse", "HEAD") != base_head:
        raise OrchestratorError("Worker changed Git HEAD; automatic processing stopped")
    files = changed(root)
    for name in files:
        safe_path(name)
        if not in_scope(name, scope):
            raise OrchestratorError(f"Out-of-scope edit: {name}")
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise OrchestratorError(f"Symlink/escaped edit: {name}")
    return files

def commit(root: Path, message: str) -> str:
    if changed(root):
        git(root, "add", "--all", "--", ".")
        expected = git(root, "write-tree")
        git(root, "-c", "user.name=Codex Orchestrator", "-c", "user.email=codex-orchestrator@localhost",
            "-c", "commit.gpgsign=false", "commit", "-m", message)
        if git(root, "rev-parse", "HEAD^{tree}") != expected or changed(root):
            raise OrchestratorError("Commit hooks changed reviewed source; approval rejected")
    return git(root, "rev-parse", "HEAD")

def diff_bytes(root: Path, base: str, head="HEAD") -> bytes:
    return command(["git", "diff", "--binary", "--no-ext-diff", "--no-renames", base, head], root)

def tracked(root: Path) -> list[str]:
    return [x.decode() for x in command(["git", "ls-files", "-z"], root).split(b"\0") if x]

def argv_expand(argv: list[str]) -> list[str]:
    return [sys.executable if item == "{python}" else item for item in argv]

def tail(path: Path, limit=1800) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")

def run_process(argv: list[str], cwd: Path, stdout: Path, stderr: Path,
                timeout: int, stdin: str | None = None, env: dict | None = None,
                observer=None, cancel=None, max_log_bytes=16777216) -> int:
    """Observe JSONL incrementally; kill the process group on timeout or a gate failure."""
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("wb") as out, stderr.open("wb") as err, tempfile.TemporaryFile() as inp:
        if stdin is not None:
            inp.write(stdin.encode())
            inp.seek(0)
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=inp, stdout=out, stderr=err,
                                       env=env, start_new_session=os.name != "nt")
        except OSError as exc:
            raise OrchestratorError(f"Cannot start {argv[0]}: {exc}") from exc
        start = time.monotonic()
        try:
            while True:
                if observer:
                    observer()
                if cancel is not None and cancel.is_set():
                    raise OrchestratorError("Execution cancelled by another failing task")
                if stdout.stat().st_size + stderr.stat().st_size > max_log_bytes:
                    raise OrchestratorError("Process log budget exceeded")
                result = process.poll()
                if result is not None:
                    if observer:
                        observer()
                    return result
                if time.monotonic() - start >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(0.03)
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            raise
