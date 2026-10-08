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
        self.assertIn('if(keyChanged&&apiKey)payload.api_key=apiKey', self.javascript)

    def test_key_reveal_and_model_persistence_use_protected_endpoints(self):
        self.assertIn('id="api-key-reveal"', self.html)
        self.assertIn('/api/config/key', self.javascript)
        self.assertIn('/api/config/model', self.javascript)
        self.assertIn('default_model:defaultModel', self.javascript)
        self.assertIn('if(keyChanged&&apiKey)payload.api_key=apiKey', self.javascript)
        self.assertIn(
            'const modelOnly=!keyChanged&&!togglesChanged&&defaultModel&&baseUrl===baseUrlInput.dataset.savedValue',
            self.javascript,
        )


    def test_key_reveal_asks_before_showing_the_secret(self):
        self.assertIn('apiFetch("/api/config/key",{headers:{"X-Reveal-Api-Key":"1"}})', self.javascript)
        self.assertIn("saved.key_reveal_enabled===false", self.javascript)
        self.assertNotIn("localStorage.setItem(\"chat-config\",JSON.stringify({apiKey", self.javascript)


    def test_successful_save_keeps_the_key_visible_in_the_dialog(self):
        self.assertIn("let apiKeyDraft=\"\";", self.javascript)
        self.assertIn("const keyChanged=apiKey!==apiKeyDraft;", self.javascript)
        self.assertIn("if(keyChanged)apiKeyDraft=apiKey;apiKeyInput.value=apiKeyDraft;return true", self.javascript)
        self.assertNotIn("loadModels(defaultModel||null);apiKeyInput.value=\"\"", self.javascript)

    def test_reopening_the_dialog_restores_the_key(self):
        self.assertIn("apiKeyInput.value=apiKeyDraft;\n  apiKeyInput.type=\"password\"", self.javascript)
        self.assertIn("apiKeyInput.value=data.api_key||\"\";apiKeyDraft=apiKeyInput.value", self.javascript)

    def test_web_search_switch_is_wired_through_the_settings_dialog(self):
        self.assertIn('id="web-search" type="checkbox"', self.html)
        self.assertIn('const webSearchInput=document.getElementById("web-search");', self.javascript)
        self.assertIn("webSearchInput.checked=Boolean(saved.web_search_enabled);", self.javascript)
        self.assertIn("web_search:webSearchInput.checked", self.javascript)

    def test_search_trace_renders_sources_as_safe_links(self):
        self.assertIn('if(!/^https?:\\/\\//i.test(url))continue;', self.javascript)
        self.assertIn('link.rel="noopener noreferrer";', self.javascript)
        self.assertIn("link.textContent=String(source.title||url).slice(0,120);", self.javascript)
        self.assertIn("payload.search", self.javascript)
        self.assertIn("payload.sources", self.javascript)
        self.assertIn("record.search=[...searches]", self.javascript)

    def test_cached_shell_advertises_the_missing_backend(self):
        self.assertIn('await apiFetch("/api/session")', self.javascript)
        self.assertIn("\u5f53\u524d\u9875\u9762\u65e0\u6cd5\u8fde\u63a5\u672c\u5730\u670d\u52a1", self.javascript)
        self.assertIn("python server.py", self.javascript)

    def test_dead_local_server_is_reported_in_plain_words(self):
        self.assertIn("try{response=await fetch(url,options)}", self.javascript)
        self.assertIn('if(error.name==="AbortError")throw error;', self.javascript)
        self.assertIn("\u65e0\u6cd5\u8fde\u63a5\u5230\u672c\u5730\u670d\u52a1", self.javascript)

    def test_failed_save_keeps_the_typed_key(self):
        self.assertNotIn("finally{apiKeyInput.value\"\"", self.javascript)
        self.assertIn("finally{apiKeyInput.type=\"password\"", self.javascript)
        self.assertIn('configError.textContent=error.message', self.javascript)


    def test_setting_toggles_are_never_dropped_by_the_model_shortcut(self):
        # flipping only 联网搜索 / 图片输入 used to POST the model-only payload,
        # which silently discarded the checkbox the user just saved.
        self.assertIn('webSearchInput.dataset.savedState=webSearchInput.checked?"1":"0";', self.javascript)
        self.assertIn('visionInput.dataset.savedState=visionInput.checked?"1":"0";', self.javascript)
        self.assertIn("const modelOnly=!keyChanged&&!togglesChanged&&defaultModel", self.javascript)
        self.assertNotIn("const modelOnly=!keyChanged&&defaultModel", self.javascript)
        self.assertIn("await loadModels(defaultModel||null);await refreshVisionCapability();", self.javascript)

    def test_refused_image_input_is_always_explained(self):
        paste = self.javascript.split('input.addEventListener("paste"', 1)[1].split("});", 1)[0]
        self.assertNotIn("if(!visionEnabled)return;", paste)
        self.assertIn("attachImageFile(file)", paste)
        self.assertIn('composer.addEventListener("dragover",(event)=>{event.preventDefault();', self.javascript)
        drop = self.javascript.split('composer.addEventListener("drop"', 1)[1].split("});", 1)[0]
        self.assertNotIn("if(!visionEnabled)return;", drop)
        self.assertIn("attachImageFile(file)", drop)


    def test_clipboard_files_without_a_mime_type_are_still_accepted(self):
        # Explorer and chat apps often put an image on the clipboard with only a
        # file name, so a missing MIME type must not silently discard the paste.
        self.assertIn("if(!file||(file.type&&!file.type.startsWith(", self.javascript)
        self.assertIn("||/\\.(png|jpe?g|webp)$/i.test(item.name", self.javascript)
        self.assertNotIn("item.kind===\"file\"&&item.type&&item.type.startsWith", self.javascript)


    def test_search_override_is_saved_and_never_dropped(self):
        self.assertIn('id="search-input" type="checkbox"', self.html)
        self.assertIn('searchInput.checked=Boolean(saved.search_input_enabled);', self.javascript)
        self.assertIn('search_input:searchInput.checked', self.javascript)
        # all three toggles must veto the model-only shortcut
        self.assertIn(
            "[webSearchInput,visionInput,searchInput].some((box)=>box.checked!==(box.dataset.savedState===\"1\"))",
            self.javascript,
        )

    def test_model_hint_warns_about_display_names(self):
        self.assertIn('id="model-hint"', self.html)
        self.assertIn("relayModels=Array.isArray(data.discovered_models)?data.discovered_models:[]", self.javascript)
        self.assertIn('modelsInput.addEventListener("input",refreshModelHint)', self.javascript)
        self.assertIn('commonModelSelect.addEventListener("change",refreshModelHint)', self.javascript)
        self.assertIn('relayModels.includes(typed)', self.javascript)


if __name__ == "__main__":
    unittest.main()
