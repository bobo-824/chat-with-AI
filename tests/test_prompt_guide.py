import json
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
TEST_TIMEOUT_SECONDS = 10


def read_static(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


class PromptGuideStoreTest(unittest.TestCase):
    """The store itself: default when nothing saved, round-trip, survives “restart”."""

    def setUp(self):
        self.path = Path(__file__).with_name(f".guide-{uuid4().hex}.prompt-guide.json")

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_load_returns_default_when_nothing_saved(self):
        store = server.PromptGuideStore(self.path)
        self.assertEqual(store.load(), server.PROMPT_GUIDE_DEFAULT_TEXT)

    def test_saved_text_round_trips_through_a_new_store(self):
        server.PromptGuideStore(self.path).save("我自己的提示词方法")
        # 新建实例 = 服务重启后重新读盘
        self.assertEqual(server.PromptGuideStore(self.path).load(), "我自己的提示词方法")

    def test_empty_saved_text_is_kept_not_replaced_by_default(self):
        server.PromptGuideStore(self.path).save("")
        self.assertEqual(server.PromptGuideStore(self.path).load(), "")

    def test_corrupt_file_falls_back_to_default(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(server.PromptGuideStore(self.path).load(), server.PROMPT_GUIDE_DEFAULT_TEXT)

    def test_non_string_payload_falls_back_to_default(self):
        self.path.write_text(json.dumps({"version": 1, "text": 42}), encoding="utf-8")
        self.assertEqual(server.PromptGuideStore(self.path).load(), server.PROMPT_GUIDE_DEFAULT_TEXT)

    def test_save_rejects_non_string_and_oversize_text(self):
        store = server.PromptGuideStore(self.path)
        with self.assertRaises(ValueError):
            store.save(None)
        with self.assertRaises(ValueError):
            store.save("x" * (server.PROMPT_GUIDE_MAX_CHARS + 1))
        self.assertFalse(self.path.exists())

    def test_default_text_is_a_usable_guide(self):
        text = server.PROMPT_GUIDE_DEFAULT_TEXT
        self.assertGreater(len(text), 200)
        for heading in ("角色", "任务", "上下文", "输出格式", "示例"):
            self.assertIn(heading, text)
        self.assertTrue(text.startswith("# "))


class PromptGuideEndpointTest(unittest.TestCase):
    """GET / POST /api/prompt-guide served by a real app server on throwaway files."""

    def setUp(self):
        self.config_path = Path(__file__).with_name(f".config-{uuid4().hex}.json")
        self.chat_server = server.ChatServer(app_password="", config_path=self.config_path)
        self.guide_path = self.chat_server.prompt_guide.path
        server.Handler.chat_server = self.chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)
        secret_path = server.Handler.chat_server.secret_store.path
        for path in (self.config_path, self.guide_path, secret_path):
            path.unlink(missing_ok=True)

    def request(self, method, payload=None):
        body = None
        headers = {}
        if payload is not None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + "/api/prompt-guide", data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=TEST_TIMEOUT_SECONDS) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            raw = error.read().decode("utf-8")
            error.close()
            return error.code, raw

    def get_guide(self):
        status, raw = self.request("GET")
        self.assertEqual(status, 200, raw)
        return json.loads(raw), raw

    def test_get_returns_current_and_default_text(self):
        data, _ = self.get_guide()
        self.assertEqual(data["text"], server.PROMPT_GUIDE_DEFAULT_TEXT)
        self.assertEqual(data["default"], server.PROMPT_GUIDE_DEFAULT_TEXT)
        self.assertFalse(data["customized"])
        self.assertEqual(data["max_chars"], server.PROMPT_GUIDE_MAX_CHARS)

    def test_post_saves_and_get_reads_it_back(self):
        status, raw = self.request("POST", {"text": "第一步：先说清楚要什么"})
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)["saved"])
        data, _ = self.get_guide()
        self.assertEqual(data["text"], "第一步：先说清楚要什么")
        self.assertTrue(data["customized"])
        # default 不受保存影响，“恢复默认”才有得还原
        self.assertEqual(data["default"], server.PROMPT_GUIDE_DEFAULT_TEXT)

    def test_post_persists_to_disk_for_the_next_process(self):
        self.request("POST", {"text": "重启之后还在的内容"})
        self.assertTrue(self.guide_path.exists())
        self.assertEqual(server.PromptGuideStore(self.guide_path).load(), "重启之后还在的内容")

    def test_blank_text_can_be_saved(self):
        status, raw = self.request("POST", {"text": ""})
        self.assertEqual(status, 200, raw)
        data, _ = self.get_guide()
        self.assertEqual(data["text"], "")

    def test_oversize_and_malformed_bodies_are_rejected(self):
        cases = [
            {"text": "x" * (server.PROMPT_GUIDE_MAX_CHARS + 1)},
            {"text": None},
            {"text": ["not", "a", "string"]},
            {},
        ]
        for payload in cases:
            status, raw = self.request("POST", payload)
            self.assertEqual(status, 400, f"{payload} -> {status} {raw[:120]}")

    def test_get_requires_authentication(self):
        self.chat_server.app_password = "secret-guide-password"
        status, raw = self.request("GET")
        self.assertEqual(status, 401, raw)
        status, raw = self.request("POST", {"text": "nope"})
        self.assertEqual(status, 401, raw)

    def test_response_does_not_leak_secrets(self):
        self.chat_server.api_key = "sk-guide-sentinel-key"
        _, raw = self.request("POST", {"text": "无害内容"})
        _, raw = self.get_guide()
        for fragment in ("sk-guide-sentinel-key", "app-secret", "server.py", str(Path.home())):
            self.assertNotIn(fragment, raw)


