"""Validated workspace tools and file change journal."""

from __future__ import annotations

import ast
import base64
import difflib
import hashlib
import json
import os
import selectors
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .permissions import command_tokens
from .storage import Store


class ToolError(RuntimeError):
    """A controlled tool validation or execution error."""


def _root(workspace: str) -> Path:
    return Path(workspace).resolve()


def _path(workspace: str, value: str) -> Path:
    if Path(value).is_absolute():
        raise ToolError("absolute paths are forbidden")
    root = _root(workspace)
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError("path escapes workspace")
    return candidate


def _tree(workspace: str, state_path=None) -> Dict[str, str]:
    root = _root(workspace)
    result: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            relative = str(path.relative_to(root))
            result[relative] = "<symlink:" + str(path.readlink()) + ">"
            continue
        if (
            not path.is_file()
            or ".git" in path.parts
            or (state_path is not None and str(path.resolve()) in {str(Path(state_path).resolve()) + suffix for suffix in ("", "-wal", "-shm")})
        ):
            continue
        relative = str(path.relative_to(root))
        result[relative] = "@file-v2:" + base64.b64encode(path.read_bytes()).decode("ascii")
    return result


def _decode_file(value: str) -> bytes:
    if value.startswith("@file-v2:"):
        return base64.b64decode(value[len("@file-v2:"):], validate=True)
    if value.startswith("<binary:"):
        raise ToolError("legacy binary snapshot cannot be restored")
    return value.encode("utf-8")


