import io
import json
import re
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


DICTIONARY_TEST_TIMEOUT_SECONDS = 15
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DICTIONARY = PROJECT_ROOT / "data" / "dict-en-zh.json"
SENTINEL_API_KEY = "sk-dictionary-sentinel-key"
DICTIONARY_FORBIDDEN_FRAGMENTS = (
    SENTINEL_API_KEY,
    "app-secret",
    "app-config",
    "chat-history",
    "server.py",
    ".py",
    "dict-en-zh",
    str(Path.home()),
)


class DictionaryEndpointTest(unittest.TestCase):
    """POST /api/dictionary served by a real app server on throwaway files."""

    def setUp(self):
        self.config_path = Path(__file__).with_name(f".config-{uuid4().hex}.json")
        self.history_path = Path(__file__).with_name(f".history-{uuid4().hex}.json")
        chat_server = server.ChatServer(app_password="", config_path=self.config_path)
        chat_server.conversations = server.ConversationStore(self.history_path)
        chat_server.api_key = SENTINEL_API_KEY
        server.Handler.chat_server = chat_server
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = f"http://127.0.0.1:{self.app.server_address[1]}"

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join(timeout=5)
        secret_path = server.Handler.chat_server.secret_store.path
        for path in (self.config_path, self.history_path, secret_path):
            path.unlink(missing_ok=True)

    def post(self, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = Request(self.base_url + "/api/dictionary", data=body, method="POST", headers=request_headers)
        with urlopen(request, timeout=DICTIONARY_TEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
        return response.status, raw

    def lookup(self, word):
        status, raw = self.post({"word": word})
        self.assertEqual(status, 200)
        return json.loads(raw), raw

    def reject(self, payload, expected_status, headers=None):
        try:
            status, raw = self.post(payload, headers)
        except HTTPError as error:
            body = error.read().decode("utf-8")
            error.close()
            self.assertEqual(error.code, expected_status, body)
            return body
        self.fail(f"expected HTTP {expected_status}, got {status}: {raw[:120]}")

    def test_known_word_returns_gloss(self):
        data, raw = self.lookup("language")
        self.assertEqual(data["word"], "language")
        self.assertEqual(data["query"], "language")
        self.assertEqual(data["source"], "local")
        self.assertTrue(data["results"], raw)
        for sense in data["results"]:
            self.assertTrue(sense["text"].strip())
            self.assertFalse(any(fragment in raw for fragment in DICTIONARY_FORBIDDEN_FRAGMENTS), raw[:200])

    def test_response_never_leaks_secrets_or_paths(self):
        for word in ("language", "children", "qqzzxw"):
            _, raw = self.lookup(word)
            for fragment in DICTIONARY_FORBIDDEN_FRAGMENTS:
                if fragment:
                    self.assertNotIn(fragment, raw, word)

    def test_inflected_word_falls_back_to_base_form(self):
        data, _ = self.lookup("Children")
        self.assertEqual(data["query"], "child")
        self.assertTrue(data["results"])

    def test_unknown_word_is_not_an_error(self):
        data, raw = self.lookup("qqzzxw")
        self.assertEqual(data["results"], [])
        self.assertTrue(data["hint"], raw)

    def test_non_word_input_is_rejected(self):
        bad_payloads = [
            {"word": ""},
            {"word": "   "},
            {"word": "hello world"},
            {"word": "123"},
            {"word": "-abc"},
            {"word": "word\ttab"},
            {"word": "\u5355\u8bcd"},
            {"word": "a" * 30},
            {"word": None},
            {},
            ["language"],
            b"not json",
        ]
        for payload in bad_payloads:
            body = self.reject(payload, 400)
            self.assertIn("error", body)

    def test_cross_site_post_is_rejected(self):
        self.reject({"word": "language"}, 403, headers={"Origin": "http://dictionary.example"})

    def test_dictionary_route_is_rate_limited(self):
        chat_server = server.Handler.chat_server
        limit, _window = server.RATE_LIMIT_WINDOWS["dictionary"]
        key = chat_server.rate_limit_key("", "127.0.0.1")
        for _ in range(limit):
            self.assertTrue(chat_server.rate_limiter.check("dictionary", key)[0])
        try:
            self.post({"word": "language"})
            self.fail("expected the dictionary route to be rate limited")
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After")
            error.close()
            self.assertEqual(error.code, 429)
            self.assertTrue(retry_after)

    def test_oversized_body_is_not_served(self):
        header = (
            "POST /api/dictionary HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n\r\n" % (server.MAX_REQUEST_BODY_BYTES + 1)
        )
        with socket.create_connection(("127.0.0.1", self.app.server_address[1]), timeout=DICTIONARY_TEST_TIMEOUT_SECONDS) as sock:
            sock.sendall(header.encode("ascii"))
            response = sock.recv(4096)
            sock.settimeout(2)
            try:
                while sock.recv(4096):
                    pass
            except OSError:
                pass
        self.assertTrue(response.startswith(b"HTTP/1.1 400"), response[:80])


class DictionaryStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).with_name(f".dict-{uuid4().hex}")
        self.directory.mkdir()
        self.words_path = self.directory / "dict-en-zh.json"

    def tearDown(self):
        for path in self.directory.glob("*"):
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    def write_payload(self, payload):
        self.words_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_phonetics_and_inflections_come_from_the_file(self):
        self.write_payload(
            {
                "words": {"child": ["n. \u5b69\u5b50"], "run": ["v. \u8dd1\nn. \u5954\u8dd1"]},
                "phonetics": {"child": "tʃaɪld"},
                "inflections": {"children": "child", "running": "run", "bogus": "missing"},
            }
        )
        store = server.DictionaryStore(self.words_path)
        data = store.lookup("Children")
        self.assertEqual(data["query"], "child")
        self.assertEqual(data["phonetic"], "tʃaɪld")
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(store.lookup("running")["query"], "run")
        self.assertEqual(store.lookup("children")["results"], data["results"])

    def test_literal_newlines_split_senses(self):
        self.write_payload({"words": {"go": ["vi. \u53bb\\nvt. \u6253\u5f00\\nn. \u5c1d\u8bd5"]}, "phonetics": {}, "inflections": {}})
        data = server.DictionaryStore(self.words_path).lookup("go")
        self.assertEqual(len(data["results"]), 3)
        self.assertEqual(data["results"][1]["pos"], "vt.")

    def test_missing_or_broken_file_degrades_to_hint(self):
        store = server.DictionaryStore(self.directory / "absent.json")
        self.assertEqual(store.lookup("language")["results"], [])
        self.assertTrue(store.lookup("language")["hint"])
        self.words_path.write_text("{not json", encoding="utf-8")
        store = server.DictionaryStore(self.words_path)
        self.assertEqual(store.lookup("language")["results"], [])

    def test_invalid_entries_are_skipped(self):
        self.write_payload(
            {
                "words": {
                    "ok": ["adj. \u597d"],
                    "bad list": ["n. \u4e0d\u4f1a\u88ab\u8bfb\u5230"],
                    "empty": [],
                    "number": 5,
                    "nonsense": ["   "],
                },
                "phonetics": [],
                "inflections": "no",
            }
        )
        store = server.DictionaryStore(self.words_path)
        self.assertEqual(sorted(store.load_core()), ["ok"])
        self.assertEqual(store.load_inflections(), {})
        self.assertEqual(store.lookup("ok")["results"][0]["text"], "\u597d")


class DictionaryBuilderTest(unittest.TestCase):
    """tools/build_dictionary.py turns a full ECDICT CSV into the committed word list."""

    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        import build_dictionary

        self.builder = build_dictionary
        self.directory = Path(__file__).with_name(f".dict-{uuid4().hex}")
        self.directory.mkdir()

    def tearDown(self):
        for path in self.directory.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        for path in self.directory.glob("*"):
            path.rmdir()
        self.directory.rmdir()

    def test_translation_cleanup(self):
        senses = self.builder.clean_senses(
            "n. \u8bed\u8a00, \u6587\u5b57\n[\u8ba1] \u8bed\u8a00\u7f16\u7a0b\nvt. \u8868\u8fbe, \u4f20\u8fbe\nn. \u6c11\u65cf"
        )
        self.assertEqual(senses, ["n. \u8bed\u8a00, \u6587\u5b57", "vt. \u8868\u8fbe, \u4f20\u8fbe", "n. \u6c11\u65cf"])

    def test_inflection_codes_are_read(self):
        forms = self.builder.inflected_forms("p:went/d:gone/i:going/3:goes/s:goses")
        self.assertEqual(forms, ["went", "gone", "going", "goes", "goses"])
        self.assertEqual(self.builder.inflected_forms("x:weird/y:odd"), [])

    def test_csv_is_compiled_into_a_runtime_payload(self):
        source = self.directory / "mini.csv"
        source.write_text(
            ",".join(
                [
                    "word",
                    "phonetic",
                    "definition",
                    "translation",
                    "pos",
                    "collins",
                    "oxford",
                    "tag",
                    "bnc",
                    "frq",
                    "exchange",
                    "detail",
                    "audio",
                ]
            )
            + "\n"
            + "\n".join(
                [
                    '"go","gou","to walk","vi. \u53bb, \u6267\u884c\\n[\u8ba1] \u8df3\u8f6c","","5","1","","44","35","p:went/i:going/3:goes","",""',
                    '"many words here","x","","n. \u8bcd\u7ec4","","","","","","","",""',
                    '"water","","","n. \u6c34, \u6c34\u91cf\n[\u5316] \u6c27\u5316\u7269","","","","","80","60","s:waters","",""',
                    '"qqzzxw","","","n. \u6ca1\u6709\u9891\u7387","","","","","","","",""',
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        rows = self.builder.load_rows(source)
        self.assertEqual(sorted(row["word"] for row in rows), ["go", "water"])
        payload = self.builder.build(rows, 0)
        self.assertEqual(payload["words"]["go"], ["vi. \u53bb, \u6267\u884c"])
        self.assertEqual(payload["phonetics"]["go"], "gou")
        self.assertEqual(payload["inflections"]["went"], "go")
        self.assertNotIn("many words here", payload["words"])
        self.assertNotIn("qqzzxw", payload["words"])
        target = self.directory / "out.json"
        self.builder.dump(payload, target)
        reloaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["words"], payload["words"])
        self.assertIn("ECDICT", reloaded["source"])
        store = server.DictionaryStore(target)
        self.assertEqual(store.lookup("went")["query"], "go")
        self.assertEqual(store.lookup("WATER")["query"], "water")


class FrontendDictionaryTest(unittest.TestCase):
    """The click-a-word card is built without innerHTML, so keep its class names honest."""

    @classmethod
    def setUpClass(cls):
        cls.script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.markup = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def produced_class_names(self):
        names = set()
        for groups in re.findall('className\\s*=\\s*(?:"([^"]*)"|`([^`]*)`)', self.script):
            for value in groups:
                names.update(token for token in value.split() if token.startswith("dictionary-"))
        for value in re.findall(r'class="([^"]*)"', self.markup):
            names.update(token for token in value.split() if token.startswith("dictionary-"))
        return names

    def test_every_queried_card_class_is_created(self):
        queried = set(re.findall(r'querySelector\(\s*"\.(dictionary-[a-z-]+)"', self.script))
        self.assertTrue(queried, "expected the dictionary card to query its own slots")
        missing = queried - self.produced_class_names()
        self.assertFalse(missing, f"card slots queried but never created: {sorted(missing)}")

    def test_loading_placeholder_is_the_phonetic_slot(self):
        self.search_assert(
            r'className="[^"]*dictionary-phonetic[^"]*dictionary-loading[^"]*"',
            "the 查询中 placeholder must be the element the renderer replaces",
        )

    def test_card_uses_text_content_only(self):
        block = self.script[self.script.index("function renderDictionaryCard") :]
        block = block[: block.index("function showDictionaryCard")]
        self.assertNotIn("innerHTML", block)
        self.assertIn("textContent", block)

    def test_lookup_is_wired_to_the_backend_route(self):
        self.assertIn('"/api/dictionary"', self.script)
        self.assertIn('id="dictionary-enabled"', self.markup)
        self.search_assert(r'localStorage\.(getItem|setItem)\("dictionary-enabled"', "the switch must be stored locally")

    def search_assert(self, pattern, message):
        self.assertTrue(re.search(pattern, self.script), message)


class BundledDictionaryTest(unittest.TestCase):
    def test_bundled_word_list_is_real_ecdict_data(self):
        payload = json.loads(BUNDLED_DICTIONARY.read_text(encoding="utf-8"))
        self.assertIn("ECDICT", payload["source"])
        words = payload["words"]
        self.assertGreaterEqual(len(words), 1000)
        pattern = server.DICTIONARY_INDEX_WORD_PATTERN
        for word, senses in words.items():
            self.assertTrue(pattern.match(word), word)
            self.assertIsInstance(senses, list)
            self.assertTrue(senses)
            for sense in senses:
                self.assertIsInstance(sense, str)
                self.assertTrue(sense.strip())
        for word in ("language", "water", "child", "go", "the"):
            self.assertIn(word, words)

    def test_bundled_inflections_point_at_words(self):
        payload = json.loads(BUNDLED_DICTIONARY.read_text(encoding="utf-8"))
        inflections = payload["inflections"]
        self.assertGreaterEqual(len(inflections), 100)
        for form, base in inflections.items():
            self.assertTrue(server.DICTIONARY_INDEX_WORD_PATTERN.match(form), form)
            self.assertTrue(server.DICTIONARY_INDEX_WORD_PATTERN.match(base), base)

    def test_bundled_dictionary_size_is_bounded(self):
        self.assertLess(BUNDLED_DICTIONARY.stat().st_size, 8 * 1024 * 1024)
