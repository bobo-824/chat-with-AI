import json
import io
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


SECRET_ECHO_KEY = "sk-echoed-relay-key-1234567890"


class MockRelayHandler(BaseHTTPRequestHandler):
    requests = []
    fail_models = False
    fail_chat = False

    def do_GET(self):
        self.__class__.requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/v1/models":
            if self.__class__.fail_models:
                self.send_json(503, {"error": "temporarily unavailable"})
                return
            self.send_json(200, {"object": "list", "data": [{"id": "relay-model"}, {"id": "shared-model"}]})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "not found"})
            return

        if self.__class__.fail_chat:
            self.send_json(
                500,
                {
                    "error": "upstream failed",
                    "echo": {"authorization": self.headers.get("Authorization")},
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", "relay-request-123")
        self.send_header("Authorization", "Bearer relay-secret")
        self.end_headers()
        for content in ("Hello", " from relay"):
            payload = {
                "id": "chatcmpl-relay-123",
                "object": "chat.completion.chunk",
                "created": 1_700_000_000,
                "model": "relay-reported-model",
                "provider": "mock-provider",
                "metadata": {"route": "mock-route", "api_key": "relay-secret"},
                "backend": "mock-backend",
                "choices": [{"delta": {"content": content}, "index": 0}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class RelayWorkflowTest(unittest.TestCase):
    def setUp(self):
        MockRelayHandler.requests = []
        MockRelayHandler.fail_models = False
        MockRelayHandler.fail_chat = False
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), MockRelayHandler)
        self.relay_thread = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.relay_thread.start()

        self.history_path = Path(__file__).with_name(f".relay-history-{uuid4().hex}.json")
        chat_server = server.ChatServer(app_password="", config_path=self.history_path.with_name(f".config-{uuid4().hex}.json"))
        self.config_path = chat_server.config_store.path
        chat_server.conversations = server.ConversationStore(self.history_path)
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
        self.history_path.unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)
        server.Handler.chat_server.secret_store.path.unlink(missing_ok=True)

    def request(self, path, method="GET", payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"} if body else {}
        request_headers.update(headers or {})
        request = Request(self.base_url + path, data=body, method=method, headers=request_headers)
        return urlopen(request, timeout=5)

    def test_models_stream_and_history(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["fallback-model", "shared-model", "fallback-model"],
            },
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["configured"])

        with self.request("/api/models") as response:
            self.assertEqual(response.status, 200)
            model_data = json.loads(response.read())
            self.assertEqual(model_data["models"], ["relay-model", "shared-model", "fallback-model"])
            self.assertEqual(model_data["source"], "relay")

        with self.request(
            "/api/chat",
            "POST",
            {
                "messages": [{"role": "user", "content": "Say hello"}],
                "model": "relay-model",
            },
        ) as response:
            stream = response.read().decode("utf-8")

        self.assertIn('"conversation_id"', stream)
        self.assertIn('"content": "Hello"', stream)
        self.assertIn('"content": " from relay"', stream)
        self.assertTrue(stream.endswith("data: [DONE]\n\n"))

        conversations = server.Handler.chat_server.conversations.list_conversations()
        self.assertEqual(len(conversations), 1)
        conversation = server.Handler.chat_server.conversations.get_conversation(conversations[0]["id"])
        self.assertEqual(conversation["messages"][-1], {"role": "assistant", "content": "Hello from relay"})
        self.assertEqual(conversation["model"], "relay-model")
        self.assertEqual(conversations[0]["model"], "relay-model")

        self.assertIn(("GET", "/v1/models", "Bearer relay-secret"), MockRelayHandler.requests)
        chat_requests = [item for item in MockRelayHandler.requests if item[0] == "POST"]
        self.assertEqual(len(chat_requests), 1)
        self.assertEqual(chat_requests[0][1], "/v1/chat/completions")
        self.assertEqual(chat_requests[0][2], "Bearer relay-secret")
        relay_payload = json.loads(chat_requests[0][3])
        self.assertEqual(relay_payload["model"], "relay-model")
        self.assertTrue(relay_payload["stream"])
        self.assertEqual(relay_payload["messages"], [{"role": "user", "content": "Say hello"}])

        MockRelayHandler.fail_models = True
        with self.request("/api/models") as response:
            cached_data = json.loads(response.read())
        self.assertEqual(cached_data["models"], ["relay-model", "shared-model", "fallback-model"])
        self.assertEqual(cached_data["source"], "relay")

    def test_debug_output_captures_relay_identity_and_redacts_secrets(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["requested-model"],
            },
        ):
            pass

        output = io.StringIO()
        with patch.dict("os.environ", {"RELAY_DEBUG_RESPONSE": "1"}), redirect_stdout(output):
            with self.request(
                "/api/chat",
                "POST",
                {"messages": [{"role": "user", "content": "debug response"}], "model": "requested-model"},
            ) as response:
                self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        debug_output = output.getvalue()
        self.assertIn('"event": "response"', debug_output)
        self.assertIn('"status": 200', debug_output)
        self.assertIn('"x-request-id": "relay-request-123"', debug_output)
        self.assertIn('"event": "chunk"', debug_output)
        self.assertIn('"model": "relay-reported-model"', debug_output)
        self.assertIn('"provider": "mock-provider"', debug_output)
        self.assertIn('"backend": "mock-backend"', debug_output)
        self.assertIn('"route": "mock-route"', debug_output)
        self.assertIn('"event": "identity_summary"', debug_output)
        self.assertNotIn("relay-secret", debug_output)
        self.assertIn("[REDACTED]", debug_output)

    def test_config_persists_across_server_restart_and_blank_key_keeps_saved_key(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["custom-model"],
            },
        ):
            pass

        restarted = server.ChatServer(app_password="", config_path=self.config_path)
        self.assertEqual(restarted.api_key, "relay-secret")
        self.assertEqual(restarted.base_url, f"http://127.0.0.1:{self.relay.server_address[1]}/v1")
        self.assertEqual(restarted.configured_models, ["custom-model"])

        restarted.configure(restarted.api_key, restarted.base_url, ["custom-model", "gpt-4o"], "gpt-4o")
        reloaded = server.ChatServer(app_password="", config_path=self.config_path)
        self.assertEqual(reloaded.api_key, "relay-secret")
        self.assertEqual(reloaded.configured_models, ["custom-model", "gpt-4o"])
        self.assertEqual(reloaded.default_model, "gpt-4o")

    def test_restart_then_chat_uses_saved_key_without_reentry(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["relay-model"],
                "default_model": "relay-model",
            },
        ):
            pass

        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)

        restarted = server.ChatServer(app_password="", config_path=self.config_path)
        restarted.conversations = server.ConversationStore(self.history_path)
        server.Handler.chat_server = restarted
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "Say hello after restart"}]},
        ) as response:
            stream = response.read().decode("utf-8")

        self.assertIn('"content": "Hello"', stream)
        chat_requests = [item for item in MockRelayHandler.requests if item[0] == "POST"]
        self.assertTrue(chat_requests)
        self.assertEqual(chat_requests[-1][2], "Bearer relay-secret")
        relay_payload = json.loads(chat_requests[-1][3])
        self.assertEqual(relay_payload["model"], "relay-model")

    def test_switch_model_then_restart_keeps_saved_key(self):
        relay_url = f"http://127.0.0.1:{self.relay.server_address[1]}/v1"
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": relay_url,
                "models": ["model-a", "model-b"],
                "default_model": "model-a",
            },
        ):
            pass

        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "first message"}]},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        with self.request("/api/config/model", "POST", {"model": "model-b"}) as response:
            self.assertEqual(json.loads(response.read())["default_model"], "model-b")

        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "second message"}]},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)
        restarted = server.ChatServer(app_password="", config_path=self.config_path)
        restarted.conversations = server.ConversationStore(self.history_path)
        server.Handler.chat_server = restarted
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "after restart"}]},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        self.assertEqual(restarted.default_model, "model-b")
        self.assertEqual(restarted.api_key, "relay-secret")
        chat_requests = [item for item in MockRelayHandler.requests if item[0] == "POST"]
        self.assertEqual([item[2] for item in chat_requests[-3:]], ["Bearer relay-secret"] * 3)
        self.assertEqual([json.loads(item[3])["model"] for item in chat_requests[-3:]], ["model-a", "model-b", "model-b"])

    def test_saved_key_supports_custom_model_without_reentry(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["provider/custom-model"],
                "default_model": "provider/custom-model",
            },
        ):
            pass

        with self.request("/api/config/model", "POST", {"model": "provider/custom-model", "models": ["provider/custom-model"]}) as response:
            self.assertEqual(json.loads(response.read())["default_model"], "provider/custom-model")

        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "custom model"}]},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        chat_request = [item for item in MockRelayHandler.requests if item[0] == "POST"][-1]
        self.assertEqual(chat_request[2], "Bearer relay-secret")
        self.assertEqual(json.loads(chat_request[3])["model"], "provider/custom-model")

    def test_existing_conversation_can_switch_models(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["model-a", "model-b"],
                "default_model": "model-a",
            },
        ):
            pass

        first_messages = [{"role": "user", "content": "first"}]
        with self.request("/api/chat", "POST", {"messages": first_messages, "model": "model-a"}) as response:
            first_stream = response.read().decode("utf-8")
        conversation_id = first_stream.split('"conversation_id": "', 1)[1].split('"', 1)[0]

        second_messages = first_messages + [{"role": "assistant", "content": "Hello from relay"}, {"role": "user", "content": "second"}]
        with self.request(
            "/api/chat",
            "POST",
            {"conversation_id": conversation_id, "messages": second_messages, "model": "model-b"},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))

        chat_requests = [item for item in MockRelayHandler.requests if item[0] == "POST"]
        self.assertEqual([json.loads(item[3])["model"] for item in chat_requests[-2:]], ["model-a", "model-b"])
        conversation = server.Handler.chat_server.conversations.get_conversation(conversation_id)
        self.assertEqual(conversation["model"], "model-b")

    def test_config_hides_key_and_reveal_is_explicit(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["custom-model"],
            },
        ):
            pass
        with self.request("/api/config") as response:
            summary = json.loads(response.read())
        self.assertNotIn("api_key", summary)
        self.assertEqual(summary["api_key_hint"], "rela…cret")
        with self.assertRaises(HTTPError) as blocked:
            self.request("/api/config/key")
        self.assertEqual(blocked.exception.code, 400)
        self.assertNotIn("relay-secret", blocked.exception.read().decode("utf-8"))
        with self.request("/api/config/key", headers={"X-Reveal-Api-Key": "1"}) as response:
            self.assertEqual(json.loads(response.read())["api_key"], "relay-secret")
            self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")

    def test_legacy_plaintext_key_is_migrated_out_of_config(self):
        legacy_path = self.config_path.with_name(f"legacy-{uuid4().hex}.json")
        legacy_secret_path = legacy_path.with_name(f"{legacy_path.stem}.secret.json")
        legacy_path.write_text(json.dumps({"api_key": "legacy-secret", "base_url": "https://relay.example.com/v1"}), encoding="utf-8")
        try:
            migrated = server.ChatServer(app_password="", config_path=legacy_path)
            self.assertEqual(migrated.api_key, "legacy-secret")
            saved = json.loads(legacy_path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", saved)
            self.assertTrue(legacy_secret_path.exists())
            self.assertNotIn("legacy-secret", legacy_secret_path.read_text(encoding="utf-8"))
        finally:
            legacy_path.unlink(missing_ok=True)
            legacy_secret_path.unlink(missing_ok=True)

    def test_model_selection_persists_as_default(self):
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["custom-model"],
            },
        ):
            pass
        with self.request("/api/config/model", "POST", {"model": "shared-model"}) as response:
            self.assertEqual(json.loads(response.read())["default_model"], "shared-model")
        restarted = server.ChatServer(app_password="", config_path=self.config_path)
        self.assertEqual(restarted.default_model, "shared-model")
        self.assertIn("shared-model", restarted.configured_models)

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/config", "POST", {"api_key": "key", "base_url": "not-a-url"})
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_non_ascii_api_key_is_rejected_with_clear_error(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "/api/config",
                "POST",
                {"api_key": "API密钥：sk-test", "base_url": "https://relay.example.com/v1"},
            )
        self.assertEqual(context.exception.code, 400)
        payload = json.loads(context.exception.read().decode("utf-8"))
        self.assertIn("只能包含英文字符和数字", payload["error"])
        context.exception.close()

    def test_api_key_with_hidden_whitespace_is_rejected(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "/api/config",
                "POST",
                {"api_key": "sk-test\tvalue", "base_url": "https://relay.example.com/v1"},
            )
        self.assertEqual(context.exception.code, 400)
        payload = json.loads(context.exception.read().decode("utf-8"))
        self.assertIn("不可见字符", payload["error"])
        context.exception.close()

    def test_manual_models_are_used_when_discovery_fails(self):
        MockRelayHandler.fail_models = True
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": "relay-secret",
                "base_url": f"http://127.0.0.1:{self.relay.server_address[1]}/v1",
                "models": ["manual-model", "manual-model"],
            },
        ):
            pass

        with self.request("/api/models") as response:
            model_data = json.loads(response.read())
        self.assertEqual(model_data["models"], ["manual-model"])
        self.assertEqual(model_data["source"], "manual")

    def test_relay_error_frames_never_carry_the_api_key_to_the_browser(self):
        relay_url = f"http://127.0.0.1:{self.relay.server_address[1]}/v1"
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": SECRET_ECHO_KEY,
                "base_url": relay_url,
                "models": ["requested-model"],
                "default_model": "requested-model",
            },
        ):
            pass

        MockRelayHandler.fail_chat = True
        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "boom"}], "model": "requested-model"},
        ) as response:
            stream = response.read().decode("utf-8")

        self.assertNotIn(SECRET_ECHO_KEY, stream)
        self.assertIn("[REDACTED]", stream)
        frames = [item.strip() for item in stream.split("data: ") if item.strip() and item.strip() != "[DONE]"]
        payload = json.loads(frames[-1])
        self.assertTrue(payload["error"].startswith("Error code: 500"))
        self.assertIn("upstream failed", payload["error"])

    def test_relay_still_receives_the_key_for_successful_requests(self):
        relay_url = f"http://127.0.0.1:{self.relay.server_address[1]}/v1"
        with self.request(
            "/api/config",
            "POST",
            {
                "api_key": SECRET_ECHO_KEY,
                "base_url": relay_url,
                "models": ["requested-model"],
                "default_model": "requested-model",
            },
        ):
            pass
        with self.request(
            "/api/chat",
            "POST",
            {"messages": [{"role": "user", "content": "hello"}], "model": "requested-model"},
        ) as response:
            self.assertIn('"content": "Hello"', response.read().decode("utf-8"))
        chat_requests = [item for item in MockRelayHandler.requests if item[0] == "POST"]
        self.assertEqual(chat_requests[-1][2], f"Bearer {SECRET_ECHO_KEY}")


if __name__ == "__main__":
    unittest.main()
