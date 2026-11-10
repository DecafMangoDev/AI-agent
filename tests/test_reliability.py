"""Regression coverage for review fixes using isolated workspaces."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.client import AssistantTurn, ToolCall
from agent.runtime import AgentRuntime
from agent.storage import Store
from tests.test_runtime import FakeModel, valid_plan


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'workspace'
        self.root.mkdir()
        self.store = Store(str(Path(self.directory.name) / 'state.sqlite3'))
        self.addCleanup(self.store.close)

    def runtime(self, limits=None):
        model = FakeModel([
            AssistantTurn('', [ToolCall('c', 'write_file', {'path': 'a', 'content': 'new'})]),
            AssistantTurn('done', []),
        ])
        return AgentRuntime(model, self.store, str(self.root), limits=limits)

    def test_plan_repairs_format_and_accepts_registered_test_runner(self):
        def plan(tool, ready_when):
            return valid_plan({
                'steps': [{'id': 'verify', 'tools': [tool], 'depends_on': []}],
                'ready_when': ready_when,
            })

        model = FakeModel([
            AssistantTurn(json.dumps(plan('functions.run_tests', ['tests pass'])), []),
            AssistantTurn(json.dumps(plan('functions.run_tests', 'tests pass')), []),
            AssistantTurn(json.dumps(plan('run_tests', 'tests pass')), []),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root))
        result = json.loads(runtime.start('plan a test run', plan=True))
        self.assertEqual(result['steps'][0]['tools'], ['run_tests'])
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'waiting_approval')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)
        first_feedback = model.seen[1][0][-1]['content']
        self.assertIn('ready_when must be a nonempty string', first_feedback)
        second_feedback = model.seen[2][0][-1]['content']
        self.assertIn('exact registered tool names', second_feedback)
        self.assertIn('run_tests', second_feedback)
        self.assertIn('without functions. prefixes', second_feedback)

    def test_plan_rejects_unknown_names_and_embedded_calls(self):
        runtime = self.runtime()
        self.store.create_run(runtime.run_id, str(self.root))
        for tool in ('unknown', 'functions.run_tests', 'run_tests()', {'name': 'run_tests'}):
            with self.subTest(tool=tool):
                plan = valid_plan({'steps': [{'id': 'verify', 'tools': [tool]}]})
                with self.assertRaisesRegex(RuntimeError, 'exact registered tool names'):
                    runtime._validate_plan(plan)

    def test_single_attempt_and_binary_rollback(self):
        original = b'\xff\x00\r\n'
        (self.root / 'a').write_bytes(original)
        runtime = self.runtime()
        runtime.start('write')
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT attempt,status FROM calls')], [(1, 'succeeded')])
        runtime.rollback()
        self.assertEqual((self.root / 'a').read_bytes(), original)
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'rolled_back')

    def test_no_effect_without_result_budget(self):
        runtime = self.runtime({'model': 2, 'tools': 2, 'effects': 2, 'result': 0})
        with self.assertRaisesRegex(RuntimeError, 'result budget'):
            runtime.start('write')
        self.assertFalse((self.root / 'a').exists())

    def test_atomic_reservation_failure(self):
        runtime = self.runtime()
        self.store.create_run(runtime.run_id, str(self.root))
        self.store.set_budget(runtime.run_id, runtime.limits)
        entry = ('c', 'read_file', {'path': 'a'}, 1, None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve_batch(runtime.run_id, {'tools': 2}, [entry, entry])
        self.assertEqual(self.store.budget(runtime.run_id)['tool_used'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)

    def test_workspace_mismatch_rejected(self):
        runtime = self.runtime()
        runtime.start('write')
        with self.assertRaisesRegex(RuntimeError, 'workspace'):
            AgentRuntime(runtime.client, self.store, self.directory.name, runtime.run_id)

    def test_followup_cannot_approve_plan(self):
        model = FakeModel([AssistantTurn(__import__('json').dumps(valid_plan({'steps': [{'id': 'one'}]})), [])])
        runtime = AgentRuntime(model, self.store, str(self.root))
        runtime.start('plan', plan=True)
        with self.assertRaisesRegex(RuntimeError, 'approved'):
            runtime.continue_prompt('clarification')

    def test_execution_prompt_and_followup(self):
        import json
        plan = {'goal': 'inspect', 'steps': [{'id': 'one', 'description': 'read', 'tools': ['read_file'], 'depends_on': []}]}
        (self.root / 'a').write_text('old')
        model = FakeModel([
            AssistantTurn(json.dumps(valid_plan(plan)), []),
            AssistantTurn('', [ToolCall('read', 'read_file', {'path': 'a'})]),
            AssistantTurn('done', []),
            AssistantTurn('', [ToolCall('write', 'write_file', {'path': 'a', 'content': 'new'})]),
            AssistantTurn('fixed', []),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root), review=lambda *_: (True, ''))
        runtime.start('inspect', plan=True)
        self.assertEqual(runtime.approve(), 'done')
        self.assertNotIn('coding agent planner', model.seen[1][0][0]['content'])
        self.assertEqual(runtime.continue_prompt('change it'), 'fixed')
        self.assertEqual((self.root / 'a').read_text(), 'new')
        self.assertEqual(len(self.store.tasks(runtime.run_id)), 2)

    def test_completed_plan_allows_conversation_and_read_only_followup(self):
        (self.root / 'a').write_text('old')
        plan = valid_plan({'steps': [{'id': 'inspect', 'tools': ['read_file']}]})
        model = FakeModel([
            AssistantTurn(json.dumps(plan), []),
            AssistantTurn('', [ToolCall('inspect', 'read_file', {'path': 'a'})]),
            AssistantTurn('inspection done', []),
            AssistantTurn('I inspected a; no changes.', []),
            AssistantTurn('', [ToolCall('reread', 'read_file', {'path': 'a'})]),
            AssistantTurn('a contains old', []),
        ])
        reviews = []

        def review(task_id, payload):
            reviews.append(task_id)
            return True, ''

        runtime = AgentRuntime(model, self.store, str(self.root), review=review)
        runtime.start('inspect', plan=True)
        runtime.approve()
        accepted = dict(self.store.tasks(runtime.run_id)[0])
        restored = AgentRuntime(model, self.store, str(self.root), runtime.run_id, review=review)
        self.assertEqual(restored.continue_prompt('what changed?'), 'I inspected a; no changes.')
        self.assertEqual(restored.continue_prompt('show a'), 'a contains old')
        self.assertEqual([dict(t) for t in self.store.tasks(runtime.run_id)], [accepted])
        self.assertEqual(reviews, ['inspect'])
        self.assertIsNone(self.store.call(runtime.run_id, 'reread')['task_id'])
        for messages, _ in model.seen[3:]:
            self.assertFalse(any(m['role'] == 'system' and 'Task state:' in m['content'] for m in messages))

    def test_followup_write_still_requires_review_after_conversation(self):
        (self.root / 'a').write_text('old')
        plan = valid_plan({'steps': [{'id': 'inspect', 'tools': ['read_file']}]})
        model = FakeModel([
            AssistantTurn(json.dumps(plan), []),
            AssistantTurn('', [ToolCall('inspect', 'read_file', {'path': 'a'})]),
            AssistantTurn('done', []),
            AssistantTurn('No changes yet.', []),
            AssistantTurn('', [ToolCall('write', 'write_file', {'path': 'a', 'content': 'new'})]),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root), review=lambda *_: (True, ''))
        runtime.start('inspect', plan=True)
        runtime.approve()
        runtime.continue_prompt('what changed?')
        runtime.review = None
        self.assertEqual(runtime.continue_prompt('change a'), 'task result awaits explicit review')
        tasks = self.store.tasks(runtime.run_id)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]['status'], 'done')
        self.assertEqual(tasks[1]['description'], 'change a')
        self.assertEqual(self.store.call(runtime.run_id, 'write')['task_id'], tasks[1]['task_id'])
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'waiting_review')

    def test_empty_final_response_is_retried_and_bounded(self):
        model = FakeModel([AssistantTurn('', []), AssistantTurn('answer', [])])
        runtime = AgentRuntime(model, self.store, str(self.root))
        self.assertEqual(runtime.start('hello'), 'answer')
        self.assertIn('response was empty', model.seen[1][0][-1]['content'])
        bad = AgentRuntime(FakeModel([AssistantTurn('  ', []) for _ in range(4)]), self.store, str(self.root))
        with self.assertRaisesRegex(RuntimeError, 'empty final responses'):
            bad.start('hello')
        self.assertEqual(self.store.run(bad.run_id)['status'], 'failed')

    def test_invalid_python_is_tool_error_then_repaired(self):
        bad = 'def x():\\n    return 1'
        good = 'def x():\n    return 1\n'
        model = FakeModel([
            AssistantTurn('', [ToolCall('bad', 'write_file', {'path': 'x.py', 'content': bad})]),
            AssistantTurn('', [ToolCall('good', 'write_file', {'path': 'x.py', 'content': good})]),
            AssistantTurn('', [ToolCall('test', 'bash', {'command': 'python3 -m unittest -v'})]),
            AssistantTurn('done', []),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root))
        runtime.start('fix')
        self.assertEqual(self.store.call(runtime.run_id, 'bad')['status'], 'failed')
        self.assertEqual((self.root / 'x.py').read_text(), good)

    def test_headless_review_waits(self):
        import json
        plan = {'steps': [{'id': 'one', 'description': 'read', 'tools': ['read_file']}]}
        (self.root / 'a').write_text('old')
        model = FakeModel([AssistantTurn(json.dumps(valid_plan(plan)), []), AssistantTurn('', [ToolCall('r', 'read_file', {'path': 'a'})])])
        runtime = AgentRuntime(model, self.store, str(self.root))
        runtime.start('plan', plan=True)
        self.assertIn('awaits', runtime.approve())
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'waiting_review')
        self.assertNotEqual(self.store.tasks(runtime.run_id)[0]['status'], 'done')

    def test_user_database_is_journaled(self):
        from agent.tools import ToolRegistry
        runtime = self.runtime()
        runtime.start('write')
        registry = ToolRegistry(str(self.root), self.store, runtime.run_id)
        registry.execute('write_file', {'path': 'data.db', 'content': 'data'})
        runtime.rollback()
        self.assertFalse((self.root / 'data.db').exists())

    def test_cycle_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'cycle'):
            self.runtime()._validate_plan(valid_plan({'steps': [{'id': 'x', 'depends_on': ['x']}]}))

    def test_tdd_plan_reviews_failing_diagnostic_then_fix_and_passing_tests(self):
        original = (
            'import unittest\nfrom order import total\n'
            'class T(unittest.TestCase):\n'
            ' def test_total(self): self.assertEqual(total([100, 50], 10), 135)\n'
        )
        (self.root / 'test_order.py').write_text(original)
        (self.root / 'order.py').write_text('def total(p, d): return sum(p)\n')
        plan = valid_plan({'steps': [
            {'id': 'baseline', 'tools': ['run_tests'], 'depends_on': []},
            {'id': 'fix', 'tools': ['write_file'], 'depends_on': ['baseline']},
            {'id': 'verify', 'tools': ['run_tests'], 'depends_on': ['fix']},
        ]})
        model = FakeModel([
            AssistantTurn(json.dumps(plan), []),
            AssistantTurn('', [ToolCall('red', 'run_tests', {'runner': 'unittest'})]),
            AssistantTurn('', [ToolCall('fix', 'write_file', {
                'path': 'order.py', 'content': 'def total(p, d): return sum(p)*(1-d/100)\n',
            })]),
            AssistantTurn('', [ToolCall('green', 'run_tests', {'runner': 'unittest'})]),
            AssistantTurn('fixed', []),
        ])
        reviews = []

        def review(task_id, payload):
            reviews.append((task_id, payload['result']))
            return True, ''

        runtime = AgentRuntime(model, self.store, str(self.root), review=review)
        runtime.start('fix', plan=True)
        self.assertEqual(runtime.approve(), 'fixed')
        self.assertEqual([task_id for task_id, _ in reviews], ['baseline', 'fix', 'verify'])
        self.assertEqual(reviews[0][1]['exit_code'], 1)
        self.assertEqual(reviews[2][1]['exit_code'], 0)
        self.assertEqual(self.store.call(runtime.run_id, 'red')['status'], 'succeeded')
        self.assertEqual([task['status'] for task in self.store.tasks(runtime.run_id)], ['done'] * 3)
        self.assertEqual((self.root / 'test_order.py').read_text(), original)
        self.assertFalse(runtime._verification_needed())

    def test_failing_tests_do_not_verify_python_write(self):
        model = FakeModel([
            AssistantTurn('', [ToolCall('write', 'write_file', {
                'path': 'test_bad.py',
                'content': 'import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail()\n',
            })]),
            AssistantTurn('', [ToolCall('red', 'run_tests', {'runner': 'unittest'})]),
        ] + [AssistantTurn('done', []) for _ in range(4)])
        runtime = AgentRuntime(model, self.store, str(self.root))
        with self.assertRaisesRegex(RuntimeError, 'require successful tests'):
            runtime.start('write and test')
        self.assertEqual(self.store.call(runtime.run_id, 'red')['status'], 'succeeded')
        self.assertTrue(runtime._verification_needed())
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'failed')

    def test_failing_diagnostic_waits_for_explicit_review(self):
        (self.root / 'test_bad.py').write_text(
            'import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail()\n'
        )
        plan = valid_plan({'steps': [{'id': 'baseline', 'tools': ['run_tests']}]})
        model = FakeModel([
            AssistantTurn(json.dumps(plan), []),
            AssistantTurn('', [ToolCall('red', 'run_tests', {'runner': 'unittest'})]),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root))
        runtime.start('confirm failure', plan=True)
        self.assertEqual(runtime.approve(), 'task result awaits explicit review')
        self.assertEqual(self.store.run(runtime.run_id)['status'], 'waiting_review')
        self.assertNotEqual(self.store.tasks(runtime.run_id)[0]['status'], 'done')

    def test_three_dependent_tasks_fix_and_verify(self):
        import json
        original = 'import unittest\nfrom order import total\nclass T(unittest.TestCase):\n def test_total(self):\n  self.assertEqual(total([100,50],10),135)\n'
        (self.root / 'test_order.py').write_text(original)
        (self.root / 'order.py').write_text('def total(p,d):\n return sum(p)-d\n')
        plan = {'goal': 'fix', 'steps': [
            {'id': 'inspect', 'description': 'read', 'tools': ['read_file'], 'depends_on': []},
            {'id': 'fix', 'description': 'fix', 'tools': ['write_file'], 'depends_on': ['inspect']},
            {'id': 'test', 'description': 'test', 'tools': ['bash'], 'depends_on': ['fix']},
        ]}
        model = FakeModel([
            AssistantTurn(json.dumps(valid_plan(plan)), []),
            AssistantTurn('', [ToolCall('r', 'read_file', {'path': 'order.py'})]),
            AssistantTurn('', [ToolCall('w', 'write_file', {'path': 'order.py', 'content': 'def total(p,d):\n return sum(p)*(1-d/100)\n'})]),
            AssistantTurn('', [ToolCall('t', 'bash', {'command': 'python3 -m unittest -v'})]),
            AssistantTurn('fixed', []),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root), review=lambda *_: (True, ''))
        runtime.start('fix', plan=True)
        self.assertEqual(runtime.approve(), 'fixed')
        self.assertEqual([t['status'] for t in self.store.tasks(runtime.run_id)], ['done'] * 3)
        self.assertEqual((self.root / 'test_order.py').read_text(), original)
        restored = AgentRuntime(FakeModel([]), self.store, str(self.root), runtime.run_id)
        self.assertIn('already finished', restored.resume())
        restored.rollback()
        self.assertIn('sum(p)-d', (self.root / 'order.py').read_text())

    def test_real_crash_requires_attribution(self):
        from unittest.mock import patch

        from agent.runtime import InjectedFault
        from agent.tools import ToolRegistry
        runtime = self.runtime()
        with patch.object(ToolRegistry, '_record_changes', side_effect=InjectedFault('after write')):
            with self.assertRaises(InjectedFault):
                runtime.start('write')
        self.assertIn('unknown', runtime.resume())
        self.assertIn('unknown', runtime.resume())
        with self.assertRaisesRegex(RuntimeError, 'attribution'):
            runtime.resolve('c', 'fail')
        runtime.resolve('c', 'fail', accept_changes=True)
        runtime.rollback()
        self.assertFalse((self.root / 'a').exists())

    def test_partial_output_timeout_is_bounded(self):
        import time

        from agent.tools import ToolRegistry
        (self.root / 'test_slow.py').write_text('import time\nprint("ready", flush=True)\ntime.sleep(5)\n')
        runtime = self.runtime()
        runtime.start('write')
        registry = ToolRegistry(str(self.root), self.store, runtime.run_id, timeout=0.1)
        start = time.monotonic()
        result = registry.execute('bash', {'command': 'python3 -m unittest -v'})
        self.assertTrue(result['timed_out'])
        self.assertLess(time.monotonic() - start, 2)

    def test_plaintext_tool_description_is_not_executed(self):
        model = FakeModel([
            AssistantTurn('{"name":"write_file","arguments":{"path":"a","content":"wrong"}}', []),
            AssistantTurn('', [ToolCall('actual', 'write_file', {'path': 'a', 'content': 'right'})]),
            AssistantTurn('done', []),
        ])
        runtime = AgentRuntime(model, self.store, str(self.root))
        runtime.start('write')
        self.assertEqual((self.root / 'a').read_text(), 'right')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 1)

    def test_incomplete_plan_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'goal'):
            self.runtime()._validate_plan({'steps': [{'id': 'x'}]})
