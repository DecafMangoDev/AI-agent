"""Argument rules cannot turn a textual prefix into arbitrary shell access."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent.client import AssistantTurn, ToolCall
from agent.config import load_permission_rules, load_permissions
from agent.permissions import select_permission, validate_rules
from agent.runtime import AgentRuntime
from agent.storage import Store
from tests.test_runtime import FakeModel


class PermissionRulesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'workspace'
        self.root.mkdir()

    def select(self, args, rules, tool='bash', default='ask'):
        return select_permission(tool, args, default, validate_rules(rules), str(self.root))[0]

    def test_prefix_matches_argv_not_text_and_rejects_shell(self):
        rule = [{'tool': 'bash', 'mode': 'allow', 'command_prefix': 'python3 -m pytest'}]
        for command in ('python3 -m pytest', 'python3   -m "pytest" -v'):
            self.assertEqual(self.select({'command': command}, rule), 'allow')
        for command in ('python3 -m pytest_bad', 'python3 -m unittest', 'python3 -c pass'):
            self.assertEqual(self.select({'command': command}, rule), 'ask')
        for command in ('python3 -m pytest && pwd', 'python3 -m pytest; pwd',
                        'python3 -m pytest > out', 'python3 -m pytest $(pwd)',
                        'python3 -m pytest | ls', 'python3 -m pytest\npwd'):
            self.assertEqual(self.select({'command': command}, rule), 'deny')

    def test_exact_command_cwd_and_priority(self):
        rule = {'tool': 'bash', 'mode': 'allow', 'command': 'python3 --version', 'cwd': '.'}
        for cwd in ('', '.', './'):
            self.assertEqual(self.select({'command': 'python3 --version', 'cwd': cwd}, [rule]), 'allow')
        self.assertEqual(self.select({'command': 'python3 --version -V'}, [rule]), 'ask')
        self.assertEqual(self.select({'command': 'python3 --version', 'cwd': 'src'}, [rule]), 'ask')
        ask = {'tool': 'bash', 'mode': 'ask', 'command_prefix': 'python3'}
        deny = {'tool': 'bash', 'mode': 'deny', 'command_prefix': 'python3'}
        args = {'command': 'python3 --version'}
        self.assertEqual(self.select(args, [rule, ask]), 'ask')
        for rules in ([rule, ask, deny], [deny, ask, rule]):
            self.assertEqual(self.select(args, rules), 'deny')
        self.assertEqual(self.select(args, [rule], default='deny'), 'allow')

    def test_path_filters_use_canonical_workspace_paths(self):
        (self.root / 'private').mkdir()
        (self.root / 'alias').symlink_to(self.root / 'private', target_is_directory=True)
        rules = [{'tool': 'write_file', 'mode': 'deny', 'path': 'private/*'}]
        for path in ('private/key', './private/key', 'other/../private/key', 'alias/key'):
            self.assertEqual(self.select({'path': path}, rules, 'write_file', 'allow'), 'deny')
        self.assertEqual(self.select({'path': 'public/key'}, rules, 'write_file', 'allow'), 'allow')
        self.assertEqual(self.select({'path': '../key'}, [], 'write_file', 'allow'), 'deny')
        self.assertEqual(self.select({'path': str(self.root / 'a')}, [], 'write_file', 'allow'), 'deny')
        rules = [{'tool': 'run_tests', 'mode': 'ask', 'runner': 'pytest', 'path': 'src/tests/*'}]
        self.assertEqual(self.select({'runner': 'pytest', 'cwd': 'src', 'path': 'tests/test_a.py'}, rules, 'run_tests', 'allow'), 'ask')

    def test_config_comments_and_invalid_rules(self):
        path = self.root.parent / 'config.toml'
        path.write_text('''[permissions]
bash = "deny"
[[permission_rules]]
tool = "bash"
mode = "allow" # comment
command_prefix = 'python3 -m pytest'
[[permission_rules]]
tool = "read_file"
mode = "deny"
path = "private/#key"
''')
        self.assertEqual(load_permissions(str(path))['bash'], 'deny')
        rules = load_permission_rules(str(path))
        self.assertEqual(rules[1]['path'], 'private/#key')
        for entry in ('tool = "unknown"\nmode = "allow"\npath = "*"',
                      'tool = "bash"\nmode = "alow"\ncommand = "pwd"',
                      'tool = "bash"\nmode = "allow"\ncommmand = "pwd"',
                      'tool = "bash"\nmode = "allow"\ncommand = "pwd"\ncommand = "ls"',
                      'tool = "bash"\nmode = "allow"'):
            path.write_text('[[permission_rules]]\n' + entry)
            with self.assertRaises(ValueError):
                load_permission_rules(str(path))

    def test_runtime_enforces_rules_and_records_matched_rule(self):
        store = Store(str(self.root.parent / 'state.sqlite3'))
        self.addCleanup(store.close)
        rule = {'tool': 'bash', 'mode': 'deny', 'command_prefix': 'git'}
        model = FakeModel([AssistantTurn('', [ToolCall('c', 'bash', {'command': 'git diff'})]),
                           AssistantTurn('done', [])])
        prompt = Mock(return_value=True)
        runtime = AgentRuntime(model, store, str(self.root), permissions={'bash': 'allow'},
                               permission_rules=[rule], permission_prompt=prompt)
        with patch('agent.tools.subprocess.Popen') as process:
            runtime.start('show diff')
            process.assert_not_called()
        prompt.assert_not_called()
        self.assertIn('permission denied', store.call(runtime.run_id, 'c')['result_json'])
        decision = json.loads(store.db.execute("SELECT value FROM decisions WHERE kind='tool_permission'").fetchone()[0])
        self.assertEqual(decision['rule'], rule)

    def test_allow_rule_executes_command_without_second_allowlist(self):
        store = Store(str(self.root.parent / 'state.sqlite3'))
        self.addCleanup(store.close)
        model = FakeModel([AssistantTurn('', [ToolCall('c', 'bash', {'command': 'python3 -c pass'})]),
                           AssistantTurn('done', [])])
        runtime = AgentRuntime(model, store, str(self.root), permissions={'bash': 'deny'},
                               permission_rules=[{'tool': 'bash', 'mode': 'allow', 'command_prefix': 'python3'}])
        runtime.start('run')
        result = json.loads(store.call(runtime.run_id, 'c')['result_json'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(store.call(runtime.run_id, 'c')['status'], 'succeeded')

    def test_actual_allowed_command_and_argument_specific_ask(self):
        for mode in ('allow', 'ask'):
            with self.subTest(mode=mode):
                store = Store(str(self.root.parent / (mode + '.sqlite3')))
                self.addCleanup(store.close)
                model = FakeModel([AssistantTurn('', [ToolCall('v', 'bash', {'command': 'python3 --version'})]),
                                   AssistantTurn('done', [])])
                prompt = Mock(return_value=True)
                runtime = AgentRuntime(model, store, str(self.root), permissions={'bash': 'deny'},
                                       permission_rules=[{'tool': 'bash', 'mode': mode, 'command': 'python3 --version'}],
                                       permission_prompt=prompt)
                runtime.start('version')
                result = json.loads(store.call(runtime.run_id, 'v')['result_json'])
                self.assertEqual(result['exit_code'], 0)
                self.assertIn('Python 3.', result['stdout'] + result['stderr'])
                self.assertEqual(prompt.call_count, 1 if mode == 'ask' else 0)