def _diff_lines(value: str) -> List[str]:
    return _decode_file(value).decode("utf-8", errors="replace").splitlines(True)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ToolRegistry:
    MAX_READ_BYTES = 100_000
    MAX_OUTPUT_BYTES = 20_000

    def __init__(self, workspace: str, store: Store, run_id: str, timeout: float = 10.0):
        self.workspace = workspace
        self.store = store
        self.run_id = run_id
        self.timeout = timeout
        self._ensure_baseline_snapshot()
        self.specs = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in workspace",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a UTF-8 file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a UTF-8 file in workspace",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Delete one regular file in workspace",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "description": (
                        "Run tests without constructing a shell command. Prefer this tool when "
                        "the user asks to run or diagnose tests. Uses python3 and either pytest "
                        "or unittest. A nonzero exit code means tests ran and failed; inspect "
                        "the output. pytest must be installed for that runner."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "runner": {"type": "string", "enum": ["pytest", "unittest"]},
                            "path": {"type": "string"},
                            "cwd": {"type": "string"},
                        },
                        "required": ["runner"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": (
                        "Run one command in workspace, subject to configured ask/allow/deny "
                        "permissions. There is no built-in command-name allowlist. "
                        "Prefer python3 because python may not be installed. "
                        "For tests, prefer run_tests. Do not combine commands: &&, pipes, "
                        "redirects and substitutions are forbidden. Use separate tool calls. "
                        "Working directory and path arguments must stay within workspace."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _ensure_baseline_snapshot(self) -> None:
        row = self.store.db.execute(
            "SELECT 1 FROM snapshots WHERE run_id=? LIMIT 1", (self.run_id,)
        ).fetchone()
        if row is not None:
            return
        self.store.db.execute(
            "INSERT INTO snapshots(run_id,seq,tree_json) VALUES(?,?,?)",
            (self.run_id, 0, json.dumps(_tree(self.workspace, self.store.path), ensure_ascii=False, sort_keys=True)),
        )

    def definitions(self) -> List[Dict[str, Any]]:
        return self.specs

    def validate(self, name: str, args: Dict[str, Any]) -> None:
        schemas = {item["function"]["name"]: item["function"]["parameters"] for item in self.specs}
        if name not in schemas:
            raise ToolError("unknown tool: " + name)
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be an object")
        schema = schemas[name]
        missing = [key for key in schema["required"] if key not in args]
        if missing:
            raise ToolError("missing required argument: " + ", ".join(missing))
        extra = set(args) - set(schema["properties"])
        if extra:
            raise ToolError("unknown arguments: " + ", ".join(sorted(extra)))
        for key, value in args.items():
            expected = schema["properties"][key]["type"]
            if expected == "string" and not isinstance(value, str):
                raise ToolError("argument {} must be a string".format(key))

    def execute(self, name: str, args: Dict[str, Any], plan_mode: bool = False) -> Dict[str, Any]:
        self.validate(name, args)
        if plan_mode and name in {"write_file", "delete_file", "bash", "run_tests"}:
            raise ToolError("side effects are forbidden before plan approval")
        if name == "list_files":
            path = _path(self.workspace, args.get("path", ""))
            return {
                "files": [
                    str(item.relative_to(_root(self.workspace)))
                    for item in sorted(path.rglob("*"))
                    if item.is_file()
                ]
            }
        if name == "read_file":
            path = _path(self.workspace, args["path"])
            with path.open("rb") as stream:
                content = stream.read(self.MAX_READ_BYTES + 1)
            if len(content) > self.MAX_READ_BYTES:
                return {
                    "path": args["path"],
                    "content": content[: self.MAX_READ_BYTES].decode("utf-8", errors="replace"),
                    "truncated": True,
                }
            return {"path": args["path"], "content": content.decode("utf-8"), "truncated": False}
        if name == "write_file":
            return self._write(args["path"], args["content"])
        if name == "delete_file":
            return self._delete(args["path"])
        if name == "run_tests":
            return self._run_tests(args["runner"], args.get("path", ""), args.get("cwd", ""))
        return self._bash(args["command"], args.get("cwd", ""))

    def _run_tests(self, runner: str, path: str, cwd: str) -> Dict[str, Any]:
        if runner not in {"pytest", "unittest"}:
            raise ToolError("runner must be pytest or unittest")
        workdir = _path(self.workspace, cwd or ".")
        if path and not (runner == "unittest" and path == "discover"):
            target = _path(self.workspace, str(Path(cwd or ".") / path))
            if not target.exists():
                raise ToolError("test target does not exist: " + path)
        command = ["python3", "-m", runner]
        if runner == "unittest":
            command.append("-v")
        if path and not (runner == "unittest" and path == "discover"):
            command.append(path)
        self._assert_baseline()
        before = _tree(self.workspace, self.store.path)
        try:
            process = subprocess.Popen(
                command, shell=False, cwd=str(workdir), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=False, start_new_session=True,
            )
        except OSError as exc:
            raise ToolError("could not start python3 test runner: " + str(exc)) from exc
        stdout, stderr, timed_out = self._collect(process)
        after = _tree(self.workspace, self.store.path)
        if after != before:
            self._record_changes(before, after)
        return {
            "runner": runner,
            "target": path or ".",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "truncated": len(stdout.encode("utf-8")) >= self.MAX_OUTPUT_BYTES
            or len(stderr.encode("utf-8")) >= self.MAX_OUTPUT_BYTES,
        }

    def _assert_baseline(self) -> None:
        rows = self.store.db.execute(
            "SELECT tree_json FROM snapshots WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if rows is None:
            return
        if json.loads(rows["tree_json"]) != _tree(self.workspace, self.store.path):
            raise ToolError("workspace conflict detected before side effect")

    def _write(self, path: str, content: str) -> Dict[str, Any]:
        self._assert_baseline()
        target = _path(self.workspace, path)
        if target.suffix == ".py":
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                raise ToolError("invalid Python source; use real newlines, preserve literal escapes in strings: " + str(exc)) from exc
        before = _tree(self.workspace, self.store.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._record_changes(before, _tree(self.workspace, self.store.path))
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def _delete(self, path: str) -> Dict[str, Any]:
        self._assert_baseline()
        target = _path(self.workspace, path)
        if not target.exists() or not target.is_file() or target.is_symlink():
            raise ToolError("delete_file accepts an existing regular file")
        before = _tree(self.workspace, self.store.path)
        target.unlink()
        self._record_changes(before, _tree(self.workspace, self.store.path))
        return {"path": path, "deleted": True}

    def _bash(self, command: str, cwd: str) -> Dict[str, Any]:
        try:
            parts = command_tokens(command)
        except ValueError as exc:
            raise ToolError("invalid bash command: " + str(exc)) from exc
        execution_root = _path(self.workspace, cwd or ".")
        for value in parts[1:]:
            if not value.startswith("-"):
                candidate = (execution_root / value).resolve()
                if candidate != _root(self.workspace) and _root(self.workspace) not in candidate.parents:
                    raise ToolError("bash argument escapes workspace")
        self._assert_baseline()
        before = _tree(self.workspace, self.store.path)
        process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", "exec " + shlex.join(parts)],
            start_new_session=True,
            shell=False,
            cwd=str(_path(self.workspace, cwd or ".")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        stdout, stderr, timed_out = self._collect(process)
        self._record_changes(before, _tree(self.workspace, self.store.path))
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "truncated": len(stdout.encode("utf-8")) >= self.MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) >= self.MAX_OUTPUT_BYTES,
        }

    def _collect(self, process: subprocess.Popen[bytes]) -> tuple[str, str, bool]:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        timed_out = False
        deadline = __import__("time").monotonic() + self.timeout
        try:
            while selector.get_map() or process.poll() is None:
                remaining = deadline - __import__("time").monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk[:max(0, self.MAX_OUTPUT_BYTES - len(buffers[key.data]))])
            process.wait(timeout=1)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        return (
            buffers["stdout"].decode("utf-8", errors="replace"),
            buffers["stderr"].decode("utf-8", errors="replace"),
            timed_out,
        )

    def _record_changes(self, before: Dict[str, str], after: Dict[str, str]) -> None:
        with self.store.transaction():
            seq = self.store.db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM snapshots WHERE run_id=?", (self.run_id,)
            ).fetchone()[0]
            self.store.db.execute(
                "INSERT INTO snapshots(run_id,seq,tree_json) VALUES(?,?,?)",
                (self.run_id, seq, json.dumps(after, ensure_ascii=False, sort_keys=True)),
            )
            for path in set(before) | set(after):
                if before.get(path) == after.get(path):
                    continue
                old, new = before.get(path), after.get(path)
                existing = self.store.db.execute("SELECT original FROM changes WHERE run_id=? AND path=?", (self.run_id, path)).fetchone()
                original = existing["original"] if existing else old
                diff = "".join(
                    difflib.unified_diff(
                        _diff_lines(original or ""),
                        _diff_lines(new or ""),
                        fromfile=path,
                        tofile=path,
                    )
                )
                self.store.db.execute(
                    """INSERT INTO changes(run_id,path,original,final,original_hash,final_hash,diff)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,path) DO UPDATE SET final=excluded.final, final_hash=excluded.final_hash, diff=excluded.diff""",
                    (
                        self.run_id,
                        path,
                        old,
                        new,
                        None if old is None else _hash(old),
                        None if new is None else _hash(new),
                        diff,
                    ),
                )
