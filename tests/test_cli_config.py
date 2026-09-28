import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import chat


class CliConfigTest(unittest.TestCase):
    def test_cli_loads_saved_configuration_after_restart(self):
        config_path = Path(__file__).with_name(f".cli-config-{uuid4().hex}.json")
        secret_path = config_path.with_name(f"{config_path.stem}.secret.json")
        try:
            settings = chat.ChatServer(app_password="", config_path=config_path)
            settings.configure(
                "relay-secret",
                "https://relay.example.com/v1",
                ["custom-model"],
                "custom-model",
            )
            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "OPENAI_MODEL": "", "OPENAI_MODELS": ""},
            ):
                api_key, base_url, model, models = chat.load_configuration(config_path)

            self.assertEqual(api_key, "relay-secret")
            self.assertEqual(base_url, "https://relay.example.com/v1")
            self.assertEqual(model, "custom-model")
            self.assertEqual(models, ["custom-model"])
            self.assertTrue(secret_path.exists())
        finally:
            config_path.unlink(missing_ok=True)
            secret_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
