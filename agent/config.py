"""Configuration loaded from a small, human-editable TOML-like file."""

from pathlib import Path
from typing import Dict, Optional

DEFAULT_LIMITS = {"model": 20, "tools": 40, "effects": 15, "result": 120000}


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
