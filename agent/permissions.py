"""Small argument-aware permission rules; execution validation remains separate."""
from __future__ import annotations

import shlex
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MATCH_FIELDS = {
    'bash': {'command', 'command_prefix', 'cwd'},
    'run_tests': {'runner', 'path', 'cwd'},
    'list_files': {'path'}, 'read_file': {'path'},
    'write_file': {'path'}, 'delete_file': {'path'},
}
MODES = {'deny': 0, 'ask': 1, 'allow': 2}


def command_tokens(command: str) -> List[str]:
    if any(char in command for char in '\r\n|;&><`$'):
        raise ValueError('compound commands and shell expansion are forbidden')
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError('empty command')
    return tokens


def validate_rules(rules: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result = []
    for rule in rules:
        if not isinstance(rule, dict) or any(not isinstance(v, str) for v in rule.values()):
            raise ValueError('permission rule fields must be strings')
        tool = rule.get('tool', '')
        if tool not in MATCH_FIELDS or rule.get('mode') not in MODES:
            raise ValueError('permission rule requires a known tool and ask/allow/deny mode')
        fields = set(rule) - {'tool', 'mode'}
        if not fields or fields - MATCH_FIELDS[tool]:
            raise ValueError('invalid argument filters for permission rule: ' + tool)
        if 'command' in fields and 'command_prefix' in fields:
            raise ValueError('use command or command_prefix, not both')
        for field in fields & {'command', 'command_prefix'}:
            command_tokens(rule[field])
        result.append(dict(rule))
    return result


def _path_value(workspace: str, value: str, cwd: str = '.') -> str:
    if Path(value).is_absolute() or Path(cwd).is_absolute():
        raise ValueError('permission paths must be relative')
    root = Path(workspace).resolve()
    path = (root / cwd / (value or '.')).resolve()
    return path.relative_to(root).as_posix()


def select_permission(
    tool: str, arguments: Dict[str, Any], default: str,
    rules: List[Dict[str, str]], workspace: str,
) -> Tuple[str, Optional[int]]:
    """Match all filters; deny wins over ask, then allow. Indices are one-based."""
    candidates = []
    values = dict(arguments)
    try:
        if tool == 'bash':
            tokens = command_tokens(values.get('command', ''))
        if tool in {'bash', 'run_tests'}:
            values['cwd'] = _path_value(workspace, values.get('cwd', '.'))
        if 'path' in MATCH_FIELDS.get(tool, set()):
            # run_tests interprets its target relative to cwd; file tools use workspace.
            values['path'] = _path_value(workspace, values.get('path', '.'),
                                         arguments.get('cwd', '.') if tool == 'run_tests' else '.')
    except (ValueError, TypeError, OSError):
        return 'deny', None
    for index, rule in enumerate(rules, 1):
        if rule['tool'] != tool:
            continue
        matches = True
        for key, pattern in rule.items():
            if key in {'tool', 'mode'}:
                continue
            if key in {'command', 'command_prefix'}:
                expected = command_tokens(pattern)
                matches = tokens == expected if key == 'command' else tokens[:len(expected)] == expected
            elif key in {'path', 'cwd'}:
                matches = fnmatchcase(values.get(key, ''), pattern)
            else:
                matches = values.get(key) == pattern
            if not matches:
                break
        if matches:
            candidates.append((MODES[rule['mode']], index, rule['mode']))
    if not candidates:
        return default, None
    _, index, mode = min(candidates)
    return mode, index
