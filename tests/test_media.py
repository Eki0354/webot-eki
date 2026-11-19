"""Tests for src.media — BLOB decoding, appmsg reduction, placeholder fallback.

The behaviour worth pinning down is what the bot *says* about a media
message: an emoji must never leak base64 into a prompt, an appmsg must
reduce to its title, and a plain-text body stored as a BLOB must survive
the round trip (it used to be dropped entirely).
"""

import base64
import sys
import tempfile
import time
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


def _v2(plain: bytes, aes_size: int = 0, xor_size: int = 0) -> bytes:
    """Build a V2 container around *plain*, unencrypted.

    With ``aes_size = 0`` there is no encrypted segment and no key is
    needed, which keeps the container-structure tests key-free.
    """
    header = b"\x07\x08V2\x08\x07"
    header += aes_size.to_bytes(4, "little", signed=True)
    header += xor_size.to_bytes(4, "little", signed=True)
    header += b"\x00"
    return header + plain


class DatContainerTests(unittest.TestCase):
    """The four-segment ``.dat`` layout, measured on real files.

    Only the head is encrypted: a plaintext middle and an XOR-masked tail
    follow.  Treating the whole payload as ciphertext never produced an
    image, which is why the layout is pinned down here.
    """

    def test_version_detection(self):
        from src.media.dat_decrypt import dat_version

        self.assertEqual(dat_version(b"\x07\x08V2\x08\x07" + b"\x00" * 9), 2)
        self.assertEqual(dat_version(b"\x07\x08V1\x08\x07" + b"\x00" * 9), 1)
        self.assertEqual(dat_version(b"\x00" * 15), 0)
        self.assertEqual(dat_version(b"abc"), 0)

    def test_header_layout(self):
        from src.media.dat_decrypt import HEADER_LEN, decrypt_dat

        # aesSize=0, xorSize=0 → the payload is returned verbatim.
        payload = b"\xff\xd8\xff" + b"x" * 100
        container = _v2(payload)
        self.assertEqual(len(container), HEADER_LEN + len(payload))
        self.assertEqual(decrypt_dat(container), payload)

    def test_xor_tail_is_unmasked(self):
        from src.media.dat_decrypt import decrypt_dat

        key = 0x37
        body = b"B" * 50
        tail = bytes(b ^ key for b in b"T" * 20)
        container = _v2(body + tail, xor_size=20)

        # Without the key the masked tail must not be guessed at.
        self.assertIsNone(decrypt_dat(container))
        self.assertEqual(decrypt_dat(container, key), body + b"T" * 20)

    def test_plaintext_middle_survives(self):
        from src.media.dat_decrypt import decrypt_dat

        # aesSize=0 with a long XOR tail: the middle is stored verbatim.
        key = 0x5A
        middle = bytes(range(256)) * 4
        tail = bytes(b ^ key for b in b"\x01" * 100)
        container = _v2(middle + tail, xor_size=100)
        self.assertEqual(decrypt_dat(container, key), middle + b"\x01" * 100)

    def test_missing_aes_key_refuses(self):
        from src.media.dat_decrypt import decrypt_dat

        container = _v2(b"\x00" * 1040, aes_size=1024)
        self.assertIsNone(decrypt_dat(container, 0x37, ""))

    def test_oversized_segment_refuses(self):
        from src.media.dat_decrypt import decrypt_dat

        # xorSize larger than what is left is a corrupt container.
        container = _v2(b"x" * 10, xor_size=999)
        self.assertIsNone(decrypt_dat(container, 0x37))

    def test_negative_segment_refuses(self):
        from src.media.dat_decrypt import decrypt_dat

        container = _v2(b"x" * 10, xor_size=-5)
        self.assertIsNone(decrypt_dat(container, 0x37))

    def test_unknown_signature_refuses(self):
        from src.media.dat_decrypt import decrypt_dat

        self.assertIsNone(decrypt_dat(b"NOTADAT" + b"\x00" * 40, 0x37))

    def test_v1_needs_xor_key(self):
        from src.media.dat_decrypt import decrypt_dat

        data = b"\x07\x08V1\x08\x07" + b"hello"
        self.assertIsNone(decrypt_dat(data))
        self.assertEqual(
            decrypt_dat(data, 0x01),
            bytes(b ^ 0x01 for b in data),
        )

    def test_expected_size_matches_real_rule(self):
        from src.media.dat_decrypt import expected_dat_size

        # aes_size=1024 is already block-aligned, so PKCS7 adds a full block
        # and the container is exactly length + 31.
        self.assertEqual(expected_dat_size(1000), 1031)
        self.assertEqual(expected_dat_size(0), 31)
        # No AES segment → only the 15-byte header.
        self.assertEqual(expected_dat_size(1000, 0), 1015)

    def test_key_normalisation(self):
        from src.media.dat_decrypt import aes_key_from_text, parse_xor_key

        self.assertEqual(parse_xor_key("0x37"), 0x37)
        self.assertEqual(parse_xor_key("55"), 55)
        self.assertEqual(parse_xor_key(0x37), 0x37)
        for bad in (None, "", "zz", 300, True):
            self.assertIsNone(parse_xor_key(bad), bad)

        # The key is the first 16 ASCII characters, *not* the hex bytes.
        self.assertEqual(
            aes_key_from_text("491e17ca4a5f665d47616022d98c0a3e"),
            b"491e17ca4a5f665d",
        )
        self.assertIsNone(aes_key_from_text("short"))

    def test_sniff_format(self):
        from src.media.dat_decrypt import sniff_format

        self.assertEqual(sniff_format(b"\xff\xd8\xff\xe0"), ".jpg")
        self.assertEqual(sniff_format(b"\x89PNG\r\n\x1a\n"), ".png")
        self.assertEqual(sniff_format(b"GIF89a"), ".gif")
        self.assertEqual(sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertEqual(sniff_format(b"wxgf" + b"\x00" * 20), "wxgf")
        # RIFF is shared with WAV/AVI — the form type must match.
        self.assertIsNone(sniff_format(b"RIFF\x00\x00\x00\x00WAVEfmt "))
        self.assertIsNone(sniff_format(b""))


class WxgfContainerTests(unittest.TestCase):
    """``wxgf`` is an HEVC wrapper; it must never be mistaken for an image."""

    def test_magic_detection(self):
        from src.media.wxgf import is_plain_image, is_wxgf

        self.assertTrue(is_wxgf(b"wxgf" + b"\x00" * 20))
        self.assertFalse(is_wxgf(b"\xff\xd8\xff"))
        self.assertFalse(is_wxgf(b""))

        self.assertTrue(is_plain_image(b"\xff\xd8\xff\xe0"))
        self.assertTrue(is_plain_image(b"\x89PNG\r\n\x1a\n"))
        self.assertFalse(is_plain_image(b"wxgf" + b"\x00" * 20))
        self.assertFalse(is_plain_image(b""))

    def test_plain_image_returned_unchanged(self):
        from src.media.wxgf import unwrap_wxgf

        # Some ``_hd_temp`` files are a bare PNG with no container at all.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        self.assertEqual(unwrap_wxgf(png), png)

    def test_embedded_jpeg_extracted(self):
        from src.media.wxgf import unwrap_wxgf

        jpeg = b"\xff\xd8\xff\xe0" + b"J" * 600
        container = b"wxgf" + b"\x00" * 12 + jpeg
        self.assertEqual(unwrap_wxgf(container), jpeg)

    def test_container_without_nalus_returns_none(self):
        from src.media.wxgf import unwrap_wxgf

        self.assertIsNone(unwrap_wxgf(b"wxgf" + b"\x00" * 40))
        self.assertIsNone(unwrap_wxgf(b""))
        self.assertIsNone(unwrap_wxgf(b"\x00" * 100))

    def test_missing_codec_degrades(self):
        from src.media.wxgf import unwrap_wxgf

        # A VPS/SPS/PPS/IDR sequence; without PyAV this must return None
        # rather than raise, so the caller keeps the placeholder.
        vps = b"\x00\x00\x00\x01" + bytes([32 << 1]) + b"\x01\x02"
        idr = b"\x00\x00\x00\x01" + bytes([19 << 1]) + b"\x03\x04"
        container = b"wxgf" + b"\x00" * 12 + vps + idr

        with patch.dict(sys.modules, {"av": None}):
            self.assertIsNone(unwrap_wxgf(container))


class ImageKeyTests(unittest.TestCase):
    """Where the per-machine image keys come from."""

    def _config(self, **kwargs):
        from src.config import BotConfig

        return BotConfig(**kwargs)

    def test_defaults_are_empty(self):
        from src.media.keys import resolve_image_keys

        keys = resolve_image_keys(self._config())
        self.assertIsNone(keys.xor_key)
        self.assertEqual(keys.aes_key_text, "")
        self.assertFalse(keys.usable)

    def test_env_provider(self):
        from src.media.keys import resolve_image_keys

        keys = resolve_image_keys(self._config(
            image_xor_key="0x37",
            image_aes_key="491e17ca4a5f665d47616022d98c0a3e",
        ))
        self.assertEqual(keys.xor_key, 0x37)
        self.assertEqual(keys.aes_key_text, "491e17ca4a5f665d47616022d98c0a3e")
        self.assertTrue(keys.usable)

    def test_invalid_values_are_dropped(self):
        from src.media.keys import resolve_image_keys

        keys = resolve_image_keys(self._config(
            image_xor_key="zz", image_aes_key="short",
        ))
        self.assertIsNone(keys.xor_key)
        self.assertEqual(keys.aes_key_text, "")
        self.assertFalse(keys.usable)

    def test_file_provider(self):
        import json

        from src.media.keys import resolve_image_keys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "image_keys.json").write_text(
                json.dumps({"xorKey": "55", "aesKey": "abcdefghijklmnopqrst"}),
                encoding="utf-8",
            )
            keys = resolve_image_keys(
                self._config(image_key_provider="file"), data_dir=root
            )
            self.assertEqual(keys.xor_key, 55)
            self.assertEqual(keys.aes_key_text, "abcdefghijklmnopqrst")

    def test_env_beats_file(self):
        import json

        from src.media.keys import resolve_image_keys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "image_keys.json").write_text(
                json.dumps({"xor_key": "55", "aes_key": "abcdefghijklmnopqrst"}),
                encoding="utf-8",
            )
            # An explicit .env entry must never be overridden by a stale file.
            keys = resolve_image_keys(
                self._config(image_key_provider="file", image_xor_key="0x99"),
                data_dir=root,
            )
            self.assertEqual(keys.xor_key, 0x99)
            self.assertEqual(keys.aes_key_text, "abcdefghijklmnopqrst")

    def test_missing_or_broken_file_is_not_fatal(self):
        from src.media.keys import resolve_image_keys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(image_key_provider="file")
            self.assertFalse(resolve_image_keys(config, data_dir=root).usable)

            (root / "image_keys.json").write_text("{not json", encoding="utf-8")
            self.assertFalse(resolve_image_keys(config, data_dir=root).usable)

            (root / "image_keys.json").write_text("[1, 2]", encoding="utf-8")
            self.assertFalse(resolve_image_keys(config, data_dir=root).usable)

    def test_unknown_provider_falls_back_to_env(self):
        from src.media.keys import resolve_image_keys

        keys = resolve_image_keys(self._config(
            image_key_provider="weird", image_xor_key="0x01",
        ))
        self.assertEqual(keys.xor_key, 1)


