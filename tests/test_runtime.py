import tempfile
import unittest
from pathlib import Path

from agent.client import AssistantTurn, ToolCall
from agent.runtime import AgentRuntime
from agent.storage import Store


def valid_plan(value):
    result = {"goal": "inspect", "ready_when": "inspection completed", "risks": [], "assumptions": [], **value}
    result["steps"] = [{"description": "inspect", "tools": ["read_file"], **step} for step in value["steps"]]
    return result


class FakeModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((messages, tools))
        return self.turns.pop(0)


class RuntimeTest(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("old", encoding="utf-8")
        store = Store(str(root / "state.sqlite3"))
        return root, store

    def test_loop_executes_tool_and_finishes(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn("", [ToolCall("c1", "read_file", {"path": "a.txt"})]),
                AssistantTurn("done", []),
            ]
        )
        result = AgentRuntime(model, store, str(root)).start("read")
        self.assertEqual(result, "done")
        self.assertEqual(
            store.call(
                next(iter([r["run_id"] for r in store.db.execute("select run_id from runs")])), "c1"
            )["status"],
            "succeeded",
        )

    def test_plan_mode_blocks_side_effect(self):
        root, store = self.make()
        before = (root / "a.txt").read_text()
        model = FakeModel(
            [
                AssistantTurn(
                    "", [ToolCall("c1", "write_file", {"path": "a.txt", "content": "new"})]
                ),
                AssistantTurn(__import__("json").dumps(valid_plan({"steps": [{"id": "read"}]})), []),
            ]
        )
        text = AgentRuntime(model, store, str(root)).start("change", plan=True)
        self.assertEqual((root / "a.txt").read_text(), before)
        self.assertIn("steps", text)

    def test_rollback_restores_file(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    "", [ToolCall("c1", "write_file", {"path": "a.txt", "content": "new"})]
                ),
                AssistantTurn("done", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("change")
        self.assertEqual((root / "a.txt").read_text(), "new")
        runtime.rollback()
        self.assertEqual((root / "a.txt").read_text(), "old")

    def test_budget_stops_tool_batch(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    "",
                    [
                        ToolCall("a", "read_file", {"path": "a.txt"}),
                        ToolCall("b", "read_file", {"path": "a.txt"}),
                    ],
                )
            ]
        )
        with self.assertRaises(RuntimeError):
            AgentRuntime(
                model,
                store,
                str(root),
                limits={"model": 2, "tools": 1, "effects": 1, "result": 1000},
            ).start("x")


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTest(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("old", encoding="utf-8")
        store = Store(str(root / "state.sqlite3"))
        return root, store

    def test_batch_effect_budget_is_atomic(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    "",
                    [
                        ToolCall("a", "write_file", {"path": "a.txt", "content": "one"}),
                        ToolCall("b", "write_file", {"path": "b.txt", "content": "two"}),
                    ],
                )
            ]
        )
        with self.assertRaises(RuntimeError):
            AgentRuntime(
                model,
                store,
                str(root),
                limits={"model": 2, "tools": 2, "effects": 1, "result": 1000},
            ).start("x")
        self.assertEqual((root / "a.txt").read_text(), "old")
        self.assertFalse((root / "b.txt").exists())

    def test_protocol_error_for_changed_arguments(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn("", [ToolCall("c", "read_file", {"path": "a.txt"})]),
                AssistantTurn("done", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("x")
        with self.assertRaises(RuntimeError):
            runtime._execute_call(
                ToolCall("c", "read_file", {"path": "other.txt"}),
                ToolRegistryForTest(runtime),
                False,
            )

    def test_schema_error_becomes_tool_result(self):
        root, store = self.make()
        model = FakeModel(
            [AssistantTurn("", [ToolCall("c", "read_file", {})]), AssistantTurn("recovered", [])]
        )
        self.assertEqual(AgentRuntime(model, store, str(root)).start("x"), "recovered")
        row = store.db.execute(
            "select result_json from calls where call_id='c' order by attempt desc limit 1"
        ).fetchone()
        self.assertIn("missing required argument", row[0])

    def test_completed_resume_is_noop(self):
        root, store = self.make()
        model = FakeModel([AssistantTurn("done", [])])
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("x")
        self.assertIn("already finished", runtime.resume())
        self.assertEqual(len(model.seen), 1)

    def test_workspace_conflict_is_detected(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    "", [ToolCall("c", "write_file", {"path": "a.txt", "content": "new"})]
                ),
                AssistantTurn("done", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("x")
        (root / "a.txt").write_text("outside", encoding="utf-8")
        result = runtime._execute_call(
            ToolCall("d", "write_file", {"path": "a.txt", "content": "again"}),
            ToolRegistryForTest(runtime),
            False,
        )
        self.assertIn("workspace conflict", result["error"])
        self.assertEqual((root / "a.txt").read_text(), "outside")

    def test_external_change_before_first_write_is_detected(self):
        root, store = self.make()
        model = FakeModel([AssistantTurn("done", [])])
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("x")
        registry = ToolRegistryForTest(runtime)
        (root / "a.txt").write_text("outside", encoding="utf-8")
        result = runtime._execute_call(
            ToolCall("c", "write_file", {"path": "a.txt", "content": "new"}),
            registry,
            False,
        )
        self.assertIn("workspace conflict", result["error"])
        self.assertEqual((root / "a.txt").read_text(), "outside")

    def test_fault_point_creates_unknown_and_retry_executes(self):
        from unittest.mock import patch

        from agent.runtime import InjectedFault

        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    "", [ToolCall("c", "write_file", {"path": "a.txt", "content": "new"})]
                ),
                AssistantTurn("done", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        original_execute = runtime._execute_call

        def execute_then_interrupt(call, registry, plan_mode, force_retry=False):
            original_execute(call, registry, plan_mode, force_retry)
            store.db.execute(
                "UPDATE calls SET status='started', result_json=NULL WHERE run_id=? AND call_id=?",
                (runtime.run_id, call.id),
            )
            raise InjectedFault("fault injected after tool execution")

        with patch.object(runtime, "_execute_call", side_effect=execute_then_interrupt):
            with self.assertRaises(RuntimeError):
                runtime.start("x")
        self.assertEqual((root / "a.txt").read_text(), "new")
        self.assertEqual(store.call(runtime.run_id, "c")["status"], "started")
        self.assertIn("unknown calls detected", runtime.resume())
        self.assertEqual(runtime.resolve("c", "retry"), "done")

    def test_rejected_plan_cannot_be_approved(self):
        root, store = self.make()
        model = FakeModel(
            [
                AssistantTurn(
                    __import__("json").dumps(valid_plan({"steps": [{"id": "one", "description": "read", "depends_on": []}]})), []
                )
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        runtime.start("plan", plan=True)
        self.assertEqual(runtime.reject(), "plan rejected")
        with self.assertRaises(RuntimeError):
            runtime.approve()


class ToolRegistryForTest:
    def __init__(self, runtime):
        from agent.tools import ToolRegistry

        self.registry = ToolRegistry(runtime.workspace, runtime.store, runtime.run_id)

    def execute(self, *args, **kwargs):
        return self.registry.execute(*args, **kwargs)


class InteractiveSessionTest(unittest.TestCase):
    def test_session_keeps_same_run_and_history_after_final_answer(self):
        root = Path(tempfile.mkdtemp())
        store = Store(str(root / "state.sqlite3"))
        model = FakeModel([AssistantTurn("first", []), AssistantTurn("second", [])])
        runtime = AgentRuntime(model, store, str(root))
        self.assertEqual(runtime.start("one"), "first")
        self.assertEqual(runtime.continue_prompt("two"), "second")
        self.assertEqual(len(store.messages(runtime.run_id)), 5)
        self.assertEqual(len(model.seen), 2)


class DeleteFileTest(unittest.TestCase):
    def test_delete_file_is_journaled_and_rollback_restores_it(self):
        root = Path(tempfile.mkdtemp())
        target = root / "русский.txt"
        target.write_text("Привет", encoding="utf-8")
        store = Store(str(root / "state.sqlite3"))
        model = FakeModel(
            [
                AssistantTurn("", [ToolCall("delete", "delete_file", {"path": "русский.txt"})]),
                AssistantTurn("done", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        self.assertEqual(runtime.start("удали файл"), "done")
        self.assertFalse(target.exists())
        runtime.rollback()
        self.assertEqual(target.read_text(encoding="utf-8"), "Привет")


class PlanRetryTest(unittest.TestCase):
    def test_invalid_plan_is_sent_back_to_model_for_correction(self):
        root = Path(tempfile.mkdtemp())
        store = Store(str(root / "state.sqlite3"))
        invalid = {"goal": "x", "steps": [{"id": "same"}, {"id": "same"}]}
        valid = {"goal": "x", "steps": [{"id": "one", "description": "read", "depends_on": []}]}
        model = FakeModel(
            [
                AssistantTurn(__import__("json").dumps(valid_plan(invalid)), []),
                AssistantTurn(__import__("json").dumps(valid_plan(valid)), []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root))
        plan = runtime.start("plan", plan=True)
        self.assertIn('"id": "one"', plan)
        self.assertEqual(len(model.seen), 2)
        self.assertEqual(store.run(runtime.run_id)["status"], "waiting_approval")


class TaskExecutionTest(unittest.TestCase):
    def test_approved_plan_retries_text_only_response_and_completes_task(self):
        root = Path(tempfile.mkdtemp())
        (root / "existing.txt").write_text("x", encoding="utf-8")
        store = Store(str(root / "state.sqlite3"))
        import json

        plan = {
            "goal": "inspect",
            "steps": [{"id": "step_1", "description": "list", "depends_on": []}],
        }
        model = FakeModel(
            [
                AssistantTurn(json.dumps(valid_plan(plan)), []),
                AssistantTurn(json.dumps(valid_plan(plan)), []),
                AssistantTurn("", [ToolCall("list", "list_files", {})]),
                AssistantTurn("готово", []),
            ]
        )
        runtime = AgentRuntime(model, store, str(root), review=lambda *_: (True, ""))
        runtime.start("plan", plan=True)
        self.assertEqual(runtime.approve(), "готово")
        self.assertEqual(store.tasks(runtime.run_id)[0]["status"], "done")

    def test_task_review_rejection_returns_feedback_to_model(self):
        import json

        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("old", encoding="utf-8")
        store = Store(str(root / "state.sqlite3"))
        plan = {"goal": "inspect", "steps": [{"id": "step_1", "description": "read", "depends_on": []}]}
        model = FakeModel(
            [
                AssistantTurn(json.dumps(valid_plan(plan)), []),
                AssistantTurn("", [ToolCall("first", "read_file", {"path": "a.txt"})]),
                AssistantTurn("готово", []),
                AssistantTurn("", [ToolCall("second", "read_file", {"path": "a.txt"})]),
                AssistantTurn("готово после доработки", []),
            ]
        )
        reviews = iter([(False, "нужна доработка"), (True, "")])
        runtime = AgentRuntime(model, store, str(root), review=lambda _task, _result: next(reviews))
        runtime.start("plan", plan=True)
        self.assertEqual(runtime.approve(), "готово после доработки")
        task = store.tasks(runtime.run_id)[0]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["completed_call_id"], "second")
        self.assertTrue(
            any("нужна доработка" in message["content"] for message in store.messages(runtime.run_id))
        )
