import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import server


class ConversationHistoryTest(unittest.TestCase):
    def setUp(self):
        self.history_path = Path(__file__).with_name(f".history-{uuid4().hex}.json")
        self.store = server.ConversationStore(self.history_path)

    def tearDown(self):
        self.history_path.unlink(missing_ok=True)
        for temp_path in self.history_path.parent.glob(f".{self.history_path.name}.*.tmp"):
            temp_path.unlink(missing_ok=True)

    def test_create_continue_and_restart(self):
        self.store.save_messages(
            "conversation-1",
            [
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
            ],
            "model-a",
        )
        created = self.store.get_conversation("conversation-1")

        self.store.save_messages(
            "conversation-1",
            [
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Follow-up"},
                {"role": "assistant", "content": "Second answer"},
            ],
            "model-b",
        )

        reopened = server.ConversationStore(self.history_path)
        conversation = reopened.get_conversation("conversation-1")
        self.assertEqual(conversation["created_at"], created["created_at"])
        self.assertEqual(conversation["title"], "First question")
        self.assertEqual(conversation["model"], "model-b")
        self.assertEqual(len(conversation["messages"]), 4)
        self.assertEqual(reopened.list_conversations()[0]["message_count"], 4)

    def test_list_is_sorted_by_latest_update(self):
        with patch("server.time.time", side_effect=[100.0, 200.0, 300.0]):
            self.store.save_messages("older", [{"role": "user", "content": "Old"}], "model-a")
            self.store.save_messages("newer", [{"role": "user", "content": "New"}], "model-a")
            self.store.save_messages("older", [{"role": "user", "content": "Old updated"}], "model-b")
        self.assertEqual([item["id"] for item in self.store.list_conversations()], ["older", "newer"])

    def test_concurrent_writes_remain_valid(self):
        errors = []

        def save(index):
            try:
                self.store.save_messages(
                    f"conversation-{index}",
                    [{"role": "user", "content": f"Question {index}"}],
                    f"model-{index}",
                )
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=save, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        loaded = server.ConversationStore(self.history_path)
        self.assertEqual(len(loaded.list_conversations()), 20)
        json.loads(self.history_path.read_text(encoding="utf-8"))

    def test_malformed_history_is_safely_filtered(self):
        self.history_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "conversations": {
                        "valid": {
                            "title": "Valid",
                            "created_at": 1,
                            "updated_at": 2,
                            "messages": [
                                {"role": "user", "content": "Hello"},
                                {"role": "system", "content": "discard me"},
                                "invalid",
                            ],
                        },
                        "bad": {"title": "Missing fields"},
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = server.ConversationStore(self.history_path)
        self.assertEqual([item["id"] for item in loaded.list_conversations()], ["valid"])
        self.assertEqual(loaded.get_conversation("valid")["messages"], [{"role": "user", "content": "Hello"}])

    def test_invalid_json_starts_with_empty_history(self):
        self.history_path.write_text("{broken", encoding="utf-8")
        loaded = server.ConversationStore(self.history_path)
        self.assertEqual(loaded.list_conversations(), [])

    def test_failed_stream_keeps_user_message(self):
        chat_server = server.ChatServer(app_password="")
        chat_server.conversations = self.store

        def fail_stream(messages, model):
            raise RuntimeError("relay unavailable")
            yield

        chat_server.stream_chat = fail_stream
        stream = chat_server.chat_with_memory(
            None,
            [{"role": "user", "content": "Please remember this"}],
            "model-a",
        )
        first_event = next(stream)
        with self.assertRaises(RuntimeError):
            list(stream)

        conversation = self.store.get_conversation(first_event["conversation_id"])
        self.assertEqual(conversation["messages"], [{"role": "user", "content": "Please remember this"}])
        self.assertEqual(conversation["model"], "model-a")

    def test_partial_stream_saves_partial_reply(self):
        chat_server = server.ChatServer(app_password="")
        chat_server.conversations = self.store

        def partial_stream(messages, model):
            yield {"content": "Partial answer"}
            raise RuntimeError("connection dropped")

        chat_server.stream_chat = partial_stream
        stream = chat_server.chat_with_memory(
            "partial",
            [{"role": "user", "content": "Question"}],
            "model-a",
        )
        self.assertEqual(next(stream), {"conversation_id": "partial"})
        self.assertEqual(next(stream), {"content": "Partial answer"})
        with self.assertRaises(RuntimeError):
            next(stream)

        conversation = self.store.get_conversation("partial")
        self.assertEqual(conversation["messages"][-1], {"role": "assistant", "content": "Partial answer"})


if __name__ == "__main__":
    unittest.main()