class ImageKeyTableTests(unittest.TestCase):
    """The per-account key table, which multi-account machines need.

    WeChat issues a different AES key per account and the wrong one decrypts
    nothing (measured 0/1 against 3808/3808), so "one pair for everybody" is
    only safe when there is exactly one account to pair it with.
    """

    def _config(self, **kwargs):
        from src.config import BotConfig

        return BotConfig(**kwargs)

    def _key(self, wxid, xor_key=0x37, aes="0123456789abcdef"):
        from src.wechat.extract_key import ImageKey

        return ImageKey(wxid, xor_key, aes)

    # -- ImageKeyTable ---------------------------------------------------

    def test_for_wxid_returns_that_accounts_pair(self):
        from src.media.keys import ImageKeyTable, ImageKeys

        table = ImageKeyTable(by_wxid={
            "wxid_a": ImageKeys(0x11, "aaaaaaaaaaaaaaaa"),
            "wxid_b": ImageKeys(0x22, "bbbbbbbbbbbbbbbb"),
        })
        self.assertEqual(table.for_wxid("wxid_b").xor_key, 0x22)
        self.assertEqual(table.for_wxid("wxid_b").aes_key_text, "b" * 16)

    def test_unknown_account_does_not_borrow_from_several(self):
        """With several accounts, guessing would decrypt with the wrong key."""
        from src.media.keys import ImageKeyTable, ImageKeys

        shared = ImageKeys(None, "")
        table = ImageKeyTable(shared=shared, by_wxid={
            "wxid_a": ImageKeys(0x11, "aaaaaaaaaaaaaaaa"),
            "wxid_b": ImageKeys(0x22, "bbbbbbbbbbbbbbbb"),
        })
        self.assertIs(table.for_wxid(None), shared)
        self.assertIs(table.for_wxid("wxid_unknown"), shared)

    def test_single_account_is_used_for_unknown_owner(self):
        """A custom data dir need not contain a wxid folder to name it."""
        from src.media.keys import ImageKeyTable, ImageKeys

        only = ImageKeys(0x33, "cccccccccccccccc")
        table = ImageKeyTable(by_wxid={"wxid_only": only})
        self.assertIs(table.for_wxid(None), only)
        self.assertIs(table.for_wxid("wxid_other"), only)

    def test_empty_table_is_not_usable(self):
        from src.media.keys import ImageKeyTable

        table = ImageKeyTable()
        self.assertFalse(table.usable)
        self.assertEqual(len(table), 0)

    # -- save / load round trip ------------------------------------------

    def test_saved_keys_are_read_back_per_account(self):
        from src.media.keys import read_key_file_table, save_image_keys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_image_keys(
                {"wxid_a": self._key("wxid_a"),
                 "wxid_b": self._key("wxid_b", 0x22, "bbbbbbbbbbbbbbbb")},
                data_dir=root,
            )
            table = read_key_file_table(root)
            self.assertEqual(sorted(table), ["wxid_a", "wxid_b"])
            self.assertEqual(table["wxid_b"].xor_key, 0x22)
            self.assertEqual(table["wxid_b"].aes_key_text, "b" * 16)

    def test_flat_key_file_yields_no_table(self):
        """The older single-pair file still works for the ``file`` provider."""
        import json

        from src.media.keys import read_key_file_table

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "image_keys.json").write_text(
                json.dumps({"xor_key": "55", "aes_key": "abcdefghijklmnop"}),
                encoding="utf-8",
            )
            self.assertEqual(read_key_file_table(root), {})

    # -- resolve_image_key_table ----------------------------------------

    def test_non_auto_provider_yields_one_shared_pair(self):
        from src.media.keys import resolve_image_key_table

        table = resolve_image_key_table(self._config(
            image_xor_key="0x37", image_aes_key="0123456789abcdef",
        ))
        self.assertEqual(len(table), 0)
        self.assertEqual(table.for_wxid(None).xor_key, 0x37)

    def test_auto_provider_builds_the_table(self):
        from src.media.keys import resolve_image_key_table

        with patch("src.media.keys._extract_auto_table", return_value={
            "wxid_a": self._key("wxid_a"),
            "wxid_b": self._key("wxid_b", 0x22, "bbbbbbbbbbbbbbbb"),
        }):
            table = resolve_image_key_table(self._config(image_key_provider="auto"))

        self.assertEqual(sorted(table.by_wxid), ["wxid_a", "wxid_b"])
        self.assertEqual(table.for_wxid("wxid_b").xor_key, 0x22)

    def test_auto_provider_prefers_saved_keys_when_wechat_is_closed(self):
        """GetImageKey only answers while the client runs."""
        from src.media.keys import resolve_image_key_table, save_image_keys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_image_keys({"wxid_saved": self._key("wxid_saved", 0x44)},
                            data_dir=root)
            with patch("src.media.keys._extract_auto_table", return_value={}):
                table = resolve_image_key_table(
                    self._config(image_key_provider="auto"), data_dir=root
                )
        self.assertEqual(table.for_wxid("wxid_saved").xor_key, 0x44)

    def test_auto_provider_without_anything_is_empty(self):
        from src.media.keys import resolve_image_key_table

        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.media.keys._extract_auto_table", return_value={}):
                table = resolve_image_key_table(
                    self._config(image_key_provider="auto"), data_dir=Path(tmp)
                )
        self.assertFalse(table.usable)


