"""Provider adapters for OpenAI-compatible chat completion APIs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol
from urllib import error, request


class ModelError(RuntimeError):
    """Raised when a provider request fails or returns an invalid payload."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    content: str
    tool_calls: List[ToolCall]


class ModelClient(Protocol):
    def complete(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> AssistantTurn: ...


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 180.0
    reasoning_effort: str = "none"

    @classmethod
    def from_environment(
        cls,
        env: Optional[Mapping[str, str]] = None,
        model_override: Optional[str] = None,
        base_url_override: Optional[str] = None,
    ) -> "ModelConfig":
        values = dict(os.environ if env is None else env)
        _load_dotenv_if_present(values)
        api_key = values.get("OPENAI_API_KEY", "").strip()
        base_url = (
            base_url_override or values.get("OPENAI_BASE_URL", "http://localhost:11434/v1/")
        ).strip()
        model = (model_override or values.get("OPENAI_MODEL", "qwen3:4b")).strip()
        if not api_key:
            raise ModelError("OPENAI_API_KEY не задан")
        if not model:
            raise ModelError("OPENAI_MODEL не задан")
        return cls(
            api_key,
            base_url.rstrip("/"),
            model,
            reasoning_effort=values.get("OPENAI_REASONING_EFFORT", "none").strip() or "none",
        )


class OpenAICompatibleClient:
    """Dependency-free Chat Completions client with tool calling support."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def complete(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> AssistantTurn:
        payload: Dict[str, Any] = {"model": self.config.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            # Ollama supports tools but not OpenAI's tool_choice/reasoning_effort fields.
            if (
                "localhost:11434" not in self.config.base_url
                and "127.0.0.1:11434" not in self.config.base_url
            ):
                payload["reasoning_effort"] = self.config.reasoning_effort
        raw = self._post(payload)
        try:
            message = raw["choices"][0]["message"]
            content = message.get("content") or ""
            calls = []
            for item in message.get("tool_calls") or []:
                function = item["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                calls.append(ToolCall(str(item["id"]), str(function["name"]), arguments))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("Некорректный ответ OpenAI-compatible API") from exc
        if not isinstance(content, str):
            raise ModelError("API вернул некорректный текст ответа")
        return AssistantTurn(content, calls)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.config.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ModelError("Модель вернула HTTP {}: {}".format(exc.code, detail)) from exc
        except (error.URLError, TimeoutError, ValueError) as exc:
            raise ModelError("Не удалось получить корректный ответ модели: {}".format(exc)) from exc


def _load_dotenv_if_present(values: Dict[str, str], path: str = ".env") -> None:
    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip().strip("\"'")
