import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class FrontendSettingsTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_settings_have_common_and_custom_model_controls(self):
        self.assertIn('id="common-model"', self.html)
        self.assertIn('value="gpt-4o"', self.html)
        self.assertIn('id="models"', self.html)
        self.assertIn("const commonModel=commonModelSelect.value", self.javascript)
        self.assertIn("const customModels=modelsInput.value", self.javascript)

    def test_api_key_is_not_saved_to_browser_storage(self):
        self.assertNotIn("localStorage.setItem(\"chat-config\",JSON.stringify({baseUrl,apiKey", self.javascript)
        self.assertNotIn("saved.apiKey", self.javascript)
        self.assertIn("/api/config", self.javascript)

    def test_saved_api_key_can_be_reused_without_reentry(self):
        self.assertIn('id="api-key" type="password"', self.html)
        self.assertNotIn('id="api-key" type="password" placeholder="sk-..." autocomplete="off" required', self.html)
        self.assertIn('apiKeyInput.value=""', self.javascript)
        self.assertIn('if(apiKey&&!modelOnly)payload.api_key=apiKey', self.javascript)

    def test_key_reveal_and_model_persistence_use_protected_endpoints(self):
        self.assertIn('id="api-key-reveal"', self.html)
        self.assertIn('/api/config/key', self.javascript)
        self.assertIn('/api/config/model', self.javascript)
        self.assertIn('default_model:defaultModel', self.javascript)
        self.assertIn('if(apiKey&&!modelOnly)payload.api_key=apiKey', self.javascript)
        self.assertIn('const modelOnly=!apiKey&&defaultModel&&baseUrl===baseUrlInput.dataset.savedValue', self.javascript)


    def test_key_reveal_asks_before_showing_the_secret(self):
        self.assertIn('apiFetch("/api/config/key",{headers:{"X-Reveal-Api-Key":"1"}})', self.javascript)
        self.assertIn("saved.key_reveal_enabled===false", self.javascript)
        self.assertNotIn("localStorage.setItem(\"chat-config\",JSON.stringify({apiKey", self.javascript)


if __name__ == "__main__":
    unittest.main()
