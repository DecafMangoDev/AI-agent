"""Agent loop, planning, budgets and recovery orchestration."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import ModelClient, ToolCall
from .config import validate_permissions
from .permissions import select_permission, validate_rules
from .storage import Store
from .tools import ToolError, ToolRegistry, _hash, _path

SYSTEM = """You are a general-purpose assistant. Answer ordinary questions and hold a conversation directly.
Use workspace tools only when the user asks to work with code, files, commands, or the project.
Do not inspect the workspace for greetings or general questions. Do not change tests.
Paths must be relative to the workspace. Execute actions using actual tool_calls, never JSON/XML in text.
Use real newline characters in file content, not literal backslash-n separators.
After changing code, run the project's tests through bash. A write result does not prove correctness.
Only report completion after checking results; describe failures honestly."""
PLAN_SYSTEM = """You are a coding agent planner. Use read-only tools only. Return final JSON with goal, steps (objects with id, description, tools, depends_on), risks, assumptions, and ready_when. tools must be a list of tool names, never embedded arguments or source code. For code changes include a final dependent bash verification step. ready_when must include passing tests, not merely writing a file."""
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
        review: Optional[Callable[[str, Dict[str, Any]], Tuple[bool, str]]] = None,
        permissions: Optional[Dict[str, str]] = None,
        permission_prompt: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        permission_rules: Optional[List[Dict[str, str]]] = None,
    ):
        self.client = client
        self.store = store
        self.workspace = str(Path(workspace).resolve())
        saved = store.run(run_id) if run_id else None
        if saved and str(Path(saved["workspace"]).resolve()) != self.workspace:
            raise ToolError("workspace does not match saved run")
        self.run_id = run_id or uuid.uuid4().hex
        self.limits = limits or DEFAULT_LIMITS.copy()
        self.progress = progress or (lambda _message: None)
        self.review = review
        self.permissions = validate_permissions(permissions or {})
        self.permission_prompt = permission_prompt
        self.permission_rules = validate_rules(permission_rules or [])

    def _authorize_tool(self, call: ToolCall, attempt: int, plan_mode: bool) -> None:
        if plan_mode and call.name in {"write_file", "delete_file", "bash", "run_tests"}:
            raise ToolError("side effects are forbidden before plan approval")
        mode, rule_index = select_permission(
            call.name, call.arguments, self.permissions.get(call.name, "deny"),
            self.permission_rules, self.workspace,
        )
        accepted = mode == "allow"
        if mode == "ask" and self.permission_prompt is not None:
            # The UI receives a copy so it cannot change the arguments being authorized.
            accepted = self.permission_prompt(call.name, json.loads(json.dumps(call.arguments))) is True
        self.store.add_decision(self.run_id, "tool_permission", json.dumps({
            "call_id": call.id, "attempt": attempt, "tool": call.name,
            "arguments": call.arguments, "policy": mode, "accepted": accepted,
            "rule": self.permission_rules[rule_index - 1] if rule_index else None,
        }, ensure_ascii=False))
        if not accepted:
            reason = "ask requires an interactive approval callback" if mode == "ask" and self.permission_prompt is None else "execution denied"
            raise ToolError("permission denied for {}: {}".format(call.name, reason))

    def _progress(self, message: str) -> None:
        """Publish a short event for interactive clients without coupling core to stdout."""
        self.progress(message)

    def _budget_remaining(self) -> str:
        """Format the persisted remaining budget for human-readable progress events."""
        row = self.store.budget(self.run_id)
        if row is None:
            return "budget is not initialized"
        return "model={}, tools={}, effects={}, result={} bytes".format(
            row["model_limit"] - row["model_used"],
            row["tool_limit"] - row["tool_used"],
            row["effect_limit"] - row["effect_used"],
            row["result_limit"] - row["result_used"],
        )

    def _review_candidate(self, task: Any) -> bool:
        """Ask the interactive client to accept the latest successful task result."""
        task_id = task["task_id"]
        call = self.store.call(self.run_id, task["last_call_id"])
        result = json.loads(call["result_json"]) if call and call["result_json"] else {}
        review_payload = {
            "tool": call["name"] if call else "unknown",
            "arguments": json.loads(call["args_json"]) if call else {},
            "result": result,
        }
        if self.review is None:
            return False
        review_payload["description"] = task["description"]
        accepted, comment = self.review(task_id, review_payload)
        self.store.add_decision(
            self.run_id,
            "task_review",
            json.dumps(
                {"task_id": task_id, "call_id": task["last_call_id"], "accepted": accepted, "comment": comment},
                ensure_ascii=False,
            ),
        )
        self.store.task_review(self.run_id, task_id, accepted, comment)
        if accepted:
            self._progress("[run {}] task {} accepted by user".format(self.run_id[:8], task_id))
            self.store.update_run(self.run_id, status="active", error=None)
            return True
        self.store.add_message(
            self.run_id,
            "user",
            {
                "role": "user",
                "content": "Task {} was rejected by the user: {}. Rework the task.".format(
                    task_id, comment or "further work is needed"
                ),
            },
        )
        self._progress("[run {}] task {} sent back for rework".format(self.run_id[:8], task_id))
        self.store.update_run(self.run_id, status="active", error=None)
        return False

    def start(self, prompt: str, plan: bool = False) -> str:
        self._progress("[run {}] starting{}".format(self.run_id[:8], " planning" if plan else ""))
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
        if row["phase"] == "planning":
            raise ToolError("plan must be explicitly approved before execution")
        if row["status"] in {"rejected", "failed", "aborted", "rolled_back"}:
            raise RuntimeError("cannot continue a {} run".format(row["status"]))
        if self._unresolved():
            raise ToolError("resolve interrupted calls before continuing")
        if self.store.tasks(self.run_id) and all(t["status"] == "done" for t in self.store.tasks(self.run_id)):
            self.store.add_task(self.run_id, "followup_" + uuid.uuid4().hex[:8], prompt, [])
        self.store.update_run(self.run_id, status="active", phase="executing", error=None)
        self.store.add_message(self.run_id, "user", {"role": "user", "content": prompt})
        self._progress("[run {}] continuing request".format(self.run_id[:8]))
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
        with self.store.transaction():
            self.store.add_decision(self.run_id, "plan", "approved")
            self.store.update_run(self.run_id, phase="executing", status="approved")
            for step in plan["steps"]:
                self.store.add_task(
                    self.run_id, step["id"], step["description"], step.get("depends_on", [])
                )
            self.store.add_message(
                self.run_id, "user", {"role": "user", "content": "Plan approved. Execute it."}
            )
        return self._safe_loop(False)

    def _validate_plan(self, plan: Dict[str, Any]) -> None:
        for key in ("goal", "ready_when"):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                raise ToolError("plan requires nonempty " + key)
        for key in ("risks", "assumptions"):
            if not isinstance(plan.get(key), list) or any(not isinstance(x, str) for x in plan[key]):
                raise ToolError("plan requires a list of strings: " + key)
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RuntimeError("plan must contain steps")
        if any(not isinstance(step, dict) for step in steps):
            raise ToolError("each step must be an object")
        ids = [step.get("id") for step in steps]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise RuntimeError("plan task IDs must be unique strings")
        for step in steps:
            deps = step.get("depends_on", [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
                raise ToolError("depends_on must be a list of task IDs")
        allowed = {"list_files", "read_file", "write_file", "delete_file", "bash"}
        for step in steps:
            if not isinstance(step.get("description"), str) or not step["description"].strip():
                raise ToolError("task description must be text")
            tools = step.get("tools")
            if not isinstance(tools, list) or any(not isinstance(tool, str) or tool not in allowed for tool in tools):
                raise ToolError("step tools must contain registered tool names, not embedded calls")
        known = set(ids)
        if any(dep not in known for step in steps for dep in step.get("depends_on", [])):
            raise RuntimeError("plan contains unknown dependency")
        graph = {step["id"]: step.get("depends_on", []) for step in steps}
        completed = set()
        while len(completed) < len(graph):
            ready = {key for key, deps in graph.items() if key not in completed and all(dep in completed for dep in deps)}
            if not ready:
                raise ToolError("plan dependency cycle")
            completed.update(ready)


    def _safe_loop(self, plan_mode: bool) -> str:
        try:
            return self._loop(plan_mode)
        except (RuntimeError, OSError, ValueError) as exc:
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
                        "the model could not produce a valid plan after 3 attempts: " + last_error
                    )
                self.store.add_message(
                    self.run_id,
                    "user",
                    {
                        "role": "user",
                        "content": (
                            "Plan rejected by the runtime: {}. Return corrected plan JSON. "
                            "Each step must have a unique string id, and depends_on must "
                            "reference existing ids."
                        ).format(last_error),
                    },
                )
                continue
            with self.store.transaction():
                self.store.save_plan(self.run_id, plan)
                self.store.update_run(self.run_id, phase="planning", status="waiting_approval")
            self._progress("[run {}] plan ready: {} tasks; approval required".format(self.run_id[:8], len(plan["steps"])))
            return json.dumps(plan, ensure_ascii=False, indent=2)
        raise RuntimeError("plan was not created: " + str(last_error))

    def _parse_plan(self, text: str) -> Dict[str, Any]:
        try:
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get("steps"), list):
                return value
        except ValueError:
            pass
        return {}

    def _verification_needed(self) -> bool:
        calls = list(self.store.db.execute("SELECT rowid AS sequence,* FROM calls WHERE run_id=? ORDER BY rowid", (self.run_id,)))
        changed = 0
        checked = 0
        for call in calls:
            if call["status"] != "succeeded":
                continue
            args = json.loads(call["args_json"])
            if call["name"] in {"write_file", "delete_file"} and args.get("path", "").endswith(".py"):
                changed = call["sequence"]
            if (call["name"] == "run_tests" and args.get("runner") in {"unittest", "pytest"}) or (call["name"] == "bash" and any(x in args.get("command", "") for x in ("unittest", "pytest"))):
                checked = call["sequence"]
        return changed > checked

    def _loop(self, plan_mode: bool) -> str:
        registry = ToolRegistry(self.workspace, self.store, self.run_id)
        idle_turns = 0
        while True:
            if not plan_mode and self._verification_needed() and self.store.tasks(self.run_id) and all(t["status"] == "done" for t in self.store.tasks(self.run_id)):
                self.store.add_task(self.run_id, "verify_" + uuid.uuid4().hex[:8], "Run project tests using bash and confirm they pass", [])
            self.store.reserve(self.run_id, "model")
            self._progress(
                "[run {}] model request; remaining: {}".format(
                    self.run_id[:8], self._budget_remaining()
                )
            )
            messages = self.store.messages(self.run_id)
            # Phase instructions are derived from persisted state, never inherited from an old plan.
            messages = [m for m in messages if m["role"] != "system"]
            messages.insert(0, {"role": "system", "content": PLAN_SYSTEM if plan_mode else SYSTEM})
            if not plan_mode and self.store.tasks(self.run_id):
                messages.insert(1, {"role": "system", "content": "Task state: " + json.dumps([
                    {"id": t["task_id"], "description": t["description"], "status": t["status"]}
                    for t in self.store.tasks(self.run_id)
                ])})
            turn = self.client.complete(messages, registry.definitions())
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
                if not plan_mode and any(marker in turn.content for marker in ('"write_file"', '"arguments"', '<tool_call', '"action"')):
                    idle_turns += 1
                    if idle_turns > 3:
                        raise ToolError("model returned tool descriptions instead of tool_calls")
                    self.store.add_message(self.run_id, "user", {"role": "user", "content": "No action was executed. Use actual tool_calls, not JSON/XML/code blocks in text."})
                    continue
                if not plan_mode and self._verification_needed():
                    idle_turns += 1
                    if idle_turns > 3:
                        raise ToolError("code changes require successful tests before completion")
                    self.store.add_message(self.run_id, "user", {"role": "user", "content": "Code changed but tests have not passed since the last write. Call bash to run the project tests; fix failures before finishing."})
                    continue
                candidate = self.store.task_candidate(self.run_id) if not plan_mode else None
                if candidate is not None:
                    self.store.update_run(self.run_id, status="waiting_review", error=None)
                    if self.review is None:
                        return "task result awaits explicit review"
                    if not self._review_candidate(candidate):
                        continue
                pending = [
                    task for task in self.store.tasks(self.run_id) if task["status"] != "done"
                ]
                if not plan_mode and pending:
                    idle_turns += 1
                    if idle_turns > 3:
                        raise RuntimeError("the model did not start the pending tasks")
                    self.store.add_message(
                        self.run_id,
                        "user",
                        {
                            "role": "user",
                            "content": (
                                "Pending tasks: {}. Do not return a plan or text. "
                                "Call a suitable tool for the first available task now."
                            ).format(", ".join(task["task_id"] for task in pending)),
                        },
                    )
                    continue
                self.store.update_run(
                    self.run_id, status="active" if plan_mode else "completed", phase="planning" if plan_mode else "done"
                )
                return turn.content
            if len({c.id for c in turn.tool_calls}) != len(turn.tool_calls):
                raise ToolError("duplicate tool_call_id in response")
            effects = sum(
                call.name in {"write_file", "delete_file", "bash", "run_tests"} for call in turn.tool_calls
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
            budget = self.store.budget(self.run_id)
            if budget["result_limit"] - budget["result_used"] < 36 * len(starts):
                raise ToolError("result budget exhausted before batch execution")
            self.store.reserve_batch(
                self.run_id, {"tools": len(turn.tool_calls), "effects": effects}, starts
            )
            for call in turn.tool_calls:
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
            if not plan_mode:
                candidate = self.store.task_candidate(self.run_id)
                if candidate is not None:
                    self.store.update_run(self.run_id, status="waiting_review", error=None)
                    if self.review is None:
                        return "task result awaits explicit review"
                    self._review_candidate(candidate)

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
        if already_started:
            attempt = int(prior["attempt"])
        if not already_started:
            self.store.record_call(
                self.run_id, call.id, call.name, call.arguments, "started", attempt, task_id=task_id
            )
        arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
        self._progress(
            "[run {}] tool: {}({}); remaining: {}".format(
                self.run_id[:8], call.name, arguments[:500], self._budget_remaining()
            )
        )
        budget = self.store.budget(self.run_id)
        if hasattr(registry, "MAX_READ_BYTES"):
            registry.MAX_READ_BYTES = min(registry.MAX_READ_BYTES, max(0, budget["result_limit"] - budget["result_used"] - 100))
            registry.MAX_OUTPUT_BYTES = min(registry.MAX_OUTPUT_BYTES, max(0, (budget["result_limit"] - budget["result_used"] - 100) // 2))
        if budget["result_limit"] - budget["result_used"] < 36:
            raise ToolError("result budget exhausted before execution")
        try:
            self._authorize_tool(call, attempt, plan_mode)
            result = registry.execute(call.name, call.arguments, plan_mode)
        except InjectedFault:
            raise
        except (ToolError, OSError, UnicodeError, ValueError, TypeError) as exc:
            result = {"error": str(exc)}
            status = "failed"
        else:
            status = "failed" if result.get("timed_out") or result.get("exit_code", 0) != 0 else "succeeded"
        with self.store.transaction():
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
            eligible = True
            plan = self.store.plan(self.run_id) or {}
            step = next((t for t in plan.get("steps", []) if t.get("id") == task_id), {})
            tool_names = [t if isinstance(t, str) else t.get("name") for t in step.get("tools", [])]
            if "bash" in tool_names or "run_tests" in tool_names or (task_id and task_id.startswith("verify_")):
                eligible = call.name in {"bash", "run_tests"}
                eligible = call.name == "bash"
            if task_id and status == "succeeded" and eligible:
                self.store.task_progress(self.run_id, task_id, call.id)
                self._progress("[run {}] task {} is awaiting review".format(self.run_id[:8], task_id))
        result_preview = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(result_preview) > 800:
            result_preview = result_preview[:800] + "…"
        self._progress(
            "[run {}] {}: {}; result: {}; remaining: {}".format(
                self.run_id[:8], call.name, status, result_preview, self._budget_remaining()
            )
        )
        return result

    def _current_task_id(self) -> Optional[str]:
        for task in self.store.tasks(self.run_id):
            if task["status"] not in {"pending", "running"}:
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

    def _unresolved(self):
        return list(self.store.db.execute(
            "SELECT * FROM calls c WHERE run_id=? AND status IN ('started','unknown') "
            "AND attempt=(SELECT MAX(attempt) FROM calls x WHERE x.run_id=c.run_id AND x.call_id=c.call_id)",
            (self.run_id,),
        ))

    def _repair_history(self):
        messages = self.store.messages(self.run_id)
        answered = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
        for message in messages:
            for item in message.get("tool_calls", []):
                if item["id"] in answered:
                    continue
                call = self.store.call(self.run_id, item["id"])
                if call and call["result_json"]:
                    self.store.add_message(self.run_id, "tool", {
                        "role": "tool", "tool_call_id": item["id"], "name": call["name"],
                        "content": call["result_json"],
                    }, item["id"], call["name"])
                elif call is None:
                    # The response was saved before reservation: no executor could have run.
                    self.store.add_message(self.run_id, "tool", {
                        "role": "tool", "tool_call_id": item["id"],
                        "content": '{"error":"interrupted before execution; issue a new call"}',
                    }, item["id"])

    def resume(self) -> str:
        row = self.store.run(self.run_id)
        if not row:
            raise RuntimeError("unknown run")
        if row["status"] in {"completed", "rejected", "aborted", "rolled_back"}:
            return "run already finished: " + row["status"]
        if row["status"] == "waiting_approval":
            plan = self.store.plan(self.run_id)
            return json.dumps(plan, ensure_ascii=False, indent=2) if plan else "plan is awaiting approval"
        self._repair_history()
        if row["status"] == "waiting_review":
            if self.review is None:
                return "task result awaits explicit review"
            candidate = self.store.task_candidate(self.run_id)
            if candidate is not None and not self._review_candidate(candidate):
                return self._safe_loop(False)
        unknown_ids = []
        for call in self.store.db.execute(
            "SELECT * FROM calls c WHERE run_id=? AND status IN ('started','unknown') AND attempt=(SELECT MAX(attempt) FROM calls latest WHERE latest.run_id=c.run_id AND latest.call_id=c.call_id)", (self.run_id,)
        ):
            self.store.db.execute(
                "UPDATE calls SET status='unknown' WHERE run_id=? AND call_id=? AND attempt=?",
                (self.run_id, call["call_id"], call["attempt"]),
            )
            if call["call_id"] not in unknown_ids:
                unknown_ids.append(call["call_id"])
        if unknown_ids:
            return "unknown calls detected: {}. Choose retry, fail, or abort.".format(
                ", ".join(unknown_ids)
            )
        self._progress("[run {}] recovering state".format(self.run_id[:8]))
        return self._planning() if row["phase"] == "planning" else self._safe_loop(False)

    def resolve(self, call_id: str, action: str, accept_changes: bool = False) -> str:
        if action not in {"retry", "fail", "abort"}:
            raise ToolError("invalid resolution")
        row = self.store.call(self.run_id, call_id)
        if not row or row["status"] != "unknown":
            raise RuntimeError("call is not unknown")
        from .tools import _tree
        registry = ToolRegistry(self.workspace, self.store, self.run_id)
        snapshot = self.store.db.execute("SELECT tree_json FROM snapshots WHERE run_id=? ORDER BY seq DESC LIMIT 1", (self.run_id,)).fetchone()
        before = json.loads(snapshot["tree_json"])
        after = _tree(self.workspace, self.store.path)
        if before != after and action != "abort":
            paths = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
            if not accept_changes:
                import difflib

                from .tools import _diff_lines
                diff = "".join("".join(difflib.unified_diff(_diff_lines(before.get(path, "")), _diff_lines(after.get(path, "")), fromfile=path, tofile=path)) for path in paths)
                raise ToolError("unknown workspace changes require explicit attribution: " + ", ".join(paths) + "\n" + diff)
            with self.store.transaction():
                registry._record_changes(before, after)
                self.store.add_decision(self.run_id, "attribution", json.dumps(paths))
        if action == "retry" and row["name"] == "bash":
            raise ToolError("bash is not safely repeatable; choose fail or abort")
        self.store.add_decision(self.run_id, "resolution", json.dumps({"call_id": call_id, "action": action}))
        if action == "abort":
            self.store.update_run(self.run_id, status="aborted", error="user aborted unknown call")
            return "aborted"
        if action == "fail":
            result = self._fit_result({"error": "user marked failed"})
            self.store.record_call(self.run_id, call_id, row["name"], json.loads(row["args_json"]), "failed", row["attempt"], result, row["task_id"])
        else:
            registry = ToolRegistry(self.workspace, self.store, self.run_id)
            self.store.reserve_batch(
                self.run_id,
                {"tools": 1, "effects": int(row["name"] in {"write_file", "delete_file", "bash"})},
                [(call_id, row["name"], json.loads(row["args_json"]), row["attempt"] + 1, row["task_id"])],
            )
            result = self._execute_call(
                ToolCall(call_id, row["name"], json.loads(row["args_json"])), registry, self.store.run(self.run_id)["phase"] == "planning", False
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
        return self.resume()

    def rollback(self) -> None:
        if self._unresolved():
            raise ToolError("resolve unknown calls before rollback")
        rows = list(self.store.db.execute("SELECT * FROM changes WHERE run_id=?", (self.run_id,)))
        from .tools import _tree

        current_tree = _tree(self.workspace, self.store.path)
        for row in rows:
            target = Path(self.workspace) / row["path"]
            _path(self.workspace, str(target.parent.relative_to(self.workspace)))
            if row["original"] is not None and not row["original"].startswith("<symlink:"):
                from .tools import _decode_file
                _decode_file(row["original"])
            if target.is_dir():
                raise ToolError("rollback conflict: directory at " + row["path"])
            current = current_tree.get(row["path"])
            current_hash = None if current is None else _hash(current)
            if current_hash != row["final_hash"]:
                raise RuntimeError(
                    "rollback conflict at {}: expected {}, found {}".format(
                        row["path"], row["final_hash"] or "<missing>", current_hash or "<missing>"
                    )
                )
        for row in rows:
            path = Path(self.workspace) / row["path"]
            if path.exists() or path.is_symlink():
                path.unlink()
            original = row["original"]
            if original is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if original.startswith("<symlink:") and original.endswith(">"):
                path.symlink_to(original[len("<symlink:") : -1])
            else:
                from .tools import _decode_file
                path.write_bytes(_decode_file(original))

        self.store.add_decision(self.run_id, "rollback", "completed")
        self.store.update_run(self.run_id, status="rolled_back")
