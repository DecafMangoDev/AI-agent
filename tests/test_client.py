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
