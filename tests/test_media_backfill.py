"""Pictures that were unreadable *when they arrived* must become readable later.

Two failure modes, both measured on a live run:

* A message whose media could not be described was stored as a bare
  ``[图片]`` / ``[表情]`` and stayed that way for good — every later prompt
  read the placeholder and the model answered "我看不到图".  :class:`MediaBackfill`
  re-describes those rows just before they are put into a prompt.
* The vision request shape was wrong for a reasoning model: the budget was
  spent on reasoning tokens, ``finish_reason`` came back ``length`` and
  ``content`` empty (19 of 22 cached images).  :class:`VisionClient` now
  negotiates the request shape and remembers what worked.

Everything here runs offline: the endpoint is a mock, the pipeline and the
raw-message reader are fakes.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Vision request negotiation ───────────────────────────────────────


def _completion(content: str, finish_reason: str = "stop"):
    """Shape of one OpenAI-compatible chat.completion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    return response


def _client(**kwargs):
    from src.media.vision import VisionClient

    params = {"api_key": "sk-test", "model": "vision-model"}
    params.update(kwargs)
    client = VisionClient(**params)
    client._client = MagicMock()
    return client


class VisionNegotiationTests(unittest.TestCase):
    """The request shape is negotiated, then reused."""

    def test_truncated_reply_escalates_and_still_describes(self):
        # First reply has no text because reasoning ate the budget; the
        # second attempt (reasoning off) must be made and used.
        client = _client()
        client._client.chat.completions.create.side_effect = [
            _completion("", finish_reason="length"),
            _completion("一只猫趴在键盘上"),
        ]
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertEqual(
                client.describe(Path("cat.png")), "一只猫趴在键盘上"
            )
        self.assertEqual(client._client.chat.completions.create.call_count, 2)

        # Reasoning is now known to be the problem: the next image starts
        # with the shape that worked, so it costs a single request.
        client._client.chat.completions.create.reset_mock()
        client._client.chat.completions.create.side_effect = None
        client._client.chat.completions.create.return_value = _completion("一只狗")
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertEqual(client.describe(Path("dog.png")), "一只狗")
        self.assertEqual(client._client.chat.completions.create.call_count, 1)
        self.assertEqual(
            client._client.chat.completions.create.call_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_endpoint_rejecting_thinking_is_handled_for_the_run(self):
        """Plain OpenAI rejects the field; it must not break every image."""
        client = _client(disable_thinking=True)
        client._client.chat.completions.create.side_effect = [
            Exception("Unknown parameter: 'thinking'"),
            _completion("一只猫"),
        ]
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertEqual(client.describe(Path("cat.png")), "一只猫")
        self.assertFalse(client._thinking_supported)

        # The rejected field is dropped for good: one request, no thinking.
        client._client.chat.completions.create.reset_mock()
        client._client.chat.completions.create.side_effect = None
        client._client.chat.completions.create.return_value = _completion("一只狗")
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertEqual(client.describe(Path("dog.png")), "一只狗")
        self.assertEqual(client._client.chat.completions.create.call_count, 1)
        self.assertIsNone(
            client._client.chat.completions.create.call_args.kwargs["extra_body"]
        )

    def test_real_failure_is_not_retried(self):
        """An auth/network error will not be fixed by another budget."""
        client = _client()
        client._client.chat.completions.create.side_effect = Exception(
            "Invalid API key"
        )
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertIsNone(client.describe(Path("cat.png")))
        self.assertEqual(client._client.chat.completions.create.call_count, 1)
        self.assertIn("Invalid API key", client.last_error)

    def test_empty_content_without_truncation_explains_itself(self):
        client = _client()
        client._client.chat.completions.create.return_value = _completion(
            "", finish_reason="stop"
        )
        with patch("src.media.vision.to_data_url", return_value="data:image/png;base64,AA"):
            self.assertIsNone(client.describe(Path("cat.png")))
        self.assertIn("finish_reason=stop", client.last_error)

    def test_no_credentials_is_reported_not_called(self):
        client = _client(api_key="")
        self.assertIsNone(client.describe(Path("cat.png")))
        self.assertIn("no api key", client.last_error)
        self.assertFalse(client._client.chat.completions.create.called)

    def test_unreadable_file_is_reported(self):
        client = _client()
        with patch("src.media.vision.to_data_url", return_value=None):
            self.assertIsNone(client.describe(Path("notes.txt")))
        self.assertIn("not a readable image", client.last_error)


# ── Backfill of old media messages ───────────────────────────────────


class _FakeStore:
    """Just enough of MessageStore to observe the write-back.

    The real row-count semantics are covered by
    :class:`MessageContentUpdateTests`; here the point is whether the
    backfill asks for a write at all, and with what content.
    """

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.updated: list[tuple[str, str]] = []

    def update_message_content(self, message_id: str, content: str) -> bool:
        if not message_id or not content:
            return False
        self.rows[message_id] = content
        self.updated.append((message_id, content))
        return True


class _FakePipeline:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    def process(self, msg, chat_id=""):
        self.calls += 1
        if callable(self._result):
            return self._result(msg)
        return self._result


def _raw(server_id: str, local_type: int, xml: str) -> dict:
    return {
        "server_id": server_id,
        "localType": local_type,
        "message_content": xml,
        "content_kind": "text",
    }


def _msg_id(raw: dict) -> str:
    from src.wechat.wcdb_backend import raw_message_id

    return raw_message_id(raw)


def _row(raw: dict, content: str, msg_type: int = 3,
         sender_id: str = "wxid_a") -> dict:
    """A stored message as ``get_messages_since`` returns it."""
    return {
        "message_id": _msg_id(raw),
        "chat_id": "c1@chatroom",
        "sender_id": sender_id,
        "sender_name": "甲",
        "content": content,
        "msg_type": msg_type,
        "timestamp": 100,
    }


class MediaBackfillTests(unittest.TestCase):
    """Only bare placeholders are revisited, and only a few per prompt."""

    def _backfill(self, pipeline, raw_messages):
        from src.media.backfill import MediaBackfill

        store = _FakeStore()
        return MediaBackfill(
            store=store,
            pipeline_provider=lambda: pipeline,
            raw_message_fetcher=lambda chat_id, limit: raw_messages,
        ), store

    def test_bare_placeholder_is_described_and_persisted(self):
        raw = _raw("s1", 3, '<msg><img md5="' + "a" * 32 + '"/></msg>')
        message = _row(raw, "[图片]")
        pipeline = _FakePipeline("[图片] 一只猫在键盘上")
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 1)
        # The prompt that triggered the backfill sees the description...
        self.assertEqual(message["content"], "[图片] 一只猫在键盘上")
        # ...and it is persisted for every later prompt.
        self.assertEqual(store.rows[message["message_id"]], "[图片] 一只猫在键盘上")

    def test_emoji_placeholder_is_described_too(self):
        raw = _raw("s2", 47, '<msg><emoji md5="' + "b" * 32 + '"/></msg>')
        message = _row(raw, "[表情]", msg_type=47)
        pipeline = _FakePipeline("[表情] 一只猫在摇头")
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 1)
        self.assertEqual(store.rows[message["message_id"]], "[表情] 一只猫在摇头")

    def test_described_message_is_never_revisited(self):
        raw = _raw("s3", 3, '<msg><img md5="' + "c" * 32 + '"/></msg>')
        message = _row(raw, "[图片] 一只猫在键盘上")
        pipeline = _FakePipeline("[图片] 不该被调用")
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(pipeline.calls, 0)
        self.assertEqual(store.updated, [])

    def test_unreadable_media_is_not_retried_every_message(self):
        """One broken sticker must not cost a vision call on every @mention."""
        raw = _raw("s4", 47, '<msg><emoji/></msg>')
        message = _row(raw, "[表情]", msg_type=47)
        pipeline = _FakePipeline(None)
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(pipeline.calls, 1)
        # Second prompt: remembered as failed, so no second attempt.
        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(pipeline.calls, 1)
        self.assertEqual(message["content"], "[表情]")
        self.assertEqual(backfill.stats["given_up"], 1)

    def test_pipeline_returning_the_placeholder_does_not_erase_the_row(self):
        raw = _raw("s5", 3, '<msg><img md5="' + "d" * 32 + '"/></msg>')
        message = _row(raw, "[图片]")
        pipeline = _FakePipeline("[图片]")
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(store.updated, [])

    def test_message_outside_the_raw_page_is_retried_later(self):
        """Too old to fetch now ≠ permanently unreadable."""
        raw = _raw("s6", 3, '<msg><img md5="' + "e" * 32 + '"/></msg>')
        message = _row(raw, "[图片]")
        pipeline = _FakePipeline("[图片] 一只猫")
        backfill, store = self._backfill(pipeline, [])  # page has nothing

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(backfill.stats["given_up"], 0)
        # The page catches up; the same message is now describable.
        backfill._fetch_raw = lambda chat_id, limit: [raw]
        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 1)

    def test_non_media_types_are_skipped(self):
        raw = _raw("s7", 49, "<msg><appmsg/></msg>")
        link = _row(raw, "[链接]", msg_type=49)
        pipeline = _FakePipeline("[链接] 不该被调用")
        backfill, _ = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [link]), 0)
        self.assertEqual(pipeline.calls, 0)

    def test_at_most_three_per_prompt(self):
        raws = [
            _raw(f"s{i}", 3, '<msg><img md5="' + str(i) * 32 + '"/></msg>')
            for i in range(5)
        ]
        messages = [_row(r, "[图片]") for r in raws]
        pipeline = _FakePipeline("[图片] 一张图")
        backfill, _ = self._backfill(pipeline, raws)

        self.assertEqual(backfill.refresh("c1@chatroom", messages), 3)
        self.assertEqual(pipeline.calls, 3)

    def test_disabled_pipeline_is_a_no_op(self):
        raw = _raw("s8", 3, '<msg><img/></msg>')
        message = _row(raw, "[图片]")
        backfill, store = self._backfill(None, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(store.updated, [])

    def test_pipeline_crash_does_not_break_the_reply(self):
        raw = _raw("s9", 3, '<msg><img md5="' + "f" * 32 + '"/></msg>')
        message = _row(raw, "[图片]")

        def boom(msg):
            raise RuntimeError("decrypt failed")

        pipeline = _FakePipeline(boom)
        backfill, store = self._backfill(pipeline, [raw])

        self.assertEqual(backfill.refresh("c1@chatroom", [message]), 0)
        self.assertEqual(message["content"], "[图片]")
        self.assertEqual(store.updated, [])


class MessageContentUpdateTests(unittest.TestCase):
    """The write-back path itself."""

    def _store(self):
        from src.db import MessageStore, initialize_db

        return MessageStore(initialize_db(":memory:"))

    def test_update_replaces_content(self):
        store = self._store()
        store.insert_message({
            "message_id": "m1",
            "chat_id": "c1@chatroom",
            "sender_id": "wxid_a",
            "sender_name": "甲",
            "content": "[图片]",
            "msg_type": 3,
            "timestamp": 100,
        })
        self.assertTrue(store.update_message_content("m1", "[图片] 一只猫"))

        rows = store.get_messages_since("c1@chatroom", 0)
        self.assertEqual(rows[0]["content"], "[图片] 一只猫")

    def test_missing_row_reports_failure(self):
        self.assertFalse(self._store().update_message_content("nope", "x"))

    def test_empty_values_are_refused(self):
        store = self._store()
        self.assertFalse(store.update_message_content("", "x"))
        self.assertFalse(store.update_message_content("m1", ""))


class EmojiCacheTests(unittest.TestCase):
    """A cache entry that is not an image must be refetched, not served."""

    def test_non_image_cache_entry_is_dropped(self):
        from src.media.emoji import EmojiStore

        md5 = "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            poisoned = cache / md5
            poisoned.write_bytes(b"<html>404 not found</html>")

            store = EmojiStore("", cache)
            # No local Persist copy and no cdnurl in the XML: the only honest
            # answer is None — and the bad file must be gone, so the next
            # attempt can download the real image.
            self.assertIsNone(store.fetch(f'<msg><emoji md5="{md5}"/></msg>'))
            self.assertFalse(poisoned.exists())

    def test_valid_cache_entry_is_served_without_downloading(self):
        from src.media.emoji import EmojiStore

        md5 = "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            good = cache / md5
            good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

            store = EmojiStore("", cache)
            with patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(
                    store.fetch(f'<msg><emoji md5="{md5}" cdnurl="http://x/y"/></msg>'),
                    good,
                )
            self.assertFalse(urlopen.called)


if __name__ == "__main__":
    unittest.main()
