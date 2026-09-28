import json
import os
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import server


PASSWORD = "correct horse battery staple"
ENVIRONMENT_KEY = "sk-from-environment-only-123456"


class SecurityTest(unittest.TestCase):
    def setUp(self):
        suffix = uuid4().hex
        self.config_path = Path(__file__).with_name(f".config-{suffix}.json")
        self.secret_path = Path(__file__).with_name(f".config-{suffix}.secret.json")
        self.history_path = Path(__file__).with_name(f".history-{suffix}.json")
        chat_server = server.ChatServer(
            app_password=PASSWORD,
            config_path=self.config_path,
        )
        chat_server.conversations = server.ConversationStore(self.history_path)
        self.chat_server = chat_server
        server.Handler.chat_server = chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"
        self.port = self.app.server_address[1]

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        for path in (self.config_path, self.secret_path, self.history_path):
            path.unlink(missing_ok=True)

    def request(
        self,
        path,
        method="GET",
        payload=None,
        cookie=None,
        headers=None,
        host=None,
        content_type="application/json",
    ):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": content_type} if body else {}
        request_headers.update(headers or {})
        if cookie:
            request_headers["Cookie"] = cookie
        request = Request(self.base_url + path, data=body, method=method, headers=request_headers)
        if host:
            request.add_header("Host", host)
        return urlopen(request, timeout=5)

    def login(self):
        with self.request("/api/login", "POST", {"password": PASSWORD}) as response:
            return response.headers["Set-Cookie"].split(";", 1)[0]

    def post_error(self, path, payload, **kwargs):
        try:
            with self.request(path, "POST", payload, **kwargs) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            with error:
                return error.code, error.read().decode("utf-8")

    def get_error(self, path, **kwargs):
        try:
            with self.request(path, **kwargs) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            with error:
                return error.code, error.read().decode("utf-8")

    def test_protected_api_requires_login(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/conversations")
        self.assertEqual(context.exception.code, 401)
        self.assertEqual(context.exception.headers["X-Frame-Options"], "DENY")
        context.exception.close()

    def test_login_cookie_authorizes_and_logout_revokes(self):
        with self.request("/api/login", "POST", {"password": PASSWORD}) as response:
            cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        session_cookie = cookie.split(";", 1)[0]

        with self.request("/api/conversations", cookie=session_cookie) as response:
            self.assertEqual(response.status, 200)

        with self.request("/api/logout", "POST", cookie=session_cookie):
            pass
        with self.assertRaises(HTTPError) as context:
            self.request("/api/conversations", cookie=session_cookie)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/login", "POST", {"password": "wrong"})
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_cross_origin_config_change_is_rejected(self):
        cookie = self.login()
        status, text = self.post_error(
            "/api/config",
            {
                "api_key": "attacker-key",
                "base_url": "http://attacker.example/v1",
                "models": ["attacker-model"],
                "default_model": "attacker-model",
            },
            cookie=cookie,
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertNotIn("attacker", json.dumps({"base_url": self.chat_server.base_url}))
        self.assertFalse(self.secret_path.exists())

    def test_cross_site_sec_fetch_header_is_rejected(self):
        cookie = self.login()
        status, _ = self.post_error(
            "/api/config",
            {"base_url": "http://attacker.example/v1", "models": ["m"]},
            cookie=cookie,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.chat_server.base_url)

    def test_form_style_non_json_content_type_is_rejected(self):
        cookie = self.login()
        status, _ = self.post_error(
            "/api/config",
            {"base_url": "http://attacker.example/v1", "models": ["m"]},
            cookie=cookie,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertIsNone(self.chat_server.base_url)

    def test_rejected_request_keeps_the_connection_usable(self):
        cookie = self.login()
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        body = json.dumps({"base_url": "http://attacker.example/v1", "models": ["m"]})
        connection.request(
            "POST",
            "/api/config",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
                "Cookie": cookie,
            },
        )
        rejected = connection.getresponse()
        rejected_status = rejected.status
        rejected.read()
        connection.request("GET", "/api/session", headers={"Cookie": cookie})
        following = connection.getresponse()
        following_status = following.status
        following.read()
        self.assertEqual(rejected_status, 403)
        self.assertEqual(following_status, 200)

    def test_throttled_login_returns_a_clean_response(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        statuses = []
        for attempt in range(server.LOGIN_MAX_FAILED_ATTEMPTS + 1):
            connection.request(
                "POST",
                "/api/login",
                body=json.dumps({"password": "guess-%d" % attempt}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            statuses.append(response.status)
            response.read()
        connection.request("GET", "/api/health")
        health = connection.getresponse()
        health_status = health.status
        health.read()
        self.assertEqual(statuses, [401] * server.LOGIN_MAX_FAILED_ATTEMPTS + [429])
        self.assertEqual(health_status, 200)

    def test_same_origin_config_change_with_origin_header_still_works(self):
        cookie = self.login()
        status, text = self.post_error(
            "/api/config",
            {
                "api_key": "relay-secret",
                "base_url": "http://127.0.0.1:9099/v1",
                "models": ["custom-model"],
                "default_model": "custom-model",
            },
            cookie=cookie,
            headers={"Origin": self.base_url, "Referer": self.base_url + "/"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(text)["configured"])
        self.assertEqual(self.chat_server.base_url, "http://127.0.0.1:9099/v1")
        self.assertTrue(self.secret_path.exists())

    def test_untrusted_host_header_cannot_read_api_key(self):
        cookie = self.login()
        self.post_error(
            "/api/config",
            {
                "api_key": "relay-secret",
                "base_url": "http://127.0.0.1:9099/v1",
                "models": ["custom-model"],
            },
            cookie=cookie,
        )
        for host in ("rebind.attacker.example", "attacker.example.com:%d" % self.port):
            status, text = self.get_error("/api/config/key", cookie=cookie, host=host)
            self.assertEqual(status, 400)
            self.assertNotIn("relay-secret", text)
        status, text = self.get_error(
            "/api/config/key",
            cookie=cookie,
            headers={"X-Reveal-Api-Key": "1"},
        )
        self.assertEqual(status, 200)
        self.assertIn("relay-secret", text)

    def test_allowed_hosts_env_var_permits_custom_domain(self):
        cookie = self.login()
        with patch.dict(os.environ, {"ALLOWED_HOSTS": "chat.example.com,*.none"}):
            status, text = self.get_error("/api/session", host="chat.example.com:%d" % self.port)
        self.assertEqual(status, 200)
        self.assertIn("authenticated", text)

    def test_login_locks_out_after_repeated_failures(self):
        for attempt in range(server.LOGIN_MAX_FAILED_ATTEMPTS):
            status, _ = self.post_error("/api/login", {"password": "guess-%d" % attempt})
            self.assertEqual(status, 401)
        status, text = self.post_error("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 429)
        self.assertNotIn("chat_session", text)
        self.chat_server.clear_login_failures("127.0.0.1")
        status, _ = self.post_error("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)

    def test_successful_login_clears_failed_attempts(self):
        failures = server.LOGIN_MAX_FAILED_ATTEMPTS - 1
        for attempt in range(failures):
            self.post_error("/api/login", {"password": "guess-%d" % attempt})
        self.assertEqual(self.login().count("chat_session="), 1)
        for attempt in range(failures):
            self.post_error("/api/login", {"password": "guess-%d" % attempt})
        status, _ = self.post_error("/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)

    def test_loopback_detection(self):
        self.assertTrue(server.is_loopback_host("127.0.0.1"))
        self.assertTrue(server.is_loopback_host("::1"))
        self.assertTrue(server.is_loopback_host("localhost"))
        self.assertFalse(server.is_loopback_host("0.0.0.0"))
        self.assertFalse(server.is_loopback_host("192.168.1.20"))

    def test_lan_startup_requires_password(self):
        with patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "0"}, clear=True):
            self.assertEqual(server.main(), 1)


class RelayUrlValidationTest(unittest.TestCase):
    def test_rejects_internal_and_malformed_relay_targets(self):
        for value in (
            "http://169.254.169.254/latest/meta-data/",
            "http://[::ffff:169.254.169.254]/v1",
            "http://user:pass@127.0.0.1:11434/v1",
            "http://0.0.0.0/v1",
            "https://224.0.0.1/v1",
            "not-a-url",
            "file:///etc/passwd",
            "http://",
            "http://127.0.0.1:99999/v1",
            "http://127.0.0.1:8080 /v1",
            "http://127.0.0.1:8080/v1\nhttp://evil.example",
            "",
            None,
        ):
            with self.assertRaises(ValueError, msg=repr(value)):
                server.validate_relay_base_url(value)

    def test_accepts_real_relay_targets(self):
        for value in (
            "https://relay.example.com/v1",
            "http://127.0.0.1:11434/v1",
            "http://localhost:8000/v1",
            "http://192.168.1.30:11434/v1",
            "https://[::1]:8443/v1",
        ):
            self.assertEqual(server.validate_relay_base_url(value), value)

    def test_trusted_host_rules(self):
        for value in ("127.0.0.1:8000", "[::1]:8000", "localhost:8000", "192.168.1.20:8000"):
            self.assertTrue(server.is_trusted_host_header(value), value)
        for value in ("", "attacker.example", "attacker.example:8000", "rebind.attacker.example:%d" % 8000):
            self.assertFalse(server.is_trusted_host_header(value), value)

    def test_allowed_hosts_env_var_extends_the_host_allowlist(self):
        with patch.dict(os.environ, {"ALLOWED_HOSTS": "chat.example.com, other.example.com"}):
            self.assertTrue(server.is_trusted_host_header("chat.example.com:8000"))
            self.assertTrue(server.is_trusted_host_header("other.example.com"))
            self.assertFalse(server.is_trusted_host_header("evil.example.com"))

    def test_error_messages_are_redacted(self):
        key = "sk-visible-key-1234567890"
        raw = RuntimeError("upstream said Bearer " + key + " and sk-another-secret-key-999")
        message = server.describe_error(raw, key)
        self.assertNotIn(key, message)
        self.assertNotIn("sk-another-secret-key-999", message)
        self.assertIn("[REDACTED]", message)


class EnvironmentKeyPersistenceTest(unittest.TestCase):
    def setUp(self):
        suffix = uuid4().hex
        self.config_path = Path(__file__).with_name(f".config-{suffix}.json")
        self.secret_path = Path(__file__).with_name(f".config-{suffix}.secret.json")
        self.history_path = Path(__file__).with_name(f".history-{suffix}.json")
        self.environment = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": ENVIRONMENT_KEY,
                "OPENAI_BASE_URL": "",
                "OPENAI_MODEL": "",
                "OPENAI_MODELS": "",
            },
        )
        self.environment.start()
        chat_server = server.ChatServer(app_password="", config_path=self.config_path)
        chat_server.conversations = server.ConversationStore(self.history_path)
        self.chat_server = chat_server
        server.Handler.chat_server = chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

    def tearDown(self):
        self.environment.stop()
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        for path in (self.config_path, self.secret_path, self.history_path):
            path.unlink(missing_ok=True)

    def post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            with error:
                return error.code, error.read().decode("utf-8")

    def test_environment_key_stays_out_of_the_secret_store(self):
        self.assertEqual(self.chat_server.api_key, ENVIRONMENT_KEY)
        status, _ = self.post(
            "/api/config",
            {
                "base_url": "http://127.0.0.1:9099/v1",
                "models": ["custom-model"],
                "default_model": "custom-model",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.secret_path.exists())
        self.assertEqual(self.chat_server.api_key, ENVIRONMENT_KEY)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["base_url"], "http://127.0.0.1:9099/v1")
        self.assertNotIn("api_key", saved)

    def test_key_supplied_by_the_request_is_still_persisted(self):
        status, _ = self.post(
            "/api/config",
            {
                "api_key": "relay-secret",
                "base_url": "http://127.0.0.1:9099/v1",
                "models": ["custom-model"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.secret_path.exists())
        self.assertEqual(server.SecretStore(self.secret_path).load(), "relay-secret")


if __name__ == "__main__":
    unittest.main()
