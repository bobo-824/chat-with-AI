import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import server

from test_security_hardening import ServerTestCase


API_KEY = "sk-release-readiness-key-123456"
ROOT = Path(__file__).parents[1]
RUNTIME_FILES = ("app-config.json", "app-secret.json", "chat-history.json")

STORE_SCRIPT = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
LOOKUP_SCRIPT = "import sys, pathlib; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())"
DELETE_SCRIPT = "import sys, pathlib; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)"


def temp_name(prefix):
    return Path(__file__).with_name(f"{prefix}-{uuid4().hex}.json")


class CredentialHelperProcessTest(unittest.TestCase):
    """The credential helper talks to a real subprocess, so bytes contracts matter."""

    def test_secret_is_delivered_on_stdin_and_read_back(self):
        vault = temp_name(".vault")
        try:
            returncode, _output = server.run_secret_command(
                [sys.executable, "-c", STORE_SCRIPT, str(vault)], secret=API_KEY
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(vault.read_bytes(), API_KEY.encode("utf-8"))
            returncode, output = server.run_secret_command(
                [sys.executable, "-c", LOOKUP_SCRIPT, str(vault)]
            )
            self.assertEqual(returncode, 0)
            self.assertEqual(output.decode("utf-8"), API_KEY)
        finally:
            vault.unlink(missing_ok=True)

    def test_helper_failure_is_reported_not_raised(self):
        returncode, output = server.run_secret_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"], secret=API_KEY
        )
        self.assertEqual(returncode, 3)
        self.assertEqual(output, b"")
        returncode, output = server.run_secret_command(["this-command-does-not-exist-anywhere"])
        self.assertIsNone(returncode)
        self.assertEqual(output, b"")

    def test_secret_store_round_trip_uses_a_real_helper_process(self):
        config_path = temp_name(".config")
        secret_path = Path(str(config_path).replace(".json", ".secret.json"))
        vault = temp_name(".vault")
        helpers = {
            "secret_store_arguments": lambda backend, service, account: [
                sys.executable, "-c", STORE_SCRIPT, str(vault)
            ],
            "secret_lookup_arguments": lambda backend, service, account: [
                sys.executable, "-c", LOOKUP_SCRIPT, str(vault)
            ],
            "secret_delete_arguments": lambda backend, service, account: [
                sys.executable, "-c", DELETE_SCRIPT, str(vault)
            ],
        }
        try:
            self.enterContext(patch.object(server, "secret_backend_name", lambda: "libsecret"))
            for name, replacement in helpers.items():
                self.enterContext(patch.object(server, name, replacement))
            store = server.SecretStore(secret_path)
            store.save(API_KEY)
            self.assertEqual(store.load(), API_KEY)
            record = json.loads(secret_path.read_text(encoding="utf-8"))
            self.assertEqual(record["protection"], "libsecret")
            self.assertNotIn(API_KEY, secret_path.read_text(encoding="utf-8"))
            self.assertEqual(vault.read_bytes(), API_KEY.encode("utf-8"))

            store.save("sk-rotated-key-abcdef123456")
            self.assertEqual(store.load(), "sk-rotated-key-abcdef123456")
            self.assertNotIn(API_KEY, vault.read_bytes().decode("utf-8"))
        finally:
            for path in (config_path, secret_path, vault):
                path.unlink(missing_ok=True)


