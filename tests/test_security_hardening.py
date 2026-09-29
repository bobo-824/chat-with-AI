import base64
import json
import os
import stat
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import server


PASSWORD = "correct horse battery staple"
API_KEY = "sk-hardening-test-key-123456"
LOCAL_RELAY = "http://127.0.0.1:9099/v1"
DEAD_RELAY = "http://127.0.0.1:1/v1"


class ServerTestCase(unittest.TestCase):
    """Runs a real HTTP server against throwaway config/secret/history files."""

    app_password = None

    def build_chat_server(self):
        return server.ChatServer(app_password=self.app_password or "", config_path=self.config_path)

    def setUp(self):
        suffix = uuid4().hex
        self.config_path = Path(__file__).with_name(f".config-{suffix}.json")
        self.secret_path = Path(__file__).with_name(f".config-{suffix}.secret.json")
        self.history_path = Path(__file__).with_name(f".history-{suffix}.json")
        self.temp_paths = [self.config_path, self.secret_path, self.history_path]
        self.chat_server = self.build_chat_server()
        self.chat_server.conversations = server.ConversationStore(self.history_path)
        server.Handler.chat_server = self.chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        for path in self.temp_paths:
            path.unlink(missing_ok=True)

    def request(self, path, method="GET", payload=None, headers=None, cookie=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"} if body else {}
        request_headers.update(headers or {})
        if cookie:
            request_headers["Cookie"] = cookie
        request = Request(self.base_url + path, data=body, method=method, headers=request_headers)
        return urlopen(request, timeout=10)

    def exchange(self, path, method="GET", **kwargs):
        try:
            with self.request(path, method, **kwargs) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except HTTPError as error:
            with error:
                return error.code, error.read().decode("utf-8"), error.headers

    def login_header(self, headers=None):
        status, _text, response_headers = self.exchange(
            "/api/login", "POST", payload={"password": PASSWORD}, headers=headers
        )
        self.assertEqual(status, 200)
        return response_headers["Set-Cookie"]

    def save_config(self, base_url=LOCAL_RELAY, api_key=API_KEY):
        return self.exchange(
            "/api/config",
            "POST",
            payload={"api_key": api_key, "base_url": base_url, "models": ["model-a"]},
        )


class CredentialHelperStoreTest(unittest.TestCase):
    """SecretStore must prefer an OS credential helper over any on-disk encoding."""

    def temp_path(self):
        path = Path(__file__).with_name(f".config-{uuid4().hex}.secret.json")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def helper(self):
        vault = {}
        calls = []

        def run(arguments, secret=None):
            argv = list(arguments)
            calls.append((argv, secret))
            if "-a" in argv:
                account = argv[argv.index("-a") + 1]
            elif "account" in argv:
                account = argv[argv.index("account") + 1]
            else:
                return 2, b""
            if "delete-generic-password" in argv or "clear" in argv:
                vault.pop(account, None)
                return 0, b""
            if "add-generic-password" in argv or "store" in argv:
                if not secret:
                    return 1, b""
                vault[account] = secret
                return 0, b""
            stored = vault.get(account)
            return (0, stored.encode("utf-8")) if stored else (1, b"")

        return vault, calls, run

    def test_key_is_written_to_and_read_from_the_os_keyring(self):
        for backend in ("macos-keychain", "libsecret"):
            with self.subTest(backend=backend):
                path = self.temp_path()
                vault, calls, run = self.helper()
                with patch.object(server, "secret_backend_name", lambda: backend):
                    with patch.object(server, "run_secret_command", run):
                        store = server.SecretStore(path)
                        store.save(API_KEY)
                        self.assertEqual(store.load(), API_KEY)
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["protection"], backend)
                self.assertEqual(record["value"], server.secret_reference(path)[1])
                self.assertNotIn(API_KEY, path.read_text(encoding="utf-8"))
                self.assertIn(API_KEY, set(vault.values()))
                for argv, secret in calls:
                    self.assertNotIn(API_KEY, argv)
                    self.assertNotIn(API_KEY, " ".join(argv))
                store_calls = [entry for entry in calls if entry[1] is not None]
                self.assertTrue(store_calls)
                self.assertEqual(store_calls[0][1], API_KEY)

    def test_saving_a_rotated_key_replaces_the_old_keyring_entry(self):
        path = self.temp_path()
        vault, calls, run = self.helper()
        with patch.object(server, "secret_backend_name", lambda: "libsecret"):
            with patch.object(server, "run_secret_command", run):
                store = server.SecretStore(path)
                store.save(API_KEY)
                store.save("sk-rotated-key-999888777")
                self.assertEqual(store.load(), "sk-rotated-key-999888777")
        self.assertEqual(len(vault), 1)
        self.assertEqual(list(vault.values()), ["sk-rotated-key-999888777"])
        self.assertNotIn(API_KEY, json.dumps(list(vault.values())))

    def test_unavailable_helper_falls_back_to_a_private_file(self):
        path = self.temp_path()
        failing = lambda arguments, secret=None: (1, b"")
        with patch.object(server, "secret_backend_name", lambda: "libsecret"):
            with patch.object(server, "run_secret_command", failing):
                store = server.SecretStore(path)
                store.save(API_KEY)
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["protection"], "file")
                self.assertNotIn(API_KEY, path.read_text(encoding="utf-8"))
                self.assertEqual(store.load(), API_KEY)
        reopened = server.SecretStore(path)
        self.assertEqual(reopened.load(), API_KEY)

    def test_helper_references_are_bound_to_their_own_secret_file(self):
        first = self.temp_path()
        second = self.temp_path()
        vault, _calls, run = self.helper()
        with patch.object(server, "secret_backend_name", lambda: "libsecret"):
            with patch.object(server, "run_secret_command", run):
                server.SecretStore(first).save(API_KEY)
                second.write_bytes(first.read_bytes())
                self.assertIsNone(server.SecretStore(second).load())
                self.assertEqual(server.SecretStore(first).load(), API_KEY)
                self.assertIn(server.secret_reference(first)[1], vault)

    def test_legacy_file_records_still_load_after_the_upgrade(self):
        path = self.temp_path()
        path.write_text(
            json.dumps({"version": 1, "protection": "file", "value": base64.b64encode(API_KEY.encode()).decode()}),
            encoding="utf-8",
        )
        vault, _calls, run = self.helper()
        with patch.object(server, "secret_backend_name", lambda: "macos-keychain"):
            with patch.object(server, "run_secret_command", run):
                self.assertEqual(server.SecretStore(path).load(), API_KEY)

    def test_malformed_secret_files_are_ignored(self):
        path = self.temp_path()
        for payload in ('{"version": 1}', '{"protection": "file", "value": "!!!"}', "not json"):
            path.write_text(payload, encoding="utf-8")
            with patch.object(server, "secret_backend_name", lambda: "file"):
                self.assertIsNone(server.SecretStore(path).load())

    @unittest.skipIf(os.name != "nt", "DPAPI is Windows only")
    def test_dpapi_round_trip_keeps_the_key_off_disk(self):
        path = self.temp_path()
        with patch.object(server, "secret_backend_name", lambda: "windows-dpapi"):
            store = server.SecretStore(path)
            store.save(API_KEY)
            self.assertEqual(store.load(), API_KEY)
            self.assertNotIn(API_KEY, path.read_text(encoding="utf-8"))


