"""Agent loop, planning, budgets and recovery orchestration."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Dict, Optional

from .client import ModelClient, ToolCall
from .storage import Store
from .tools import ToolError, ToolRegistry, _hash, _path

SYSTEM = """Ты coding agent. Исследуй workspace инструментами. Не меняй тесты. Для каждого шага используй инструменты, а когда задача проверена, ответь кратко. Пути должны быть относительными workspace."""
PLAN_SYSTEM = """Ты планировщик coding agent. Используй только инструменты чтения. В финальном ответе верни JSON с полями goal, steps (массив объектов {id, description, tools, depends_on}), risks, assumptions, ready_when."""
DEFAULT_LIMITS = {"model": 20, "tools": 40, "effects": 15, "result": 120000}


class InjectedFault(RuntimeError):
    """Deterministic fault point used by recovery tests."""


class AgentRuntime:
    def __init__(
        self,
        client: ModelClient,
        store: Store,
        workspace: str,
        run_id: Optional[str] = None,
        limits: Optional[Dict[str, int]] = None,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.store = store
        self.workspace = workspace
        self.run_id = run_id or uuid.uuid4().hex
        self.limits = limits or DEFAULT_LIMITS.copy()
        self.progress = progress or (lambda _message: None)

    def _progress(self, message: str) -> None:
        """Publish a short event for interactive clients without coupling core to stdout."""
        self.progress(message)

    def start(self, prompt: str, plan: bool = False) -> str:
        self._progress("[run {}] запуск{}".format(self.run_id[:8], " планирования" if plan else ""))
        self.store.create_run(self.run_id, self.workspace, "planning" if plan else "executing")
        self.store.set_budget(self.run_id, self.limits)
        self.store.add_message(
            self.run_id, "system", {"role": "system", "content": PLAN_SYSTEM if plan else SYSTEM}
        )
        self.store.add_message(self.run_id, "user", {"role": "user", "content": prompt})
        return self._planning() if plan else self._safe_loop(False)

    def continue_prompt(self, prompt: str) -> str:
        row = self.store.run(self.run_id)
        if not row:
            raise RuntimeError("run is not initialized")
        if row["status"] in {"rejected", "failed"}:
            raise RuntimeError("cannot continue a {} run".format(row["status"]))
        self.store.update_run(self.run_id, status="active", phase="executing", error=None)
        self.store.add_message(self.run_id, "user", {"role": "user", "content": prompt})
        self._progress("[run {}] продолжаю запрос".format(self.run_id[:8]))
        return self._safe_loop(False)

    def reject(self) -> str:
        row = self.store.run(self.run_id)
        if not row or row["status"] != "waiting_approval":
            raise RuntimeError("run is not waiting for approval")
        self.store.add_decision(self.run_id, "plan", "rejected")
        self.store.update_run(self.run_id, phase="rejected", status="rejected")
        return "plan rejected"

    def approve(self) -> str:
        row = self.store.run(self.run_id)
        if not row or row["status"] != "waiting_approval":
            raise RuntimeError("run is not waiting for approval")
        plan = self.store.plan(self.run_id) or {}
        self._validate_plan(plan)
        self.store.add_decision(self.run_id, "plan", "approved")
        self.store.update_run(self.run_id, phase="executing", status="approved")
        for step in plan["steps"]:
            self.store.add_task(
                self.run_id, step["id"], step["description"], step.get("depends_on", [])
            )
        self.store.add_message(
            self.run_id, "user", {"role": "user", "content": "План утверждён. Выполняй его."}
        )
        return self._safe_loop(False)

    def _validate_plan(self, plan: Dict[str, Any]) -> None:
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RuntimeError("plan must contain steps")
        ids = [step.get("id") for step in steps]
        if any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids):
            raise RuntimeError("plan task IDs must be unique strings")
        known = set(ids)
        if any(dep not in known for step in steps for dep in step.get("depends_on", [])):
            raise RuntimeError("plan contains unknown dependency")

    def _safe_loop(self, plan_mode: bool) -> str:
        try:
            return self._loop(plan_mode)
        except Exception as exc:
            self.store.update_run(self.run_id, status="failed", error=str(exc))
            raise

    def _planning(self) -> str:
        last_error = None
        for attempt in range(3):
            result = self._safe_loop(True)
            plan = self._parse_plan(result)
            try:
                self._validate_plan(plan)
            except RuntimeError as exc:
                last_error = str(exc)
                if attempt == 2:
                    raise RuntimeError(
                        "модель не смогла выдать корректный план после 3 попыток: " + last_error
                    )
                self.store.add_message(
                    self.run_id,
                    "user",
                    {
                        "role": "user",
                        "content": (
                            "План отклонён runtime: {}. Верни исправленный JSON-план. "
                            "Каждый шаг обязан иметь уникальный строковый id, а depends_on "
                            "должен ссылаться только на существующие id."
                        ).format(last_error),
                    },
                )
                continue
            self.store.save_plan(self.run_id, plan)
            self.store.update_run(self.run_id, phase="planning", status="waiting_approval")
            self._progress("[run {}] план готов: {} задач; ожидается approve".format(self.run_id[:8], len(plan["steps"])))
            return json.dumps(plan, ensure_ascii=False, indent=2)
        raise RuntimeError("план не сформирован: " + str(last_error))

    def _parse_plan(self, text: str) -> Dict[str, Any]:
        try:
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get("steps"), list):
                return value
        except ValueError:
            pass
        return {
            "goal": "Выполнить запрос пользователя",
            "steps": [
                {
                    "id": "execute",
                    "description": text,
                    "tools": ["read_file", "write_file", "bash"],
                    "depends_on": [],
                }
            ],
            "risks": [],
            "assumptions": [],
            "ready_when": "Тесты проходят",
        }

    def _loop(self, plan_mode: bool) -> str:
        registry = ToolRegistry(self.workspace, self.store, self.run_id)
        idle_turns = 0
        while True:
            self.store.reserve(self.run_id, "model")
            self._progress("[run {}] запрос к модели (лимит использован)".format(self.run_id[:8]))
            turn = self.client.complete(self.store.messages(self.run_id), registry.definitions())
            assistant: Dict[str, Any] = {"role": "assistant", "content": turn.content}
            if turn.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for call in turn.tool_calls
                ]
            self.store.add_message(self.run_id, "assistant", assistant)
            if not turn.tool_calls:
                pending = [
                    task for task in self.store.tasks(self.run_id) if task["status"] != "done"
                ]
                if not plan_mode and pending:
                    idle_turns += 1
                    if idle_turns > 3:
                        raise RuntimeError("модель не начала выполнять незавершённые задачи")
                    self.store.add_message(
                        self.run_id,
                        "user",
                        {
                            "role": "user",
                            "content": (
                                "У тебя есть незавершённые задачи: {}. Не выводи план и не отвечай текстом. "
                                "Сейчас вызови подходящий инструмент для первой доступной задачи."
                            ).format(", ".join(task["task_id"] for task in pending)),
                        },
                    )
                    continue
                self.store.update_run(
                    self.run_id, status="completed", phase="planning" if plan_mode else "done"
                )
                return turn.content
            effects = sum(
                call.name in {"write_file", "delete_file", "bash"} for call in turn.tool_calls
            )
            starts = []
            for call in turn.tool_calls:
                prior = self.store.call(self.run_id, call.id)
                canonical = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
                if prior and (prior["name"] != call.name or prior["args_json"] != canonical):
                    raise ToolError("protocol error: tool_call_id arguments changed")
                if prior and prior["status"] == "unknown":
                    raise ToolError("unknown tool call requires explicit resolve")
                if not prior or prior["status"] != "succeeded":
                    task_id = prior["task_id"] if prior else self._current_task_id()
                    if self.store.tasks(self.run_id) and task_id is None:
                        raise ToolError("task dependencies are not satisfied")
                    starts.append(
                        (
                            call.id,
                            call.name,
                            call.arguments,
                            1 if not prior else int(prior["attempt"]) + 1,
                            task_id,
                        )
                    )
            self.store.reserve_batch(
                self.run_id, {"tools": len(turn.tool_calls), "effects": effects}, starts
            )
            for call in turn.tool_calls:
                self._progress("[run {}] инструмент: {}".format(self.run_id[:8], call.name))
                result = self._execute_call(call, registry, plan_mode)
                self.store.add_message(
                    self.run_id,
                    "tool",
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, ensure_ascii=False),
                    },
                    call.id,
                    call.name,
                )

    def _execute_call(
        self, call: ToolCall, registry: ToolRegistry, plan_mode: bool, force_retry: bool = False
    ) -> Dict[str, Any]:
        prior = self.store.call(self.run_id, call.id)
        canonical = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        if prior:
            if prior["name"] != call.name or prior["args_json"] != canonical:
                raise ToolError("protocol error: tool_call_id arguments changed")
            if prior["status"] == "succeeded" and not force_retry:
                return json.loads(prior["result_json"])
            if prior["status"] == "unknown" and not force_retry:
                raise ToolError("unknown tool call requires explicit resolve")
        attempt = 1 if not prior else int(prior["attempt"]) + 1
        task_id = prior["task_id"] if prior else self._current_task_id()
        if self.store.tasks(self.run_id) and task_id is None:
            raise ToolError("task dependencies are not satisfied")
        already_started = bool(prior and prior["status"] == "started" and not force_retry)
        if not already_started:
            self.store.record_call(
                self.run_id, call.id, call.name, call.arguments, "started", attempt, task_id=task_id
            )
        try:
            result = registry.execute(call.name, call.arguments, plan_mode)
        except InjectedFault:
            raise
        except (ToolError, OSError, UnicodeError, ValueError, TypeError) as exc:
            result = {"error": str(exc)}
            status = "failed"
        else:
            status = "succeeded"
        fault_id = os.environ.get("AGENT_FAULT_AFTER_TOOL_ID")
        if fault_id and fault_id == call.id:
            raise InjectedFault("fault injected after tool execution")
        result = self._fit_result(result)
        self.store.record_call(
            self.run_id,
            call.id,
            call.name,
            call.arguments,
            status,
            attempt,
            result,
            task_id,
        )
        if task_id and status == "succeeded":
            self.store.task_status(
                self.run_id, task_id, "done", {"tool_call_id": call.id, "result": result}
            )
            self._progress("[run {}] задача {} отмечена выполненной".format(self.run_id[:8], task_id))
        self._progress("[run {}] {}: {}".format(self.run_id[:8], call.name, status))
        return result

    def _current_task_id(self) -> Optional[str]:
        for task in self.store.tasks(self.run_id):
            if task["status"] != "pending":
                continue
            dependencies = json.loads(task["deps_json"])
            statuses = {row["task_id"]: row["status"] for row in self.store.tasks(self.run_id)}
            if all(statuses.get(dep) == "done" for dep in dependencies):
                return task["task_id"]
        return None

    def _fit_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        row = self.store.budget(self.run_id)
        remaining = row["result_limit"] - row["result_used"]
        if len(encoded) <= remaining:
            self.store.reserve(self.run_id, "result", len(encoded))
            return result
        for size in range(min(remaining, 10000), -1, -1):
            candidate = {
                "truncated": True,
                "content": json.dumps(result, ensure_ascii=False)[:size],
            }
            encoded = json.dumps(candidate, ensure_ascii=False).encode("utf-8")
            if len(encoded) <= remaining:
                self.store.reserve(self.run_id, "result", len(encoded))
                return candidate
        raise RuntimeError("result budget exhausted")

    def resume(self) -> str:
        row = self.store.run(self.run_id)
        if not row:
            raise RuntimeError("unknown run")
        if row["status"] in {"completed", "rejected"}:
            return "run already finished: " + row["status"]
        if row["status"] == "waiting_approval":
            plan = self.store.plan(self.run_id)
            return json.dumps(plan, ensure_ascii=False, indent=2) if plan else "план ожидает approve"
        for call in self.store.db.execute(
            "SELECT * FROM calls WHERE run_id=? AND status='started'", (self.run_id,)
        ):
            self.store.db.execute(
                "UPDATE calls SET status='unknown' WHERE run_id=? AND call_id=? AND attempt=?",
                (self.run_id, call["call_id"], call["attempt"]),
            )
        self._progress("[run {}] восстановление состояния".format(self.run_id[:8]))
        return self._safe_loop(row["phase"] == "planning")

    def resolve(self, call_id: str, action: str) -> str:
        row = self.store.call(self.run_id, call_id)
        if not row or row["status"] != "unknown":
            raise RuntimeError("call is not unknown")
        if action == "abort":
            self.store.update_run(self.run_id, status="failed", error="user aborted unknown call")
            return "aborted"
        if action == "fail":
            result = {"error": "user marked failed"}
        else:
            registry = ToolRegistry(self.workspace, self.store, self.run_id)
            self.store.reserve_batch(
                self.run_id,
                {"tools": 1, "effects": int(row["name"] in {"write_file", "delete_file", "bash"})},
            )
            result = self._execute_call(
                ToolCall(call_id, row["name"], json.loads(row["args_json"])), registry, False, True
            )
        self.store.add_message(
            self.run_id,
            "tool",
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": row["name"],
                "content": json.dumps(result, ensure_ascii=False),
            },
            call_id,
            row["name"],
        )
        return self._safe_loop(False)

    def rollback(self) -> None:
        rows = list(self.store.db.execute("SELECT * FROM changes WHERE run_id=?", (self.run_id,)))
        from .tools import _tree

        current_tree = _tree(self.workspace)
        for row in rows:
            current = current_tree.get(row["path"])
            if (None if current is None else _hash(current)) != row["final_hash"]:
                raise RuntimeError("rollback conflict: " + row["path"])
        for row in rows:
            path = _path(self.workspace, row["path"])
            if path.exists() or path.is_symlink():
                path.unlink()
            original = row["original"]
            if original is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if original.startswith("<symlink:") and original.endswith(">"):
                path.symlink_to(original[len("<symlink:") : -1])
            else:
                path.write_text(original, encoding="utf-8")
