import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import server


VISION_TEST_API_KEY = "sk-vision-sentinel-key"
ALIAS_MODEL = "gpt-pro-稳定 2x"
VISION_TEST_TIMEOUT_SECONDS = 15
# 1x1 opaque PNG, used as a well-formed inline image.
TINY_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="


# The gateway answers a search-enabled image question on the native endpoint.
NATIVE_IMAGE_SEARCH_EVENTS = [
    {"type": "message_start", "message": {"id": "msg_vision_search", "role": "assistant", "content": []}},
    {"type": "content_block_start", "index": 0, "content_block": {
        "type": "server_tool_use", "id": "srvtoolu_v", "name": "web_search", "input": {}}},
    {"type": "content_block_delta", "index": 0, "delta": {
        "type": "input_json_delta", "partial_json": "{\"query\": \"tailwind red 500\"}"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {
        "type": "web_search_tool_result", "tool_use_id": "srvtoolu_v", "content": [
            {"type": "web_search_result", "url": "https://tailwindcss.com/palette", "title": "Palette 500"}]}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "LEFT=red, RIGHT=blue"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
]


class VisionMockRelayHandler(BaseHTTPRequestHandler):
    """Records every chat payload the app forwards, then answers with a tiny stream."""

    requests = []
    native_search_reply = False

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw}
        self.__class__.requests.append((self.path, body))
        if self.path == "/v1/messages":
            if not self.__class__.native_search_reply:
                self.send_json(404, {"type": "error", "error": {"type": "not_found_error"}})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for event in NATIVE_IMAGE_SEARCH_EVENTS:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        payload = {
            "id": "chatcmpl-vision",
            "object": "chat.completion.chunk",
            "created": 1_700_000_000,
            "model": "relay-reported-model",
            "choices": [{"delta": {"content": "noted"}, "index": 0}],
        }
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [
                {"id": "vision-test-claude"}, {"id": "claude-专业版"},
            ]})
            return
        self.send_json(404, {"error": "not found"})

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class VisionInputTest(unittest.TestCase):
    def setUp(self):
        VisionMockRelayHandler.requests = []
        VisionMockRelayHandler.native_search_reply = False
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), VisionMockRelayHandler)
        self.relay_thread = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.relay_thread.start()
        self.config_path = Path(__file__).with_name(f".config-{uuid4().hex}.json")
        self.history_path = Path(__file__).with_name(f".history-{uuid4().hex}.json")
        chat_server = server.ChatServer(app_password="", config_path=self.config_path)
        chat_server.conversations = server.ConversationStore(self.history_path)
        chat_server.configure(
            VISION_TEST_API_KEY,
            f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
            ["vision-test-claude", "text-only-model"],
            "vision-test-claude",
        )
        server.Handler.chat_server = chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)
        self.relay.shutdown()
        self.relay.server_close()
        self.relay_thread.join(timeout=5)
        for path in (self.config_path, self.history_path, server.Handler.chat_server.secret_store.path):
            path.unlink(missing_ok=True)

    def post(self, path, payload, timeout=VISION_TEST_TIMEOUT_SECONDS):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            error.close()
            return error.code, detail

    def post_refusal(self, path, payload):
        """POST a body the server must refuse.

        A refusal is either a clean 4xx or an aborted connection: the server
        never reads an oversized body, so it closes the socket to keep the
        leftovers from being parsed as a new request.
        """
        try:
            return self.post(path, payload)
        except (URLError, OSError):
            return None, ""

    def get(self, path):
        request = Request(self.base_url + path, method="GET")
        try:
            with urlopen(request, timeout=VISION_TEST_TIMEOUT_SECONDS) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def chat(self, messages, model="vision-test-claude"):
        return self.post("/api/chat", {"messages": messages, "model": model})

    def relay_bodies(self, path="/v1/chat/completions"):
        return [body for name, body in VisionMockRelayHandler.requests if name == path]

    def content_parts(self, message):
        content = message["content"]
        self.assertIsInstance(content, list)
        return content

    def test_image_reaches_the_relay_as_a_content_part(self):
        status, stream = self.chat([{"role": "user", "content": "what is in the picture", "images": [TINY_PNG]}])
        self.assertEqual(status, 200, stream)
        bodies = self.relay_bodies()
        self.assertEqual(len(bodies), 1)
        parts = self.content_parts(bodies[0]["messages"][0])
        self.assertEqual(parts[0], {"type": "text", "text": "what is in the picture"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(parts[1]["image_url"]["url"], TINY_PNG)

    def test_image_stays_visible_on_later_turns(self):
        history = [{"role": "user", "content": "first", "images": [TINY_PNG]}, {"role": "assistant", "content": "seen"}]
        self.chat(history + [{"role": "user", "content": "again?"}])
        bodies = self.relay_bodies()
        parts = self.content_parts(bodies[0]["messages"][0])
        self.assertEqual(parts[1]["image_url"]["url"], TINY_PNG)
        self.assertEqual(bodies[0]["messages"][-1]["content"], "again?")

    def test_plain_text_messages_keep_string_content(self):
        self.chat([{"role": "user", "content": "no picture here"}])
        bodies = self.relay_bodies()
        self.assertEqual(bodies[0]["messages"][0]["content"], "no picture here")

    def test_history_keeps_the_attachment_and_answers_are_streamed(self):
        status, stream = self.chat([{"role": "user", "content": "picture", "images": [TINY_PNG]}])
        self.assertIn("noted", stream)
        conversations = server.Handler.chat_server.conversations.list_conversations()
        self.assertEqual(len(conversations), 1)
        stored = server.Handler.chat_server.conversations.get_conversation(conversations[0]["id"])
        self.assertEqual(stored["messages"][0]["images"], [TINY_PNG])

    def test_image_for_a_non_vision_model_is_rejected(self):
        status, body = self.chat(
            [{"role": "user", "content": "picture", "images": [TINY_PNG]}],
            model="text-only-model",
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(self.relay_bodies(), [])

    def test_manual_switch_allows_images_for_any_model(self):
        relay_port = self.relay.server_address[1]
        status, body = self.post(
            "/api/config",
            {
                "api_key": VISION_TEST_API_KEY,
                "base_url": f"http://127.0.0.1:{relay_port}/v1",
                "models": ["text-only-model"],
                "default_model": "text-only-model",
                "vision_input": True,
            },
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(server.Handler.chat_server.vision_input)
        with urlopen(Request(self.base_url + "/api/config"), timeout=VISION_TEST_TIMEOUT_SECONDS) as response:
            summary = json.loads(response.read().decode("utf-8"))
        self.assertTrue(summary["vision_input_enabled"])
        self.assertFalse(summary["vision_auto_supported"])
        status, body = self.chat(
            [{"role": "user", "content": "picture", "images": [TINY_PNG]}],
            model="text-only-model",
        )
        self.assertEqual(status, 200, body)

    def test_unsafe_or_oversized_attachments_are_rejected(self):
        huge = "data:image/png;base64," + "A" * ((server.MAX_IMAGE_BYTES + 10) * 4 // 3)
        cases = {
            "remote url": "https://example.com/picture.png",
            "svg data uri": "data:image/svg+xml;base64,AAA=",
            "not base64": "data:image/png;base64,@@@@",
            "header mismatch": "data:image/jpeg;base64,iVBORw0KGgo=",
            "too large": huge,
        }
        for name, image in cases.items():
            status, body = self.chat([{"role": "user", "content": "picture", "images": [image]}])
            self.assertEqual(status, 400, f"{name} must be rejected: {body[:120]}")
        status, body = self.chat([{"role": "user", "content": "picture", "images": [TINY_PNG, TINY_PNG]}])
        self.assertEqual(status, 400)
        self.assertIn("1", body)
        status, body = self.chat([{"role": "assistant", "content": "picture", "images": [TINY_PNG]}])
        self.assertEqual(status, 400)
        status, body = self.chat([{"role": "user", "content": "picture", "images": TINY_PNG}])
        self.assertEqual(status, 400)
        self.assertEqual(self.relay_bodies(), [])

    def test_rejected_images_never_reach_the_relay_and_leak_nothing(self):
        self.chat([{"role": "user", "content": "picture", "images": ["https://example.com/x.png"]}])
        self.assertEqual(self.relay_bodies(), [])
        status, body = self.chat([{"role": "user", "content": "picture", "images": ["data:image/png;base64,@@@@"], "extra": VISION_TEST_API_KEY}])
        self.assertEqual(status, 400)
        self.assertNotIn(VISION_TEST_API_KEY, body)

    def test_chat_accepts_a_larger_body_than_config(self):
        padding = "x" * (2 * 1024 * 1024)
        status, stream = self.chat([{"role": "user", "content": padding, "images": [TINY_PNG]}])
        self.assertEqual(status, 200, stream[:160])
        self.assertEqual(len(self.relay_bodies()), 1)
        status, body = self.post_refusal(
            "/api/config", {"api_key": "sk-" + padding, "base_url": "http://127.0.0.1:1/v1"}
        )
        if status is not None:
            self.assertEqual(status, 400)
            self.assertIn("Invalid JSON body", body)
        self.assertEqual(server.Handler.chat_server.api_key, VISION_TEST_API_KEY)
        self.assertTrue(server.Handler.chat_server.base_url.endswith("/v1"))
        self.assertNotIn("127.0.0.1:1", server.Handler.chat_server.base_url)

    def test_images_and_web_search_share_the_native_endpoint(self):
        VisionMockRelayHandler.native_search_reply = True
        server.Handler.chat_server.web_search_enabled = True
        status, stream = self.chat([{"role": "user", "content": "colors?", "images": [TINY_PNG]}])
        self.assertEqual(status, 200, stream)
        self.assertNotIn("notice", stream)
        self.assertIn("LEFT=red, RIGHT=blue", stream)
        native = [body for name, body in VisionMockRelayHandler.requests if name == "/v1/messages"]
        self.assertEqual(len(native), 1)
        blocks = native[0]["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "colors?"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(native[0]["tools"], [server.web_search_tool_declaration()])
        self.assertNotIn("image_url", json.dumps(blocks[1]))
        self.assertEqual([name for name, _body in VisionMockRelayHandler.requests], ["/v1/messages"])

    def test_images_fall_back_when_the_native_channel_refuses_search(self):
        server.Handler.chat_server.web_search_enabled = True
        status, stream = self.chat([{"role": "user", "content": "picture", "images": [TINY_PNG]}])
        self.assertEqual(status, 200, stream)
        self.assertIn(server.NATIVE_SEARCH_IMAGE_UNAVAILABLE_NOTICE, stream)
        self.assertIn("noted", stream)
        bodies = self.relay_bodies()
        self.assertEqual(len(bodies), 1)
        parts = bodies[0]["messages"][0]["content"]
        self.assertEqual(parts[1], {"type": "image_url", "image_url": {"url": TINY_PNG}})
        self.assertNotIn(VISION_TEST_API_KEY, stream)

    def test_anthropic_projection_is_side_effect_free_and_type_strict(self):
        projected = server.anthropic_relay_payload([
            {"role": "user", "content": "hi", "images": [TINY_PNG]},
            {"role": "assistant", "content": "hello"},
        ])
        self.assertEqual(projected[1], {"role": "assistant", "content": "hello"})
        self.assertEqual(projected[0]["content"][1]["source"]["type"], "base64")
        self.assertEqual(
            server.anthropic_relay_payload([{"role": "user", "content": "hi"}]),
            [{"role": "user", "content": "hi"}],
        )
        for bad in ("data:image/svg+xml;base64,AAAA", "data:image/png;base64,", "not-a-data-uri"):
            with self.assertRaises(ValueError):
                server.anthropic_image_block(bad)

    def test_model_switch_keeps_both_manual_switches(self):
        base_url = server.Handler.chat_server.base_url
        status, body = self.post("/api/config", {
            "api_key": VISION_TEST_API_KEY,
            "base_url": base_url,
            "models": ["vision-test-claude", "text-only-model"],
            "default_model": "text-only-model",
            "web_search": False,
            "vision_input": True,
            "search_input": True,
        })
        self.assertEqual(status, 200, body)
        status, body = self.post(
            "/api/config/model", {"model": "vision-test-claude", "models": ["vision-test-claude"]}
        )
        self.assertEqual(status, 200, body)
        reloaded = server.ChatServer(app_password="", config_path=self.config_path)
        self.assertTrue(reloaded.vision_input, "switching models erased the image switch")
        self.assertTrue(reloaded.search_input, "switching models erased the search switch")

    def test_search_override_enables_alias_models(self):
        VisionMockRelayHandler.native_search_reply = True
        server.Handler.chat_server.web_search_enabled = True
        server.Handler.chat_server.search_input = True
        status, stream = self.chat([{"role": "user", "content": "latest news?"}], model=ALIAS_MODEL)
        self.assertEqual(status, 200, stream)
        self.assertIn("LEFT=red, RIGHT=blue", stream)
        self.assertEqual([name for name, _body in VisionMockRelayHandler.requests], ["/v1/messages"])
        body = VisionMockRelayHandler.requests[0][1]
        self.assertEqual(body["model"], ALIAS_MODEL)
        self.assertEqual(body["tools"], [server.web_search_tool_declaration()])

    def test_alias_model_without_override_is_not_searched(self):
        VisionMockRelayHandler.native_search_reply = True
        server.Handler.chat_server.web_search_enabled = True
        status, stream = self.chat([{"role": "user", "content": "latest news?"}], model=ALIAS_MODEL)
        self.assertEqual(status, 200, stream)
        self.assertIn(server.NATIVE_SEARCH_UNSUPPORTED_MODEL_NOTICE, stream)
        paths = [name for name, _body in VisionMockRelayHandler.requests]
        self.assertNotIn("/v1/messages", paths)
        self.assertIn(ALIAS_MODEL, [body.get("model") for _name, body in VisionMockRelayHandler.requests])

    def test_models_endpoint_separates_the_relay_list(self):
        status, body = self.get("/api/models")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["source"], "relay")
        self.assertIn("claude-专业版", payload["discovered_models"])
        self.assertIn("text-only-model", payload["models"])
        self.assertNotIn("text-only-model", payload["discovered_models"])

    def test_relay_payload_projection_is_side_effect_free(self):
        projected = server.relay_payload([{"role": "user", "content": "hi", "images": [TINY_PNG]}])
        self.assertEqual(projected[0]["content"][1]["image_url"]["url"], TINY_PNG)
        self.assertEqual(
            server.relay_payload([{"role": "user", "content": "hi"}]),
            [{"role": "user", "content": "hi"}],
        )

    def test_signature_check_rejects_lying_data_uri(self):
        with self.assertRaises(ValueError):
            server.normalize_message_images(["data:image/webp;base64,iVBORw0KGgo="], "user", True)
        self.assertEqual(
            server.normalize_message_images([TINY_PNG], "user", True)[0],
            TINY_PNG,
        )


if __name__ == "__main__":
    unittest.main()
