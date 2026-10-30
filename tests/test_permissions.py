"""Tool permissions apply before execution and persist user decisions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.client import AssistantTurn, ToolCall
from agent.config import load_permissions
from agent.runtime import AgentRuntime, InjectedFault
from agent.storage import Store
from agent.tools import ToolRegistry
from tests.test_runtime import FakeModel, valid_plan


class PermissionsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'workspace'
        self.root.mkdir()
        self.store = Store(str(Path(directory.name) / 'state.sqlite3'))
        self.addCleanup(self.store.close)
        self.write = ToolCall('write', 'write_file', {'path': 'note.txt', 'content': 'hello'})

    def runtime(self, mode, prompt=None, calls=None):
        model = FakeModel([
            AssistantTurn('', calls or [self.write]), AssistantTurn('done', []),
        ])
        return AgentRuntime(model, self.store, str(self.root),
                            permissions={'write_file': mode}, permission_prompt=prompt)

    def test_allow_executes_without_prompt(self):
        prompt = unittest.mock.Mock(side_effect=AssertionError('unexpected prompt'))
        runtime = self.runtime('allow', prompt)
        runtime.start('write')
        self.assertEqual((self.root / 'note.txt').read_text(), 'hello')
        prompt.assert_not_called()

    def test_echo_executes_after_approval_and_is_blocked_on_refusal(self):
        for mode, answer in (("ask", True), ("ask", False), ("deny", True)):
            with self.subTest(mode=mode, answer=answer):
                prompt = unittest.mock.Mock(return_value=answer)
                model = FakeModel([
                    AssistantTurn('', [ToolCall('echo', 'bash', {'command': 'echo 123', 'cwd': '.'})]),
                    AssistantTurn('done', []),
                ])
                runtime = AgentRuntime(model, self.store, str(self.root),
                                       permissions={'bash': mode}, permission_prompt=prompt)
                if mode == 'ask' and answer:
                    runtime.start('echo')
                    result = json.loads(self.store.call(runtime.run_id, 'echo')['result_json'])
                    self.assertEqual(result['stdout'], '123\n')
                    self.assertEqual(result['exit_code'], 0)
                else:
                    with patch('agent.tools.subprocess.Popen') as process:
                        runtime.start('echo')
                        process.assert_not_called()
                    self.assertIn('permission denied', self.store.call(runtime.run_id, 'echo')['result_json'])
                self.assertEqual(prompt.call_count, 1 if mode == 'ask' else 0)

    def test_deny_is_a_tool_error_without_executor(self):
        runtime = self.runtime('deny')
        with patch.object(ToolRegistry, 'execute') as execute:
            self.assertEqual(runtime.start('write'), 'done')
            execute.assert_not_called()
        call = self.store.call(runtime.run_id, 'write')
        self.assertEqual(call['status'], 'failed')
        self.assertIn('permission denied', call['result_json'])
        self.assertFalse((self.root / 'note.txt').exists())

    def test_ask_records_yes_no_and_headless_denial(self):
        for answer in (True, False, None):
            with self.subTest(answer=answer):
                callback = None if answer is None else lambda *_: answer
                runtime = self.runtime('ask', callback)
                runtime.start('write')
                decision = json.loads(self.store.db.execute(
                    "SELECT value FROM decisions WHERE run_id=? AND kind='tool_permission'",
                    (runtime.run_id,),
                ).fetchone()[0])
                self.assertEqual(decision['accepted'], answer is True)
                self.assertEqual(decision['arguments'], self.write.arguments)
                self.assertEqual(self.store.call(runtime.run_id, 'write')['status'],
                                 'succeeded' if answer is True else 'failed')

    def test_successful_call_replay_does_not_prompt_again(self):
        prompt = unittest.mock.Mock(return_value=True)
        runtime = self.runtime('ask', prompt)
        runtime.client.turns = [AssistantTurn('', [self.write]),
                                AssistantTurn('', [self.write]), AssistantTurn('done', [])]
        runtime.start('write')
        prompt.assert_called_once_with('write_file', self.write.arguments)

    def test_plan_mode_blocks_changing_tools_before_prompt(self):
        for name, arguments in (
            ('write_file', self.write.arguments), ('delete_file', {'path': 'note.txt'}),
            ('bash', {'command': 'pwd'}), ('run_tests', {'runner': 'unittest'}),
        ):
            with self.subTest(tool=name):
                prompt = unittest.mock.Mock(return_value=True)
                model = FakeModel([AssistantTurn('', [ToolCall('blocked', name, arguments)]),
                                   AssistantTurn(json.dumps(valid_plan({'steps': [{'id': 'inspect'}]})), [])])
                runtime = AgentRuntime(model, self.store, str(self.root),
                                       permissions={name: 'ask'}, permission_prompt=prompt)
                with patch.object(ToolRegistry, 'execute') as execute:
                    runtime.start('plan', plan=True)
                    execute.assert_not_called()
                prompt.assert_not_called()

    def test_retry_uses_current_policy(self):
        runtime = self.runtime('allow')
        with patch.object(ToolRegistry, 'execute', side_effect=InjectedFault('crash')):
            with self.assertRaises(InjectedFault):
                runtime.start('write')
        resumed = AgentRuntime(FakeModel([AssistantTurn('done', [])]), self.store,
                               str(self.root), runtime.run_id, permissions={'write_file': 'deny'})
        self.assertIn('unknown', resumed.resume())
        with patch.object(ToolRegistry, 'execute') as execute:
            resumed.resolve('write', 'retry')
            execute.assert_not_called()
        self.assertFalse((self.root / 'note.txt').exists())

    def test_configuration_rejects_typos(self):
        config = self.root.parent / 'agent.toml'
        config.write_text('[permissions]\nwrite_file = "ask"\nbash = "deny"\n')
        values = load_permissions(str(config))
        self.assertEqual(values['write_file'], 'ask')
        self.assertEqual(values['bash'], 'deny')
        self.assertEqual(values['read_file'], 'allow')
        for entry in ('bash = "alow"', 'bas = "deny"', 'bash'):
            config.write_text('[permissions]\n' + entry)
            with self.assertRaises(ValueError):
                load_permissions(str(config))
