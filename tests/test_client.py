import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.client import ModelConfig, ModelError, OpenAICompatibleClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "готово"}}]}).encode("utf-8")


class ClientTest(unittest.TestCase):
    def test_luna_tools_explicitly_disable_reasoning(self):
        tool = {"type": "function", "function": {"name": "list_files", "parameters": {"type": "object", "properties": {}}}}
        for model in ("gpt-6-luna", "gpt-6-luna-2026-10-01"):
            with self.subTest(model=model):
                config = ModelConfig("test-key", "https://example.test/v1", model)
                with patch("agent.client.request.urlopen", return_value=FakeResponse()) as send:
                    OpenAICompatibleClient(config).complete([], [tool])
                payload = json.loads(send.call_args.args[0].data)
                self.assertEqual(payload["reasoning_effort"], "none")
                self.assertEqual(payload["tools"], [tool])

    def test_luna_incompatible_reasoning_fails_before_http(self):
        config = ModelConfig("test-key", "https://example.test/v1", "gpt-6-luna", reasoning_effort="medium")
        with patch("agent.client.request.urlopen") as send:
            with self.assertRaisesRegex(ModelError, "OPENAI_REASONING_EFFORT=none"):
                OpenAICompatibleClient(config).complete([], [{"type": "function"}])
            send.assert_not_called()

    def test_generic_provider_does_not_receive_default_reasoning(self):
        config = ModelConfig("test-key", "https://example.test/v1", "test-model")
        with patch("agent.client.request.urlopen", return_value=FakeResponse()) as send:
            OpenAICompatibleClient(config).complete([], [{"type": "function"}])
        self.assertNotIn("reasoning_effort", json.loads(send.call_args.args[0].data))

    def test_complete_parses_chat_completion(self):
        config = ModelConfig("test-key", "https://example.test/v1", "test-model")
        with patch("agent.client.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = OpenAICompatibleClient(config).complete(
                [{"role": "user", "content": "Привет"}], []
            )
        self.assertEqual(result.content, "готово")
        sent_request = urlopen.call_args.args[0]
        self.assertEqual(sent_request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(json.loads(sent_request.data)["model"], "test-model")

    def test_config_requires_key_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                with self.assertRaises(ModelError):
                    ModelConfig.from_environment({})
            finally:
                os.chdir(old_cwd)

    def test_config_loads_local_dotenv_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                "OPENAI_API_KEY=file-key\nOPENAI_MODEL=file-model\n",
                encoding="utf-8",
            )
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                config = ModelConfig.from_environment({})
            finally:
                os.chdir(old_cwd)
        self.assertEqual(config.api_key, "file-key")
        self.assertEqual(config.model, "file-model")

    def test_model_and_endpoint_can_be_overridden(self):
        config = ModelConfig.from_environment(
            {"OPENAI_API_KEY": "key", "OPENAI_MODEL": "env-model"},
            model_override="cli-model",
            base_url_override="http://localhost:11434/v1",
        )
        self.assertEqual(config.model, "cli-model")
        self.assertEqual(config.base_url, "http://localhost:11434/v1")


if __name__ == "__main__":
    unittest.main()