class PromptGuideMarkupTest(unittest.TestCase):
    """Static files: button sits between 新对话 and 历史对话, dialog is editable and wired."""

    def setUp(self):
        self.html = read_static("index.html")
        self.javascript = read_static("app.js")

    def test_button_lives_between_new_chat_and_history_section(self):
        new_chat = self.html.index('id="new-chat"')
        guide = self.html.index('id="prompt-guide"')
        history = self.html.index('class="history-section"')
        self.assertLess(new_chat, guide)
        self.assertLess(guide, history)

    def test_button_label_is_prompt_suggestion(self):
        button = re.search(r'<button class="prompt-guide-button" id="prompt-guide".*?</button>', self.html, re.DOTALL)
        self.assertIsNotNone(button)
        self.assertIn("prompt建议", button.group(0))

    def test_dialog_exposes_textarea_and_three_actions(self):
        self.assertIn('id="prompt-guide-dialog"', self.html)
        self.assertIn('<textarea id="prompt-guide-text"', self.html)
        self.assertIn('id="prompt-guide-restore"', self.html)
        self.assertIn('type="submit"', self.html)

    def test_dialog_is_reused_off_the_existing_config_dialog_pattern(self):
        self.assertIn('class="config-dialog prompt-guide-dialog"', self.html)

    def test_opening_the_dialog_fetches_from_the_server(self):
        self.assertIn('promptGuideButton.addEventListener("click",openPromptGuideDialog)', self.javascript)
        self.assertIn('await apiFetch("/api/prompt-guide")', self.javascript)
        self.assertIn("promptGuideDialog.showModal()", self.javascript)

    def test_saving_is_the_only_thing_that_writes_to_disk(self):
        self.assertIn('method:"POST"', self.javascript)
        self.assertIn("JSON.stringify({text:promptGuideText.value})", self.javascript)
        restore_handler = re.search(
            r"promptGuideRestoreButton\.addEventListener\(\"click\",.*?\n\}\);", self.javascript, re.DOTALL
        )
        self.assertIsNotNone(restore_handler)
        self.assertNotIn("POST", restore_handler.group(0))

    def test_default_copy_is_not_duplicated_in_the_frontend(self):
        # 默认稿只有服务端一份来源，前端只用接口返回的 data.default
        self.assertNotIn("怎样写好提示词", self.javascript)
        self.assertIn("promptGuideDefault=data.default", self.javascript)

    def test_textarea_has_styles_including_dark_mode_inheritance(self):
        css = read_static("style.css")
        self.assertIn("#prompt-guide-text", css)
        self.assertIn(".prompt-guide-button", css)
        self.assertIn("var(--background)", css.split("#prompt-guide-text {")[1].split("}")[0])


if __name__ == "__main__":
    unittest.main()
