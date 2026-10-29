"""Explicitly permitted diagnostic commands and their execution boundaries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.storage import Store
from agent.tools import ToolError, ToolRegistry


class BashAllowlistTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'workspace'
        self.root.mkdir()
        self.store = Store(str(Path(directory.name) / 'state.sqlite3'))
        self.addCleanup(self.store.close)
        self.store.create_run('diagnostics', str(self.root))
        self.registry = ToolRegistry(str(self.root), self.store, 'diagnostics')

    def test_exact_python_version_forms(self):
        for executable in ('python', 'python3'):
            for flag in ('--version', '-V'):
                with self.subTest(executable=executable, flag=flag):
                    self.assertTrue(self.registry._allowed_argv([executable, flag]))
                    self.assertFalse(self.registry._allowed_argv([executable, flag, 'script.py']))
                    self.assertFalse(self.registry._allowed_argv([executable, flag, '-c', 'pass']))
        self.assertFalse(self.registry._allowed_argv(['python3', '-m', 'unittest', '--version']))

    def test_version_and_listing_execute_through_bash(self):
        (self.root / '.example').write_text('fixture')
        # Rebaseline after fixture creation, before any command.
        self.store.db.execute('DELETE FROM snapshots')
        self.registry = ToolRegistry(str(self.root), self.store, 'diagnostics')
        for command in ('python3 --version', 'python3 -V', 'ls -la'):
            with self.subTest(command=command):
                result = self.registry.execute('bash', {'command': command})
                self.assertEqual(result['exit_code'], 0, result)
                output = result['stdout'] + result['stderr']
                self.assertIn('.example' if command.startswith('ls') else 'Python 3.', output)
                self.assertFalse(result['timed_out'])

    def test_unsafe_commands_rejected_before_process_creation(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (self.root / 'external').symlink_to(outside, target_is_directory=True)
        commands = (
            'python3 --version && pwd', 'python3 -V; pwd',
            'python3 --version > version.txt', 'python3 -V | ls',
            'python3 $(pwd)', 'python3 `pwd`', 'python3 -c pass',
            'ls -la ../outside', 'ls -la /tmp', 'ls -la external/',
        )
        with patch('agent.tools.subprocess.Popen') as spawn:
            for command in commands:
                with self.subTest(command=command), self.assertRaises(ToolError):
                    self.registry.execute('bash', {'command': command})
            spawn.assert_not_called()

    def test_diagnostics_remain_blocked_in_plan_mode(self):
        with patch('agent.tools.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ToolError, 'before plan approval'):
                self.registry.execute('bash', {'command': 'python3 --version'}, plan_mode=True)
            spawn.assert_not_called()

class RunTestsToolTest(BashAllowlistTest):
    def test_run_tests_uses_python3_and_reports_test_failure(self):
        (self.root / 'test_example.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n')
        self.store.db.execute('DELETE FROM snapshots')
        self.registry = ToolRegistry(str(self.root), self.store, 'diagnostics')
        result = self.registry.execute('run_tests', {'runner': 'unittest', 'path': 'discover'})
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('Ran 1 test', result['stderr'])
        (self.root / 'test_example.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("expected")\n')
        self.store.db.execute('DELETE FROM snapshots')
        self.registry = ToolRegistry(str(self.root), self.store, 'diagnostics')
        result = self.registry.execute('run_tests', {'runner': 'unittest', 'path': 'discover'})
        self.assertEqual(result['exit_code'], 1)
        self.assertIn('expected', result['stderr'])

    def test_run_tests_rejects_unknown_runner_and_bad_paths(self):
        with self.assertRaises(ToolError):
            self.registry.execute('run_tests', {'runner': 'bash'})
        with self.assertRaises(ToolError):
            self.registry.execute('run_tests', {'runner': 'pytest', 'path': '../outside'})
        with self.assertRaises(ToolError):
            self.registry.execute('run_tests', {'runner': 'pytest', 'path': 'missing'})