class ImageStoreTests(unittest.TestCase):
    """Locating a ``.dat`` for a message, then decrypting it."""

    CHAT = "44600994796@chatroom"

    def _store(self, root: Path, **kwargs):
        from src.media.image import ImageStore

        params = {"xor_key": 0x37, "aes_key_text": "0123456789abcdef"}
        params.update(kwargs)
        return ImageStore(str(root), root / "cache", **params)

    def _img_dir(self, root: Path) -> Path:
        import hashlib

        digest = hashlib.md5(self.CHAT.encode()).hexdigest()
        img = root / "msg" / "attach" / digest / "2026-03" / "Img"
        img.mkdir(parents=True)
        return img

    def _msg(self, md5: str, length: int) -> dict:
        return {
            "chat_id": self.CHAT,
            "localType": 3,
            "create_time": 1772000000,
            "content_kind": "text",
            "message_content": f'<msg><img md5="{md5}" length="{length}" /></msg>',
        }

    def test_variant_suffixes(self):
        from src.media.image import strip_variant, variant_rank

        base = "a" * 32
        for suffix in ("_h", "_hd", "_b", "_w", "_c", "_t", ".t", ".h", "_thumb"):
            self.assertEqual(strip_variant(base + suffix), base, suffix)
        self.assertEqual(strip_variant(base), base)

        # A full-size variant must beat the thumbnail, and the unsuffixed
        # original must sit between them.
        self.assertGreater(variant_rank(base + "_h"), variant_rank(base))
        self.assertGreater(variant_rank(base), variant_rank(base + "_t"))

    def test_locate_by_md5_prefers_full_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "a" * 32
            full = img / f"{md5}_h.dat"
            thumb = img / f"{md5}_t.dat"
            full.write_bytes(_v2(b"\xff\xd8\xff" + b"x" * 100))
            thumb.write_bytes(_v2(b"\xff\xd8\xff" + b"y" * 20))

            store = self._store(root)
            found = store._locate(self._msg(md5, 100), self._msg(md5, 100)["message_content"])
            self.assertEqual(found, full)

    def test_locate_by_size_when_md5_differs(self):
        from src.media.dat_decrypt import expected_dat_size

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            payload = b"\xff\xd8\xff" + b"x" * 1000
            dat = img / f"{'a' * 32}.dat"
            dat.write_bytes(_v2(payload))
            length = dat.stat().st_size - 31
            self.assertEqual(expected_dat_size(length), dat.stat().st_size)

            store = self._store(root)
            # The XML md5 is not the filename, so the size rule is the only
            # thing that can pair this message with its file.
            xml = self._msg("b" * 32, length)["message_content"]
            self.assertEqual(store._locate(self._msg("b" * 32, length), xml), dat)

    def test_locate_by_hevc_mid_size_when_md5_differs(self):
        """Real data: the XML ``md5`` names no file, ``hevc_mid_size`` names it.

        Measured on 13 consecutive images in one group — the XML md5 matched
        0 on-disk base names, while ``hevc_mid_size + 31`` matched 13.  The
        original is a ``wxgf`` container, so this rule is what carries image
        description; without it every picture stayed ``[图片]``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            payload = b"wxgf" + b"\x00" * 8 + b"\xff\xd8\xff" + b"x" * 1000
            dat = img / f"{'c' * 32}.dat"
            dat.write_bytes(_v2(payload, aes_size=1024))
            plain_len = dat.stat().st_size - 31

            msg = {
                "chat_id": self.CHAT,
                "localType": 3,
                "create_time": 1772000000,
                "content_kind": "text",
                # The md5 below appears in no filename, exactly as in real data.
                "message_content": (
                    f'<msg><img md5="{"d" * 32}" hevc_mid_size="{plain_len}" '
                    f'cdnthumblength="20" length="{plain_len + 500}" /></msg>'
                ),
            }
            store = self._store(root)
            self.assertEqual(store._locate(msg, msg["message_content"]), dat)

    def test_locate_prefers_whole_group_of_the_matching_picture(self):
        """Same-size pictures are told apart by write time, not by rank.

        Two different pictures can be stored at the same byte size.  Picking
        by file name alone cannot tell them apart, and picking a wrong one
        would describe someone else's image, which is worse than ``[图片]``.
        A picture's variants are written together, so the group closest to
        the message is the right one and every variant in it is fair game.
        """
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            body = b"\xff\xd8\xff" + b"x" * 1000

            older_a = img / f"{'a' * 32}.dat"
            older_a.write_bytes(_v2(body, aes_size=1024))
            newer_b = img / f"{'b' * 32}.dat"
            newer_b.write_bytes(_v2(body, aes_size=1024))
            # Same size, one day apart: only time can separate them.
            os.utime(older_a, (1771900000, 1771900000))
            os.utime(newer_b, (1772000000, 1772000000))

            thumb_b = img / f"{'b' * 32}_t.dat"
            thumb_b.write_bytes(_v2(b"\xff\xd8\xff" + b"y" * 40, aes_size=1024))

            msg = {
                "chat_id": self.CHAT,
                "localType": 3,
                "create_time": 1772000000,
                "content_kind": "text",
                "message_content": (
                    f'<msg><img md5="{"e" * 32}" '
                    f'hevc_mid_size="{newer_b.stat().st_size - 31}" /></msg>'
                ),
            }
            store = self._store(root)
            found = store._locate(msg, msg["message_content"])
            # The full-size copy of the newer picture, not the older one.
            self.assertEqual(found, newer_b)

    def test_locate_without_chat_id_gives_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._img_dir(root)
            store = self._store(root)
            self.assertIsNone(store._locate({"localType": 3}, "<msg/>"))

    def test_fetch_without_keys_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "a" * 32
            (img / f"{md5}.dat").write_bytes(_v2(b"\xff\xd8\xff" + b"x" * 100))

            # Derivation disabled: with no key at all there is nothing to try.
            store = self._store(
                root, xor_key=None, aes_key_text="", allow_derive_xor=False
            )
            self.assertFalse(store.can_decrypt)
            self.assertIsNone(store.fetch(self._msg(md5, 100)))

    def test_xor_key_is_derived_when_not_configured(self):
        """The XOR key is recoverable from the files, not a secret to supply."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            key = 0xED
            # Three files sharing one key — enough votes to settle it.
            for i in range(3):
                md5 = f"{i}" * 32
                jpeg = b"\xff\xd8\xff\xe0" + b"J" * 200 + b"\xff\xd9"
                tail = bytes(b ^ key for b in jpeg[-16:])
                (img / f"{md5}.dat").write_bytes(
                    _v2(jpeg[:-16] + tail, xor_size=16)
                )

            store = self._store(
                root, xor_key=None, aes_key_text="", allow_derive_xor=True
            )
            self.assertEqual(store._resolve_xor_key(), key)
            # The answer is cached: a second call must not rescan.
            self.assertEqual(store._resolve_xor_key(), key)

    def test_derivation_gives_up_when_undecidable(self):
        """Too few matching files must not produce a guess."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            # A tail that no key can turn into a terminator.
            (img / f"{'a' * 32}.dat").write_bytes(
                _v2(b"\xff\xd8\xff" + b"x" * 100, xor_size=16)
            )

            store = self._store(
                root, xor_key=None, aes_key_text="", allow_derive_xor=True
            )
            self.assertIsNone(store._resolve_xor_key())

    def test_configured_xor_key_is_never_overridden(self):
        """An explicit key wins; derivation must not run at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._img_dir(root)

            store = self._store(root, xor_key=0x37, allow_derive_xor=True)
            self.assertEqual(store._resolve_xor_key(), 0x37)
            self.assertFalse(store._xor_derive_done)

    def test_fetch_decrypts_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "a" * 32
            jpeg = b"\xff\xd8\xff\xe0" + b"J" * 2000 + b"\xff\xd9"
            (img / f"{md5}.dat").write_bytes(_v2(jpeg))

            store = self._store(root)
            out = store.fetch(self._msg(md5, 2000))
            self.assertIsNotNone(out)
            self.assertEqual(out.read_bytes(), jpeg)
            # Second call must hit the cache, not re-decrypt.
            self.assertEqual(store.fetch(self._msg(md5, 2000)), out)

    def test_fetch_rejects_non_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "a" * 32
            # Decrypts cleanly but is not an image — a wrong key looks like
            # this, and it must not reach the vision model or the cache.
            (img / f"{md5}.dat").write_bytes(_v2(b"\x00" * 500))

            store = self._store(root)
            self.assertIsNone(store.fetch(self._msg(md5, 500)))
            self.assertFalse((root / "cache" / f"{md5}.jpg").exists())

    def test_wxgf_falls_back_to_plain_sibling(self):
        """An HEVC container must borrow the ``_h`` JPEG of the same image.

        The packaged build excludes PyAV, so a ``wxgf`` payload cannot be
        decoded there.  WeChat stores the same picture again as a plain JPEG
        sibling, and without this fallback *every* such image degraded to
        ``[图片]`` — measured 1683/1683 images on a real account.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "a" * 32
            jpeg = b"\xff\xd8\xff\xe0" + b"J" * 2000 + b"\xff\xd9"

            # The container cannot be decoded without a codec...
            vps = b"\x00\x00\x00\x01" + bytes([32 << 1]) + b"\x01\x02"
            idr = b"\x00\x00\x00\x01" + bytes([19 << 1]) + b"\x03\x04"
            (img / f"{md5}.dat").write_bytes(
                _v2(b"wxgf" + b"\x00" * 12 + vps + idr)
            )
            # ...but the plain sibling can.
            (img / f"{md5}_h.dat").write_bytes(_v2(jpeg))

            store = self._store(root)
            with patch.dict(sys.modules, {"av": None}):
                out = store.fetch(self._msg(md5, 2000))
            self.assertIsNotNone(out)
            self.assertEqual(out.read_bytes(), jpeg)

    def test_wxgf_without_sibling_stays_placeholder(self):
        """No codec and no sibling means the placeholder, never a fake image."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "b" * 32
            vps = b"\x00\x00\x00\x01" + bytes([32 << 1]) + b"\x01\x02"
            idr = b"\x00\x00\x00\x01" + bytes([19 << 1]) + b"\x03\x04"
            (img / f"{md5}.dat").write_bytes(
                _v2(b"wxgf" + b"\x00" * 12 + vps + idr)
            )

            store = self._store(root)
            with patch.dict(sys.modules, {"av": None}):
                self.assertIsNone(store.fetch(self._msg(md5, 2000)))
            self.assertFalse((root / "cache" / f"{md5}.jpg").exists())

    def test_new_message_waits_for_the_file_to_land(self):
        """WeChat writes the row first and the picture ~2 s later.

        The poll interval is 1 s, so without a retry the tick that sees the
        message finds no file and the image stays ``[图片]`` for good — this
        happened for every image sent while the bot was running.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img = self._img_dir(root)
            md5 = "c" * 32
            jpeg = b"\xff\xd8\xff\xe0" + b"J" * 2000 + b"\xff\xd9"
            msg = self._msg(md5, 2000)
            msg["create_time"] = int(time.time())  # just arrived

            store = self._store(root)
            calls = {"n": 0}
            real_fetch_once = store._fetch_once

            def slow_disk(m, xml):
                """No file on the first look, written by the second."""
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                (img / f"{md5}.dat").write_bytes(_v2(jpeg))
                return real_fetch_once(m, xml)

            with patch.object(store, "_fetch_once", side_effect=slow_disk), \
                 patch("src.media.image._ARRIVAL_POLL_SEC", 0):
                out = store.fetch(msg)

            self.assertIsNotNone(out)
            self.assertEqual(out.read_bytes(), jpeg)
            self.assertEqual(calls["n"], 2)

    def test_old_message_does_not_wait(self):
        """An image never opened in the client has no file and never will.

        Waiting on those would stall the queue for seconds per message.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._img_dir(root)
            md5 = "d" * 32
            msg = self._msg(md5, 2000)
            msg["create_time"] = int(time.time()) - 3600  # an hour old

            store = self._store(root)
            calls = {"n": 0}

            def counted(m, xml):
                calls["n"] += 1
                return None

            with patch.object(store, "_fetch_once", side_effect=counted):
                self.assertIsNone(store.fetch(msg))
            self.assertEqual(calls["n"], 1)