class PrivateFilePermissionTest(ServerTestCase):
    def test_every_sensitive_write_tightens_permissions(self):
        touched = []
        original = server.restrict_file_permissions

        def spy(path):
            touched.append(Path(path))
            return original(path)

        with patch.object(server, "restrict_file_permissions", spy):
            self.chat_server.configure(API_KEY, LOCAL_RELAY, ["model-a"], "model-a")
            self.chat_server.conversations.append_message("conversation-1", "user", "hello")
            self.chat_server.select_default_model("model-b")
        self.assertIn(self.config_path, touched)
        self.assertIn(self.secret_path, touched)
        self.assertIn(self.history_path, touched)

    def test_atomic_temporary_files_are_removed(self):
        self.chat_server.configure(API_KEY, LOCAL_RELAY, ["model-a"], "model-a")
        leftovers = [item.name for item in self.config_path.parent.glob("*.tmp")]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    def test_sensitive_files_are_owner_only_on_posix(self):
        self.chat_server.configure(API_KEY, LOCAL_RELAY, ["model-a"], "model-a")
        self.chat_server.conversations.append_message("conversation-1", "user", "hello")
        for path in (self.config_path, self.secret_path, self.history_path):
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), server.FILE_PRIVATE_MODE)


class SessionCookieTest(ServerTestCase):
    app_password = PASSWORD

    def test_loopback_session_cookie_stays_usable_over_http(self):
        header = self.login_header()
        self.assertTrue(header.startswith(server.SESSION_COOKIE_NAME + "="))
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertIn(f"Max-Age={server.SESSION_MAX_AGE_SECONDS}", header)
        self.assertNotIn("Secure", header)
        cookie = header.split(";", 1)[0]
        status, _text, _headers = self.exchange("/api/config", cookie=cookie)
        self.assertEqual(status, 200)

    def test_https_proxy_upgrades_the_session_cookie(self):
        header = self.login_header(headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", header)

    def test_plain_http_behind_a_proxy_is_not_marked_secure(self):
        header = self.login_header(headers={"X-Forwarded-Proto": "http, https"})
        self.assertNotIn("Secure", header)

    def test_cookie_secure_environment_switch(self):
        with patch.dict(os.environ, {"COOKIE_SECURE": "1"}):
            self.assertIn("Secure", self.login_header())
        with patch.dict(os.environ, {"COOKIE_SECURE": "0"}):
            self.assertNotIn("Secure", self.login_header(headers={"X-Forwarded-Proto": "https"}))

    def test_logout_clears_the_cookie_with_the_same_attributes(self):
        cookie = self.login_header().split(";", 1)[0]
        status, _text, headers = self.exchange("/api/logout", "POST", cookie=cookie)
        self.assertEqual(status, 200)
        cleared = headers["Set-Cookie"]
        self.assertTrue(cleared.startswith(server.SESSION_COOKIE_NAME + "="))
        self.assertIn("HttpOnly", cleared)
        self.assertIn("SameSite=Strict", cleared)
        self.assertIn("Max-Age=0", cleared)
        status, _text, _headers = self.exchange("/api/config", cookie=cookie)
        self.assertEqual(status, 401)

    def test_session_lifetime_matches_the_cookie_lifetime(self):
        token = self.chat_server.create_session()
        expires_at = self.chat_server.sessions[token]
        self.assertAlmostEqual(
            expires_at - time.time(), server.SESSION_MAX_AGE_SECONDS, delta=5
        )
        self.chat_server.sessions[token] = time.time() - 1
        status, _text, _headers = self.exchange(
            "/api/config", cookie=server.SESSION_COOKIE_NAME + "=" + token
        )
        self.assertEqual(status, 401)


class ApiKeyRevealTest(ServerTestCase):
    def test_reveal_requires_an_explicit_confirmation_header(self):
        self.save_config()
        status, text, _headers = self.exchange("/api/config/key")
        self.assertEqual(status, 400)
        self.assertNotIn(API_KEY, text)

    def test_reveal_needs_the_expected_confirmation_value(self):
        self.save_config()
        status, text, _headers = self.exchange("/api/config/key", headers={"X-Reveal-Api-Key": "yes-please"})
        self.assertEqual(status, 400)
        self.assertNotIn(API_KEY, text)

    def test_confirmed_reveal_returns_the_key_once(self):
        self.save_config()
        status, text, headers = self.exchange("/api/config/key", headers={"X-Reveal-Api-Key": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["api_key"], API_KEY)
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")

    def test_reveal_can_be_switched_off_for_a_deployment(self):
        self.save_config()
        with patch.dict(os.environ, {"ALLOW_KEY_REVEAL": "0"}):
            status, text, _headers = self.exchange("/api/config/key", headers={"X-Reveal-Api-Key": "1"})
            self.assertEqual(status, 404)
            self.assertNotIn(API_KEY, text)
            _status, summary, _headers = self.exchange("/api/config")
            self.assertFalse(json.loads(summary)["key_reveal_enabled"])
        _status, summary, _headers = self.exchange("/api/config")
        self.assertTrue(json.loads(summary)["key_reveal_enabled"])

    def test_config_summary_only_ever_shares_a_hint(self):
        self.save_config()
        status, text, _headers = self.exchange("/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn(API_KEY, text)
        self.assertIn("api_key_hint", text)


class RateLimitEndpointTest(ServerTestCase):
    def rebuild(self, windows):
        with patch.dict(server.RATE_LIMIT_WINDOWS, windows, clear=False):
            chat_server = server.ChatServer(app_password="", config_path=self.config_path)
        chat_server.conversations = server.ConversationStore(self.history_path)
        server.Handler.chat_server = chat_server
        self.chat_server = chat_server

    def test_repeated_config_writes_are_throttled(self):
        self.rebuild({"config_write": (3, 60.0)})
        for _ in range(3):
            status, _text, _headers = self.save_config()
            self.assertEqual(status, 200)
        status, text, headers = self.save_config()
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertNotIn(API_KEY, text)

    def test_throttled_write_does_not_change_the_saved_configuration(self):
        self.rebuild({"config_write": (2, 60.0)})
        self.save_config(api_key="first-key-1234567890")
        self.save_config(api_key="second-key-1234567890")
        status, _text, _headers = self.save_config(api_key="third-key-123456789")
        self.assertEqual(status, 429)
        self.assertEqual(self.chat_server.api_key, "second-key-1234567890")

    def test_model_selection_shares_the_configuration_budget(self):
        self.rebuild({"config_write": (2, 60.0)})
        self.save_config()
        status, _text, _headers = self.exchange(
            "/api/config/model", "POST", payload={"model": "model-b", "models": ["model-b"]}
        )
        self.assertEqual(status, 200)
        status, _text, _headers = self.exchange(
            "/api/config/model", "POST", payload={"model": "model-c", "models": ["model-c"]}
        )
        self.assertEqual(status, 429)

    def test_chat_requests_are_throttled(self):
        self.rebuild({"chat": (2, 60.0)})
        payload = {"messages": [{"role": "user", "content": "hello"}], "model": "model-a"}
        for _ in range(2):
            status, _text, _headers = self.exchange("/api/chat", "POST", payload=payload)
            self.assertEqual(status, 409)
        status, text, headers = self.exchange("/api/chat", "POST", payload=payload)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)

    def test_read_only_endpoints_are_not_throttled(self):
        self.rebuild({"config_write": (1, 60.0)})
        self.save_config()
        for _ in range(6):
            status, _text, _headers = self.exchange("/api/conversations")
            self.assertEqual(status, 200)


class RateLimiterUnitTest(unittest.TestCase):
    def test_window_expiry_restores_the_budget(self):
        limiter = server.RateLimiter({"chat": (2, 30.0)})
        self.assertTrue(limiter.check("chat", "client", now=100.0)[0])
        self.assertTrue(limiter.check("chat", "client", now=110.0)[0])
        allowed, retry_after = limiter.check("chat", "client", now=115.0)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)
        self.assertTrue(limiter.check("chat", "client", now=131.0)[0])

    def test_clients_have_separate_budgets(self):
        limiter = server.RateLimiter({"chat": (1, 60.0)})
        self.assertTrue(limiter.check("chat", "one")[0])
        self.assertFalse(limiter.check("chat", "one")[0])
        self.assertTrue(limiter.check("chat", "two")[0])

    def test_unlisted_actions_are_allowed(self):
        self.assertTrue(server.RateLimiter({}).check("models", "client")[0])

    def test_stale_clients_are_pruned(self):
        limiter = server.RateLimiter({"chat": (5, 10.0)})
        for index in range(server.RATE_LIMIT_TRACKED_KEYS + 8):
            limiter.check("chat", f"client-{index}", now=0.0)
        self.assertGreater(len(limiter.events), server.RATE_LIMIT_TRACKED_KEYS)
        limiter.check("chat", "recent", now=1000.0)
        self.assertLessEqual(len(limiter.events), server.RATE_LIMIT_TRACKED_KEYS)

    def test_rate_limit_keys_hide_the_session_token(self):
        chat_server = server.ChatServer(app_password="", config_path=self.temp_path())
        key = chat_server.rate_limit_key("super-secret-session-token", "203.0.113.9")
        self.assertNotIn("super-secret-session-token", key)
        self.assertTrue(key.startswith("session:"))
        self.assertEqual(chat_server.rate_limit_key("", "203.0.113.9"), "ip:203.0.113.9")

    @staticmethod
    def temp_path():
        path = Path(__file__).with_name(f".config-{uuid4().hex}.json")
        path.unlink(missing_ok=True)
        return path


class RelayAddressGuardTest(unittest.TestCase):
    def resolver(self, addresses):
        return lambda host: set(addresses)

    def test_a_hostname_pointing_at_the_metadata_service_is_rejected(self):
        for address in ("169.254.169.254", "fd00:ec2::254"):
            with self.subTest(address=address):
                with patch.object(server, "resolve_host_addresses", self.resolver([address])):
                    with self.assertRaises(ValueError):
                        server.validate_relay_base_url("https://relay.example.com/v1")

    def test_named_metadata_hosts_are_rejected_without_querying_dns(self):
        for host in ("metadata.google.internal", "metadata.goog", "metadata.arm.cloud"):
            with self.subTest(host=host):
                with patch.object(server, "resolve_host_addresses", return_value=None) as lookup:
                    with self.assertRaises(ValueError):
                        server.validate_relay_base_url(f"https://{host}/v1")
                    lookup.assert_not_called()

    def test_local_and_lan_model_servers_keep_working(self):
        for host, address in (
            ("lmstudio.local", "192.168.0.50"),
            ("desktop-box.local", "127.0.0.1"),
            ("intranet.example.com", "10.8.0.7"),
        ):
            with self.subTest(host=host):
                candidate = f"http://{host}:11434/v1"
                with patch.object(server, "resolve_host_addresses", self.resolver([address])):
                    self.assertEqual(server.validate_relay_base_url(candidate), candidate)

    def test_dns_failures_do_not_block_configuration(self):
        for addresses in (None, set()):
            with self.subTest(addresses=addresses):
                candidate = "https://relay.example.com/v1"
                with patch.object(server, "resolve_host_addresses", lambda host: addresses):
                    self.assertEqual(server.validate_relay_base_url(candidate), candidate)

    def test_the_resolved_address_check_can_be_disabled(self):
        with patch.dict(os.environ, {"RELAY_CHECK_RESOLVED_ADDRESS": "0"}, clear=False):
            with patch.object(server, "resolve_host_addresses", return_value=None) as lookup:
                candidate = "https://relay.example.com/v1"
                self.assertEqual(server.validate_relay_base_url(candidate), candidate)
                lookup.assert_not_called()

    def test_ipv4_mapped_link_local_literals_are_rejected(self):
        self.assertTrue(server.is_blocked_relay_host("::ffff:169.254.169.254"))
        self.assertFalse(server.is_blocked_relay_host("::ffff:127.0.0.1"))

    def test_ipv6_unique_local_metadata_endpoints_are_rejected(self):
        for address in ("fd00:ec2::254", "fc00::1"):
            with self.subTest(address=address):
                self.assertTrue(server.is_blocked_relay_host(address))
                with self.assertRaises(ValueError):
                    server.validate_relay_base_url(f"http://[{address}]:80/v1")

    def test_ipv6_loopback_model_servers_keep_working(self):
        self.assertFalse(server.is_blocked_relay_host("::1"))
        candidate = "http://[::1]:11434/v1"
        self.assertEqual(server.validate_relay_base_url(candidate), candidate)


class ErrorRedactionTest(ServerTestCase):
    def test_transport_errors_hide_paths_and_credentials(self):
        self.save_config(base_url=DEAD_RELAY)
        status, text, _headers = self.exchange(
            "/api/chat",
            "POST",
            payload={"messages": [{"role": "user", "content": "hello"}], "model": "model-a"},
        )
        self.assertEqual(status, 200)
        self.assertIn('"error"', text)
        self.assertNotIn(API_KEY, text)
        for marker in {str(Path.home()), os.getcwd(), str(Path("server.py").resolve())}:
            if len(marker) >= 3:
                self.assertNotIn(marker, text)

    def test_internal_paths_are_replaced_with_a_placeholder(self):
        leaked = str(Path.home()) + os.sep + "private-app-config.json"
        message = server.describe_error(Exception("cannot open " + leaked))
        self.assertNotIn("private-app-config.json", message)
        self.assertIn("[PATH]", message)

    def test_cookies_and_quoted_secrets_are_redacted(self):
        message = server.describe_error(
            Exception(
                "failed with Set-Cookie: chat_session=abcdef1234567890 "
                "and password: \"correct horse battery staple\""
            )
        )
        self.assertNotIn("abcdef1234567890", message)
        self.assertNotIn("correct horse battery staple", message)

    def test_the_app_password_never_comes_back_in_an_error(self):
        chat_server = server.ChatServer(
            app_password="super-secret-login-passphrase",
            config_path=Path(__file__).with_name(f".config-{uuid4().hex}.json"),
        )
        self.addCleanup(
            chat_server.config_store.path.unlink, missing_ok=True
        )
        self.addCleanup(chat_server.secret_store.path.unlink, missing_ok=True)
        message = chat_server.safe_error_message(Exception("login with super-secret-login-passphrase failed"))
        self.assertNotIn("super-secret-login-passphrase", message)

    def test_useful_diagnostics_are_preserved(self):
        message = server.describe_error(Exception("Error code: 404 - model not found: gpt-4o-mini"))
        self.assertIn("Error code: 404", message)
        self.assertIn("model not found: gpt-4o-mini", message)
        header = server.redact_relay_debug({"x-request-id": "relay-request-123"}, API_KEY)
        self.assertEqual(header["x-request-id"], "relay-request-123")

    def test_persistence_failings_return_a_clean_error(self):
        self.save_config()
        leak = "cannot write " + str(Path.home()) + os.sep + "app-config.json"
        with patch.object(server.AppConfigStore, "save", side_effect=OSError(leak)):
            status, text, _headers = self.exchange(
                "/api/config",
                "POST",
                payload={"api_key": API_KEY, "base_url": LOCAL_RELAY, "models": ["model-a"]},
            )
            self.assertEqual(status, 500)
            self.assertNotIn(API_KEY, text)
            self.assertNotIn(str(Path.home()), text)
            self.assertIn("[PATH]", text)
            status, text, _headers = self.exchange(
                "/api/config/model", "POST", payload={"model": "model-b"}
            )
            self.assertEqual(status, 500)
            self.assertNotIn(str(Path.home()), text)
        status, _text, _headers = self.exchange("/api/config")
        self.assertEqual(status, 200)

    def test_relay_debug_never_prints_the_key(self):
        dumped = server.redact_relay_debug(
            {"authorization": "Bearer " + API_KEY, "notes": "kept"}, API_KEY
        )
        self.assertNotIn(API_KEY, json.dumps(dumped))
        self.assertEqual(dumped["notes"], "kept")

class RelayTimeoutTest(unittest.TestCase):
    """Upstream relay calls must fail fast instead of holding a browser socket."""

    def build_chat_server(self):
        config_path = Path(__file__).with_name(f".timeout-{uuid4().hex}.json")
        self.addCleanup(config_path.unlink, missing_ok=True)
        self.addCleanup(config_path.with_name(config_path.stem + ".secret.json").unlink, missing_ok=True)
        chat_server = server.ChatServer(app_password="", config_path=config_path)
        chat_server.api_key = API_KEY
        chat_server.base_url = LOCAL_RELAY
        return chat_server

    def test_timeouts_are_configurable_and_reject_nonsense(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.relay_timeout_seconds(), server.RELAY_TIMEOUT_SECONDS)
            self.assertEqual(server.relay_discovery_timeout_seconds(), server.RELAY_DISCOVERY_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {"RELAY_TIMEOUT_SECONDS": "7", "RELAY_DISCOVERY_TIMEOUT_SECONDS": "2"}):
            self.assertEqual(server.relay_timeout_seconds(), 7.0)
            self.assertEqual(server.relay_discovery_timeout_seconds(), 2.0)
        with patch.dict(os.environ, {"RELAY_TIMEOUT_SECONDS": "soon", "RELAY_DISCOVERY_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(server.relay_timeout_seconds(), server.RELAY_TIMEOUT_SECONDS)
            self.assertEqual(server.relay_discovery_timeout_seconds(), server.RELAY_DISCOVERY_TIMEOUT_SECONDS)

    def test_relay_client_is_built_with_a_bounded_timeout(self):
        captured = {}

        class RecordingOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        chat_server = self.build_chat_server()
        with patch.object(server, "OpenAI", RecordingOpenAI):
            chat_server.ensure_client()
        self.assertIn("timeout", captured)
        self.assertIn("max_retries", captured)
        self.assertLessEqual(captured["timeout"], 120.0)
        self.assertLess(captured["max_retries"], 3)

    def test_model_discovery_bounds_its_own_request(self):
        seen = {}

        class RecordingModels:
            def list(self, **kwargs):
                seen.update(kwargs)
                return []

        class RecordingClient:
            models = RecordingModels()

        chat_server = self.build_chat_server()
        chat_server.client = RecordingClient()
        summary = chat_server.list_models()
        self.assertIn("timeout", seen)
        self.assertLessEqual(seen["timeout"], 30.0)
        self.assertLess(seen["timeout"], server.relay_timeout_seconds())
        self.assertEqual(summary["source"], "manual")



if __name__ == "__main__":
    unittest.main()
