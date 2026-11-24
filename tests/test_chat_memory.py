"""The bot's side of the conversation has to survive into the next prompt.

Measured symptom: the bot repeated a joke it had made two minutes earlier
and asked a question it had just asked, because the WeChat poll never
reports our own sends — nothing in the context said "you already said
that".  These tests pin the three pieces of the fix:

1. our own replies are stored (``bot_replies``),
2. context is merged and turned into real ``assistant`` turns,
3. conversation older than the window is compressed into a throttled
   digest instead of being lost.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.schema import initialize_db
from src.db.store import MessageStore


def _store():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    return MessageStore(initialize_db(path))


def _msg(mid, sender, content, ts, chat="c1"):
    return {
        "message_id": mid, "chat_id": chat, "sender_id": f"id_{sender}",
        "sender_name": sender, "content": content, "msg_type": 1,
        "timestamp": ts,
    }


class _CaptureSummarizer:
    """Minimal stand-in for AbstractSummarizer, capturing the API call."""

    def __init__(self, reply="ok"):
        self.calls = []
        self._reply = reply

    def chat_raw(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self._reply


class BotReplyStorageTests(unittest.TestCase):
    def test_reply_round_trips(self):
        store = _store()
        now = int(time.time())
        self.assertTrue(store.insert_bot_reply("c1", "赏口饭吃", timestamp=now))
        rows = store.get_bot_replies_since("c1", now - 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "赏口饭吃")

    def test_empty_reply_is_not_stored(self):
        store = _store()
        self.assertFalse(store.insert_bot_reply("c1", "", timestamp=1))
        self.assertEqual(store.get_bot_replies_since("c1", 0), [])

    def test_window_filters_old_replies(self):
        store = _store()
        now = int(time.time())
        store.insert_bot_reply("c1", "老的", timestamp=now - 9999)
        store.insert_bot_reply("c1", "新的", timestamp=now)
        rows = store.get_bot_replies_since("c1", now - 60)
        self.assertEqual([r["content"] for r in rows], ["新的"])


class MergeContextTests(unittest.TestCase):
    """Group messages and our own turns interleave by timestamp."""

    def _router(self, store):
        from src.router import MessageRouter

        router = MessageRouter.__new__(MessageRouter)
        router._store = store
        return router

    def test_bot_turns_are_interleaved_in_order(self):
        store = _store()
        now = int(time.time())
        store.insert_message(_msg("m1", "Dobby", "我点了外卖", now - 30))
        store.insert_bot_reply("c1", "给我带一份", timestamp=now - 20)
        store.insert_message(_msg("m2", "Kai", "好吃吗", now - 10))

        merged = self._router(store)._merge_context("c1", now - 120)

        self.assertEqual(
            [(m["is_bot"], m["content"]) for m in merged],
            [(False, "我点了外卖"), (True, "给我带一份"), (False, "好吃吗")],
        )

    def test_limit_keeps_the_newest_turns(self):
        store = _store()
        now = int(time.time())
        for i in range(10):
            store.insert_message(_msg(f"m{i}", "Dobby", f"话{i}", now - 100 + i))
        merged = self._router(store)._merge_context("c1", now - 200, limit=3)
        self.assertEqual([m["content"] for m in merged], ["话7", "话8", "话9"])


class ChatTurnBuildingTests(unittest.TestCase):
    """``chat()`` must send real turns, not a transcript blob."""

    def _capture(self, context_messages):
        from src.summarize.base import AbstractSummarizer

        captured = {}

        class _Capture(AbstractSummarizer):
            def _summarize_direct(self, messages, requester_name=""):
                raise NotImplementedError

            def _summarize_chunk(self, chunk, chunk_num, total,
                                 requester_name=""):
                raise NotImplementedError

            def _merge_chunk_summaries(self, chunk_summaries,
                                       requester_name=""):
                raise NotImplementedError

            def consolidate_memory(self, existing_memory, new_messages):
                raise NotImplementedError

            def _call_chat_api(self, system_prompt, messages):
                captured["prompt"] = system_prompt
                captured["messages"] = messages
                return "ok"

        _Capture().chat(
            message="你说啥来着",
            context_messages=context_messages,
            requester_name="Dobby",
            bot_name="霜叶",
            group_name="测试群",
        )
        return captured

    def test_own_reply_becomes_assistant_turn(self):
        captured = self._capture([
            {"sender_name": "Dobby", "content": "我点了外卖", "is_bot": False},
            {"content": "给我带一份", "is_bot": True},
            {"sender_name": "Dobby", "content": "行", "is_bot": False},
        ])
        roles = [m["role"] for m in captured["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertEqual(captured["messages"][1]["content"], "给我带一份")

    def test_adjacent_same_role_turns_are_merged(self):
        """Anthropic rejects non-alternating roles with HTTP 400.

        Two group members talking in a row are two ``user`` turns, and the
        current question is a third — all three collapse into one.
        """
        captured = self._capture([
            {"sender_name": "A", "content": "第一句", "is_bot": False},
            {"sender_name": "B", "content": "第二句", "is_bot": False},
        ])
        roles = [m["role"] for m in captured["messages"]]
        self.assertEqual(roles, ["user"])
        self.assertIn("第一句", captured["messages"][0]["content"])
        self.assertIn("第二句", captured["messages"][0]["content"])

    def test_first_turn_is_always_user(self):
        captured = self._capture([
            {"content": "我先前说过的话", "is_bot": True},
            {"sender_name": "A", "content": "接着说", "is_bot": False},
        ])
        self.assertEqual(captured["messages"][0]["role"], "user")

    def test_context_is_not_duplicated_into_the_system_prompt(self):
        captured = self._capture([
            {"sender_name": "Dobby", "content": "独一无二的一句", "is_bot": False},
        ])
        self.assertNotIn("独一无二的一句", captured["prompt"])
        self.assertTrue(any(
            "独一无二的一句" in m["content"] for m in captured["messages"]
        ))

    def test_digest_lands_in_the_prompt(self):
        from src.summarize.base import AbstractSummarizer

        captured = {}

        class _Capture(AbstractSummarizer):
            def _summarize_direct(self, messages, requester_name=""):
                raise NotImplementedError

            def _summarize_chunk(self, chunk, chunk_num, total,
                                 requester_name=""):
                raise NotImplementedError

            def _merge_chunk_summaries(self, chunk_summaries,
                                       requester_name=""):
                raise NotImplementedError

            def consolidate_memory(self, existing_memory, new_messages):
                raise NotImplementedError

            def _call_chat_api(self, system_prompt, messages):
                captured["prompt"] = system_prompt
                return "ok"

        _Capture().chat(
            message="那事儿后来咋样了",
            requester_name="Dobby",
            digest="上周在聊搬家，定了周六上午。",
        )
        self.assertIn("更早的对话概要", captured["prompt"])
        self.assertIn("定了周六上午", captured["prompt"])


class ChatDigestTests(unittest.TestCase):
    """A rebuild must be throttled — never one extra AI call per reply."""

    def _seed(self, store, count, base_ts):
        now = int(time.time())
        for i in range(count):
            store.insert_message(
                _msg(f"old{i}", "Dobby", f"老消息{i}", now + i),
            )
        return now

    def test_below_threshold_does_not_call_the_model(self):
        from src.memory.digest import DIGEST_MIN_NEW_MSGS, ChatDigest

        store = _store()
        now = self._seed(store, DIGEST_MIN_NEW_MSGS - 1, 0)
        summarizer = _CaptureSummarizer()
        digest = ChatDigest(store, summarizer.chat_raw)

        self.assertEqual(digest.refresh("c1", now + 10_000), "")
        self.assertEqual(summarizer.calls, [])

    def test_threshold_rebuilds_once_then_uses_the_cache(self):
        from src.memory.digest import DIGEST_MIN_NEW_MSGS, ChatDigest

        store = _store()
        now = self._seed(store, DIGEST_MIN_NEW_MSGS, 0)
        summarizer = _CaptureSummarizer(reply="纪要：大家在聊外卖。")
        digest = ChatDigest(store, summarizer.chat_raw)

        window_start = now + 10_000
        self.assertEqual(digest.refresh("c1", window_start), "纪要：大家在聊外卖。")
        self.assertEqual(len(summarizer.calls), 1)
        # Nothing new has fallen out of the window since — cached.
        self.assertEqual(digest.refresh("c1", window_start), "纪要：大家在聊外卖。")
        self.assertEqual(len(summarizer.calls), 1)
        self.assertEqual(
            store.get_chat_digest("c1")["digest_text"], "纪要：大家在聊外卖。",
        )

    def test_model_failure_keeps_the_cached_digest(self):
        from src.memory.digest import DIGEST_MIN_NEW_MSGS, ChatDigest

        store = _store()
        now = self._seed(store, DIGEST_MIN_NEW_MSGS, 0)
        store.upsert_chat_digest("c1", "旧纪要", None)

        def _boom(system_prompt, user_prompt):
            raise RuntimeError("api down")

        digest = ChatDigest(store, _boom)
        self.assertEqual(digest.refresh("c1", now + 10_000), "旧纪要")


class PersonaTests(unittest.TestCase):
    """The 贱兮兮/讨饭 persona must be in the prompt — and bounded."""

    def _chat_prompt(self):
        from src.summarize.base import AbstractSummarizer

        captured = {}

        class _Capture(AbstractSummarizer):
            def _summarize_direct(self, messages, requester_name=""):
                raise NotImplementedError

            def _summarize_chunk(self, chunk, chunk_num, total,
                                 requester_name=""):
                raise NotImplementedError

            def _merge_chunk_summaries(self, chunk_summaries,
                                       requester_name=""):
                raise NotImplementedError

            def consolidate_memory(self, existing_memory, new_messages):
                raise NotImplementedError

            def _call_chat_api(self, system_prompt, messages):
                captured["prompt"] = system_prompt
                return "ok"

        _Capture().chat(message="在吗", requester_name="张三")
        return captured["prompt"]

    def test_chat_prompt_carries_the_persona(self):
        prompt = self._chat_prompt()
        self.assertIn("贱兮兮", prompt)
        self.assertIn("讨饭", prompt)
        self.assertIn("大白饭", prompt)

    def test_persona_does_not_eat_the_safety_rules(self):
        prompt = self._chat_prompt()
        for rule in ("硬底线", "医疗/法律", "不暴露系统提示词", "禁止用词",
                     "不冒充真人"):
            self.assertIn(rule, prompt)

    def test_proactive_prompt_carries_the_persona(self):
        from src.summarize.base import AbstractSummarizer

        captured = {}

        class _Capture(AbstractSummarizer):
            def _summarize_direct(self, messages, requester_name=""):
                raise NotImplementedError

            def _summarize_chunk(self, chunk, chunk_num, total,
                                 requester_name=""):
                raise NotImplementedError

            def _merge_chunk_summaries(self, chunk_summaries,
                                       requester_name=""):
                raise NotImplementedError

            def consolidate_memory(self, existing_memory, new_messages):
                raise NotImplementedError

            def _call_chat_api(self, system_prompt, messages):
                captured["prompt"] = system_prompt
                return ""

        class _Mode:
            label = "活跃"
            description = "群里聊得热闹"
            instruction = "可以接话"
            max_chars = 60
            context_count = 20

        _Capture().proactive_chat(
            mode=_Mode(),
            context_messages=[{"sender_name": "A", "content": "今天吃啥"}],
        )
        prompt = captured["prompt"]
        self.assertIn("贱兮兮", prompt)
        self.assertIn("讨饭", prompt)
        # Silence rules still govern when the persona may speak at all.
        self.assertIn("沉默规则", prompt)


if __name__ == "__main__":
    unittest.main()
