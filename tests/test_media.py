"""Tests for src.media — BLOB decoding, appmsg reduction, placeholder fallback.

The behaviour worth pinning down is what the bot *says* about a media
message: an emoji must never leak base64 into a prompt, an appmsg must
reduce to its title, and a plain-text body stored as a BLOB must survive
the round trip (it used to be dropped entirely).
"""

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard

from src.media.appmsg import looks_like_xml, reduce_appmsg, strip_sender_prefix
from src.media.blob import decode_blob, is_binary
from src.media.vision import detect_mime, to_data_url


def _blob(text: str) -> str:
    """Encode *text* the way the native layer emits a BLOB column."""
    packed = zstandard.ZstdCompressor().compress(text.encode("utf-8"))
    return base64.b64encode(packed).decode("ascii")


class BlobDecodeTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode_blob(_blob("<msg/>")), "<msg/>")

    def test_binary_flag(self):
        self.assertTrue(is_binary({"content_kind": "blob"}))
        self.assertFalse(is_binary({"content_kind": "text"}))
        self.assertFalse(is_binary({}))

    def test_empty_and_garbage_do_not_raise(self):
        self.assertIsNone(decode_blob(""))
        self.assertIsNone(decode_blob("not base64 !!!"))

    def test_uncompressed_base64_falls_back_to_text(self):
        raw = base64.b64encode(b"hello").decode("ascii")
        self.assertEqual(decode_blob(raw), "hello")


class SenderPrefixTests(unittest.TestCase):
    def test_wxid_prefix_removed(self):
        self.assertEqual(
            strip_sender_prefix("wxid_abc123:\nhello"), "hello"
        )

    def test_chatroom_prefix_removed(self):
        self.assertEqual(
            strip_sender_prefix("123@chatroom: hi"), "hi"
        )

    def test_ordinary_text_untouched(self):
        # A chat line that happens to start with digits and a colon must
        # not be mistaken for a sender prefix.
        self.assertEqual(strip_sender_prefix("12:30 开会"), "12:30 开会")


class AppmsgTests(unittest.TestCase):
    def test_link_title_extracted(self):
        xml = (
            '<msg><appmsg><title>总决赛你的队友</title>'
            "<type>5</type></appmsg></msg>"
        )
        self.assertEqual(reduce_appmsg(xml), "[链接] 总决赛你的队友")

    def test_file_label(self):
        xml = "<msg><appmsg><title>a.html</title><type>6</type></appmsg></msg>"
        self.assertEqual(reduce_appmsg(xml), "[文件] a.html")

    def test_cdata_title(self):
        xml = "<msg><appmsg><title><![CDATA[标题]]></title><type>5</type></appmsg></msg>"
        self.assertEqual(reduce_appmsg(xml), "[链接] 标题")

    def test_system_notice_dropped(self):
        self.assertIsNone(reduce_appmsg('<sysmsg type="x"><a/></sysmsg>'))

    def test_unknown_appmsg_without_title_dropped(self):
        self.assertIsNone(reduce_appmsg("<msg><appmsg><type/></appmsg></msg>"))

    def test_markup_detection(self):
        self.assertTrue(looks_like_xml("  <msg/>"))
        self.assertFalse(looks_like_xml("哈哈哈哈"))


class VisionHelperTests(unittest.TestCase):
    def test_detect_mime(self):
        self.assertEqual(detect_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(detect_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(detect_mime(b"GIF89a"), "image/gif")
        self.assertIsNone(detect_mime(b"not an image"))

    def test_to_data_url_rejects_non_image(self):
        with patch.object(Path, "read_bytes", return_value=b"plain text"):
            self.assertIsNone(to_data_url(Path("x")))


class EmojiStoreTests(unittest.TestCase):
    """Local Persist files are only trusted when they really are images."""

    def _store(self, data_dir: Path, cache: Path):
        from src.media.emoji import EmojiStore

        return EmojiStore(str(data_dir), cache)

    def test_non_image_local_file_is_skipped(self):
        # Real data: 5 of 5 Persist payloads began with opaque bytes that
        # are neither zstd nor zlib.  Handing one to vision fails, so the
        # store must skip it rather than return it.
        md5 = "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persist = root / "wxid_x" / "business" / "emoticon" / "Persist"
            (persist / md5[:2]).mkdir(parents=True)
            (persist / md5[:2] / md5).write_bytes(b"\xb7\xdc\xea\x8co0\x01\xe4O")

            store = self._store(root, root / "cache")
            self.assertIsNone(store._find_local(md5))

    def test_image_local_file_is_returned(self):
        md5 = "c" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persist = root / "wxid_x" / "business" / "emoticon" / "Persist"
            (persist / md5[:2]).mkdir(parents=True)
            png = persist / md5[:2] / md5
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

            store = self._store(root, root / "cache")
            self.assertEqual(store._find_local(md5), png)


class MediaPipelineTests(unittest.TestCase):
    """End-to-end behaviour of process(), without touching the network."""

    def _pipeline(self, vision: bool = False):
        from src.media.pipeline import MediaPipeline

        config = MagicMock()
        config.media_vision_enabled = vision
        config.media_vision_model = ""
        config.wechat_data_dir = ""
        config.ai_backend = "deepseek"
        config.deepseek_api_key = ""
        config.deepseek_model = "deepseek-v4-flash"
        config.deepseek_base_url = "https://api.deepseek.com"
        return MediaPipeline(config)

    def test_image_falls_back_to_placeholder(self):
        msg = {
            "localType": 3,
            "content_kind": "blob",
            "message_content": _blob("<msg><img/></msg>"),
        }
        # None → the backend uses "[图片]"; an image container cannot be
        # decrypted, so no vision call is attempted.
        self.assertIsNone(self._pipeline().process(msg))

    def test_emoji_without_vision_returns_none(self):
        msg = {
            "localType": 47,
            "content_kind": "blob",
            "message_content": _blob('<msg><emoji md5="' + "a" * 32 + '"/></msg>'),
        }
        self.assertIsNone(self._pipeline().process(msg))

    def test_appmsg_reduced(self):
        xml = "<msg><appmsg><title>标题</title><type>5</type></appmsg></msg>"
        msg = {
            "localType": 21474836529,
            "content_kind": "blob",
            "message_content": _blob("wxid_x:\n" + xml),
        }
        self.assertEqual(self._pipeline().process(msg), "[链接] 标题")

    def test_plain_text_blob_survives(self):
        msg = {
            "localType": 1,
            "content_kind": "blob",
            "message_content": _blob("wxid_x:\n哈哈哈哈哈哈哈"),
        }
        self.assertEqual(self._pipeline().process(msg), "哈哈哈哈哈哈哈")

    def test_system_notice_dropped(self):
        msg = {
            "localType": 10000,
            "content_kind": "blob",
            "message_content": _blob('<sysmsg type="x"/>'),
        }
        self.assertIsNone(self._pipeline().process(msg))

    def test_vision_absent_when_backend_unsupported(self):
        from src.media.pipeline import MediaPipeline

        config = MagicMock()
        config.media_vision_enabled = True
        config.ai_backend = "claude"
        pipeline = MediaPipeline(config)
        self.assertIsNone(pipeline._vision)


if __name__ == "__main__":
    unittest.main()
