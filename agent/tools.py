"""Validated workspace tools and file change journal."""

from __future__ import annotations

import difflib
import hashlib
import json
import selectors
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .storage import Store


class ToolError(RuntimeError):
    """A controlled tool validation or execution error."""


def _root(workspace: str) -> Path:
    return Path(workspace).resolve()


def _path(workspace: str, value: str) -> Path:
    root = _root(workspace)
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError("path escapes workspace")
    return candidate


def _tree(workspace: str) -> Dict[str, str]:
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
            or ".agent" in path.parts
            or path.suffix in {".sqlite3", ".db"}
            or path.name.endswith(("-wal", "-shm"))
        ):
            continue
        relative = str(path.relative_to(root))
        try:
            result[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            result[relative] = "<binary:" + _hash_bytes(path.read_bytes()) + ">"
    return result


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
                    "name": "bash",
                    "description": "Run an allowlisted read/test command in workspace",
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
            (self.run_id, 0, json.dumps(_tree(self.workspace), ensure_ascii=False, sort_keys=True)),
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
        if plan_mode and name in {"write_file", "delete_file", "bash"}:
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
            content = path.read_bytes()
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
        return self._bash(args["command"], args.get("cwd", ""))

    def _assert_baseline(self) -> None:
        rows = self.store.db.execute(
            "SELECT tree_json FROM snapshots WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if rows is None:
            return
        if json.loads(rows["tree_json"]) != _tree(self.workspace):
            raise ToolError("workspace conflict detected before side effect")

    def _write(self, path: str, content: str) -> Dict[str, Any]:
        self._assert_baseline()
        target = _path(self.workspace, path)
        before = _tree(self.workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._record_changes(before, _tree(self.workspace))
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def _delete(self, path: str) -> Dict[str, Any]:
        self._assert_baseline()
        target = _path(self.workspace, path)
        if not target.exists() or not target.is_file() or target.is_symlink():
            raise ToolError("delete_file accepts an existing regular file")
        before = _tree(self.workspace)
        target.unlink()
        self._record_changes(before, _tree(self.workspace))
        return {"path": path, "deleted": True}

    def _bash(self, command: str, cwd: str) -> Dict[str, Any]:
        if any(char in command for char in "\r\n|;&><`$"):
            raise ToolError("bash command rejected by allowlist")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ToolError("invalid shell syntax") from exc
        if not parts:
            raise ToolError("empty bash command")
        if not self._allowed_argv(parts):
            raise ToolError("bash command rejected by allowlist")
        self._assert_baseline()
        before = _tree(self.workspace)
        process = subprocess.Popen(
            parts,
            shell=False,
            cwd=str(_path(self.workspace, cwd or ".")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        stdout, stderr, timed_out = self._collect(process)
        self._record_changes(before, _tree(self.workspace))
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": process.returncode,
            "timed_out": timed_out,
        }

    def _allowed_argv(self, parts: List[str]) -> bool:
        for item in parts[1:]:
            if item.startswith("/") or item == ".." or item.startswith("../") or "/../" in item:
                return False
        if parts[0] in {"pwd", "ls", "find"}:
            return all(not item.startswith("-") or item in {"-v", "-t"} for item in parts[1:])
        if parts[0] in {"python", "python3"}:
            return len(parts) >= 3 and parts[1:3] in [["-m", "unittest"], ["-m", "pytest"]]
        if parts[0] == "pytest":
            return all(not item.startswith("-") or item in {"-v", "-q"} for item in parts[1:])
        if parts[0] == "git":
            return (
                len(parts) >= 2
                and parts[1] in {"status", "diff"}
                and all(
                    not item.startswith("-") or item in {"--", "--stat", "--check"}
                    for item in parts[2:]
                )
            )
        return False

    def _collect(self, process: subprocess.Popen[bytes]) -> tuple[str, str, bool]:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        timed_out = False
        deadline = __import__("time").monotonic() + self.timeout
        while selector.get_map():
            remaining = deadline - __import__("time").monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                remaining = 0.2
            for key, _ in selector.select(max(0.05, remaining)):
                chunk = key.fileobj.read(4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(
                    chunk[: max(0, self.MAX_OUTPUT_BYTES - len(buffers[key.data]))]
                )
            if process.poll() is not None and not selector.get_map():
                break
        process.wait()
        return (
            buffers["stdout"].decode("utf-8", errors="replace"),
            buffers["stderr"].decode("utf-8", errors="replace"),
            timed_out,
        )

    def _record_changes(self, before: Dict[str, str], after: Dict[str, str]) -> None:
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
            diff = "".join(
                difflib.unified_diff(
                    (old or "").splitlines(True),
                    (new or "").splitlines(True),
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