class MediaPipelineImageTests(unittest.TestCase):
    """The pipeline must keep ``[图片]`` unless keys *and* vision are ready."""

    def _pipeline(self, **overrides):
        from src.media.pipeline import MediaPipeline

        config = MagicMock()
        config.media_vision_enabled = True
        config.media_vision_model = ""
        config.wechat_data_dir = ""
        config.ai_backend = "deepseek"
        config.deepseek_api_key = "sk-test"
        config.deepseek_model = "deepseek-v4-flash"
        config.deepseek_base_url = "https://api.deepseek.com"
        config.image_xor_key = ""
        config.image_aes_key = ""
        config.image_key_provider = "env"
        for key, value in overrides.items():
            setattr(config, key, value)
        return MediaPipeline(config)

    def test_no_keys_means_no_image_store(self):
        self.assertIsNone(self._pipeline()._image)

    def test_keys_build_the_store(self):
        pipeline = self._pipeline(
            image_xor_key="0x37", image_aes_key="0123456789abcdef",
        )
        self.assertIsNotNone(pipeline._image)
        self.assertTrue(pipeline._image.can_decrypt)

    def test_image_without_location_returns_none(self):
        msg = {
            "localType": 3,
            "content_kind": "blob",
            "message_content": _blob("<msg><img md5=\"" + "a" * 32 + "\"/></msg>"),
        }
        pipeline = self._pipeline(image_xor_key="0x37")
        # No chat_id and no data dir: nothing can be located, so the caller
        # keeps "[图片]".
        self.assertIsNone(pipeline.process(msg))


if __name__ == "__main__":
    unittest.main()
