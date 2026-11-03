"""Configuration loaded from a small, human-editable TOML-like file."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from .permissions import validate_rules

DEFAULT_LIMITS = {"model": 20, "tools": 40, "effects": 15, "result": 120000}
DEFAULT_PERMISSIONS = {
    "list_files": "allow", "read_file": "allow", "write_file": "allow",
    "delete_file": "allow", "bash": "allow", "run_tests": "allow",
}


def validate_permissions(overrides: Dict[str, str]) -> Dict[str, str]:
    permissions = DEFAULT_PERMISSIONS.copy()
    for name, mode in overrides.items():
        if name not in permissions:
            raise ValueError("unknown tool in permissions: " + name)
        if mode not in {"ask", "allow", "deny"}:
            raise ValueError("permission for {} must be ask, allow or deny".format(name))
        permissions[name] = mode
    return permissions


def load_permissions(path: str = "agent.toml") -> Dict[str, str]:
    """Load the optional [permissions] section of the project's small config format."""
    config_path = Path(path)
    values: Dict[str, str] = {}
    if config_path.is_file():
        section = False
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                section = line == "[permissions]"
            elif section and line:
                if "=" not in line:
                    raise ValueError("invalid permission entry: " + line)
                name, value = (item.strip() for item in line.split("=", 1))
                values[name] = value.strip("\"'")
    return validate_permissions(values)


def _without_comment(line: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index].strip()
    return line.strip()


def load_permission_rules(path: str = "agent.toml") -> List[Dict[str, str]]:
    """Read repeated [[permission_rules]] with single-line quoted string fields."""
    config_path = Path(path)
    rules: List[Dict[str, str]] = []
    current = None
    if not config_path.is_file():
        return rules
    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = _without_comment(raw)
        if not line:
            continue
        if line.startswith("["):
            current = None
            if line == "[[permission_rules]]":
                current = {}
                rules.append(current)
            elif "permission_rules" in line:
                raise ValueError("use [[permission_rules]] for argument rules")
        elif current is not None:
            if "=" not in line:
                raise ValueError("invalid permission rule: " + line)
            key, value = (part.strip() for part in line.split("=", 1))
            if key in current:
                raise ValueError("duplicate permission rule field: " + key)
            if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
                current[key] = value[1:-1]
            else:
                try:
                    current[key] = json.loads(value)
                except ValueError as exc:
                    raise ValueError("permission rule values must be quoted strings") from exc
    return validate_rules(rules)


def load_limits(path: str = "agent.toml", overrides: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """Read [limits] values without adding a runtime dependency on a TOML package."""
    limits = DEFAULT_LIMITS.copy()
    config_path = Path(path)
    if config_path.is_file():
        section = False
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip() == "limits"
                continue
            if section and "=" in line:
                key, value = (item.strip() for item in line.split("=", 1))
                if key in limits:
                    try:
                        parsed = int(value)
                    except ValueError as exc:
                        raise ValueError("лимит {} должен быть целым числом".format(key)) from exc
                    if parsed < 0:
                        raise ValueError("лимит {} не может быть отрицательным".format(key))
                    limits[key] = parsed
    if overrides:
        limits.update({key: value for key, value in overrides.items() if value is not None})
    return limits