class FirstRunExperienceTest(ServerTestCase):
    """What a brand-new user sees before any configuration exists."""

    def test_nothing_is_written_before_the_first_save(self):
        self.assertFalse(self.config_path.exists())
        self.assertFalse(self.secret_path.exists())
        self.assertFalse(self.history_path.exists())
        status, text, _headers = self.exchange("/api/config")
        self.assertEqual(status, 200)
        summary = json.loads(text)
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["api_key_saved"])
        self.assertEqual(summary["api_key_hint"], "")
        self.assertFalse(self.config_path.exists())

    def test_unconfigured_endpoints_point_at_the_settings_dialog(self):
        status, text, _headers = self.exchange("/api/models")
        self.assertEqual(status, 409)
        self.assertIn("连接设置", text)
        status, text, _headers = self.exchange(
            "/api/chat", "POST", payload={"messages": [{"role": "user", "content": "hi"}], "model": "any"}
        )
        self.assertEqual(status, 409)
        self.assertIn("连接设置", text)

    def test_guidance_never_invents_a_top_right_button(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("右上角", source)
        self.assertIn("连接设置", source)

    def test_saving_the_first_configuration_creates_the_runtime_files(self):
        status, _text, _headers = self.exchange(
            "/api/config",
            "POST",
            payload={"api_key": API_KEY, "base_url": "http://127.0.0.1:9099/v1", "models": ["model-a"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.config_path.exists())
        self.assertTrue(self.secret_path.exists())
        self.assertNotIn(API_KEY, self.secret_path.read_text(encoding="utf-8"))
        self.assertNotIn(API_KEY, self.config_path.read_text(encoding="utf-8"))


class StaticAssetTest(ServerTestCase):
    def test_every_cached_asset_is_served(self):
        listed = re.findall(r'"(/[^"]*)"', server.SERVICE_WORKER_JS.split("ASSETS=[")[1].split("]")[0])
        self.assertTrue(listed)
        for asset in listed:
            status, _text, _headers = self.exchange(asset)
            self.assertEqual(status, 200, asset)

    def test_offline_shell_version_forces_a_fresh_app_script(self):
        match = re.search(r'CACHE="chat-app-v(\d+)"', server.SERVICE_WORKER_JS)
        self.assertIsNotNone(match, "the cached shell must be versioned")
        self.assertGreaterEqual(int(match.group(1)), 11, "bump CACHE whenever the shell assets change")

    def test_static_map_matches_the_files_on_disk(self):
        for filename, _content_type in server.STATIC_FILES.values():
            self.assertTrue((ROOT / "static" / filename).is_file(), filename)

    def test_frontend_loads_no_third_party_resources(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        external = [
            target
            for target in re.findall(r'(?:src|href)="([^"]+)"', html)
            if target.startswith(("http://", "https://", "//"))
        ]
        self.assertEqual(external, [])


class ReleaseHygieneTest(unittest.TestCase):
    def test_runtime_files_are_gitignored(self):
        rules = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        for name in RUNTIME_FILES:
            self.assertIn(name, rules)
        for pattern in ("__pycache__/", "*.pyc", "tests/.history-*.json", "tests/.relay-history-*.json", ".env"):
            self.assertIn(pattern, rules)

    def test_example_configuration_holds_only_placeholders(self):
        raw = (ROOT / ".env.example").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        self.assertIn("sk-your-key", text)
        self.assertIn("choose-a-strong-password", text)
        self.assertNotIn("C:\\Users", text)
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            value = line.split("=", 1)[1]
            self.assertNotRegex(value, r"sk-[A-Za-z0-9]{24,}")

    def test_release_files_are_present(self):
        for name in ("LICENSE", ".gitattributes", ".env.example", "requirements.txt", "README.md"):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text(encoding="utf-8"))

    def test_shipped_text_has_no_developer_machine_paths(self):
        markers = [str(Path.home()), os.environ.get("USERPROFILE", ""), os.environ.get("USERNAME", "")]
        markers = [marker for marker in markers if len(marker) > 3]
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.name in {"test_release_readiness.py"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for marker in markers:
                self.assertNotIn(marker, text, str(path.relative_to(ROOT)))

    def test_text_files_use_one_line_ending(self):
        shipped = [ROOT / name for name in ("server.py", "chat.py", "README.md", "requirements.txt", "LICENSE", ".gitattributes", ".env.example", ".gitignore")]
        shipped += [ROOT / "static" / name for name in ("index.html", "app.js", "style.css")]
        shipped += sorted((ROOT / "tests").glob("*.py"))
        for path in shipped:
            raw = path.read_bytes()
            self.assertNotIn(b"\r\n", raw, str(path.relative_to(ROOT)))
            self.assertTrue(raw.endswith(b"\n"), str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
