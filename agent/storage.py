"""SQLite persistence for runs, messages, calls, budgets and changes."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, plan_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_call_id TEXT, name TEXT, payload_json TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS calls (run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL, args_json TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, result_json TEXT, task_id TEXT, PRIMARY KEY(run_id, call_id, attempt));
CREATE TABLE IF NOT EXISTS budgets (run_id TEXT PRIMARY KEY, model_limit INTEGER, model_used INTEGER NOT NULL, tool_limit INTEGER, tool_used INTEGER NOT NULL, effect_limit INTEGER, effect_used INTEGER NOT NULL, result_limit INTEGER, result_used INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (run_id TEXT NOT NULL, task_id TEXT NOT NULL, description TEXT NOT NULL, deps_json TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT, PRIMARY KEY(run_id, task_id));
CREATE TABLE IF NOT EXISTS snapshots (run_id TEXT NOT NULL, seq INTEGER NOT NULL, tree_json TEXT NOT NULL, PRIMARY KEY(run_id, seq));
CREATE TABLE IF NOT EXISTS changes (run_id TEXT NOT NULL, path TEXT NOT NULL, original TEXT, final TEXT, original_hash TEXT, final_hash TEXT, diff TEXT, PRIMARY KEY(run_id, path));
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, created_at REAL NOT NULL);
"""


class Store:
    def __init__(self, path: str = ".agent/state.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(calls)")}
        if "task_id" not in columns:
            self.db.execute("ALTER TABLE calls ADD COLUMN task_id TEXT")

    def close(self) -> None:
        self.db.close()

    def create_run(self, run_id: str, workspace: str, phase: str = "planning") -> None:
        self.db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
            (run_id, workspace, phase, "active", time.time(), None, None),
        )

    def runs(self) -> List[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT run_id, workspace, phase, status, created_at, error FROM runs ORDER BY created_at DESC"
            )
        )

    def recent_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        """Return resumable chats for the interactive selector."""
        return list(
            self.db.execute(
                "SELECT run_id, workspace, phase, status, created_at, error "
                "FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def run(self, run_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        keys = list(fields)
        self.db.execute(
            "UPDATE runs SET " + ",".join(k + "=?" for k in keys) + " WHERE run_id=?",
            [fields[k] for k in keys] + [run_id],
        )

    def add_message(
        self,
        run_id: str,
        role: str,
        payload: Dict[str, Any],
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO messages(run_id,role,content,tool_call_id,name,payload_json) VALUES(?,?,?,?,?,?)",
            (
                run_id,
                role,
                payload.get("content"),
                tool_call_id,
                name,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    def messages(self, run_id: str) -> List[Dict[str, Any]]:
        return [
            json.loads(r["payload_json"])
            for r in self.db.execute(
                "SELECT payload_json FROM messages WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]

    def set_budget(self, run_id: str, limits: Dict[str, int]) -> None:
        self.db.execute(
            "INSERT INTO budgets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                limits["model"],
                0,
                limits["tools"],
                0,
                limits["effects"],
                0,
                limits["result"],
                0,
            ),
        )

    def budget(self, run_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM budgets WHERE run_id=?", (run_id,)).fetchone()

    def reserve(self, run_id: str, kind: str, amount: int = 1) -> None:
        columns = {"model": "model", "tools": "tool", "effects": "effect", "result": "result"}
        prefix = columns[kind]
        with self.db:
            row = self.budget(run_id)
            used = row[prefix + "_used"]
            limit = row[prefix + "_limit"]
            if used + amount > limit:
                raise RuntimeError("budget exceeded: " + kind)
            self.db.execute(
                "UPDATE budgets SET {}_used={}_used+? WHERE run_id=?".format(prefix, prefix),
                (amount, run_id),
            )

    def reserve_batch(
        self, run_id: str, amounts: Dict[str, int], starts: Optional[List[tuple]] = None
    ) -> None:
        columns = {"model": "model", "tools": "tool", "effects": "effect", "result": "result"}
        with self.db:
            row = self.budget(run_id)
            for kind, amount in amounts.items():
                prefix = columns[kind]
                if row[prefix + "_used"] + amount > row[prefix + "_limit"]:
                    raise RuntimeError("budget exceeded: " + kind)
            for kind, amount in amounts.items():
                prefix = columns[kind]
                self.db.execute(
                    "UPDATE budgets SET {}_used={}_used+? WHERE run_id=?".format(prefix, prefix),
                    (amount, run_id),
                )
            for call_id, name, args, attempt, task_id in starts or []:
                self.db.execute(
                    "INSERT INTO calls(run_id,call_id,name,args_json,status,attempt,result_json,task_id) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        call_id,
                        name,
                        json.dumps(args, sort_keys=True, ensure_ascii=False),
                        "started",
                        attempt,
                        None,
                        task_id,
                    ),
                )

    def record_call(
        self,
        run_id: str,
        call_id: str,
        name: str,
        args: Dict[str, Any],
        status: str,
        attempt: int = 1,
        result: Any = None,
        task_id: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO calls(run_id,call_id,name,args_json,status,attempt,result_json,task_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                call_id,
                name,
                json.dumps(args, sort_keys=True, ensure_ascii=False),
                status,
                attempt,
                None if result is None else json.dumps(result, ensure_ascii=False),
                task_id,
            ),
        )

    def call(self, run_id: str, call_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM calls WHERE run_id=? AND call_id=? ORDER BY attempt DESC LIMIT 1",
            (run_id, call_id),
        ).fetchone()

    def add_decision(self, run_id: str, kind: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO decisions(run_id,kind,value,created_at) VALUES(?,?,?,?)",
            (run_id, kind, value, time.time()),
        )

    def save_plan(self, run_id: str, plan: Dict[str, Any]) -> None:
        self.update_run(run_id, plan_json=json.dumps(plan, ensure_ascii=False))

    def plan(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.run(run_id)
        return None if not row or not row["plan_json"] else json.loads(row["plan_json"])

    def add_task(self, run_id: str, task_id: str, description: str, deps: Iterable[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?)",
            (run_id, task_id, description, json.dumps(list(deps)), "pending", None),
        )

    def tasks(self, run_id: str) -> List[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,))
        )

    def task_status(self, run_id: str, task_id: str, status: str, evidence: Any = None) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, evidence_json=? WHERE run_id=? AND task_id=?",
            (status, None if evidence is None else json.dumps(evidence), run_id, task_id),
        )
