"""Turn a WeChat media message into text the bot can use.

Entry point is ``MediaPipeline.process(msg)``, mirroring
``VoicePipeline.process(msg)``: it takes a raw WCDB message dict and returns
a string, or None when nothing could be extracted.

Two independent layers, deliberately not gated by the same switch:

* **Text reduction** (always on) — appmsg XML becomes ``"[链接] 标题"`` and a
  compressed plain-text body becomes its text.  This costs nothing and keeps
  base64 out of the AI prompt, so it must not depend on vision being enabled.
* **Vision description** (``MEDIA_VISION_ENABLED``) — emoji images are
  downloaded and described by the configured AI backend.

What is supported, and why:

* **emoji (localType=47)** — local ``business/emoticon/Persist`` cache, else
  the signed ``cdnurl`` in the message XML, then a vision description.
  Measured on real data: 3 of 9 emoji had a usable local file, and the
  other 6 downloaded from the CDN; 6 of 9 produced a description.  The
  remaining 3 carry no ``cdnurl`` *and* their local file is an opaque
  container that is neither zstd nor zlib, so they degrade to ``[表情]``.
* **image (localType=3)** — the local ``.dat`` file is decrypted (four-segment
  container) and a ``wxgf`` payload is unwrapped to JPEG, then described.
  This needs the per-machine image keys (``IMAGE_XOR_KEY`` /
  ``IMAGE_AES_KEY``) *and* vision; without either, the message keeps its
  ``[图片]`` placeholder.  See :mod:`src.media.image`.
"""

import logging
from typing import Optional

from src.config import PROJECT_ROOT, resolve_wechat_data_dir

from .appmsg import (
    appmsg_title,
    extract_refermsg,
    looks_like_xml,
    reduce_appmsg,
    strip_sender_prefix,
)
from .blob import decode_blob, is_binary
from .emoji import EmojiStore
from .image import ImageStore
from .keys import resolve_image_key_table, resolve_image_keys
from .vision import VisionClient

logger = logging.getLogger(__name__)

# localType values seen in real data.
LOCAL_TYPE_IMAGE = 3
LOCAL_TYPE_EMOJI = 47
LOCAL_TYPE_SYSTEM = 10000


class MediaPipeline:
    """Describe media messages as short text.

    Usage::

        pipeline = MediaPipeline(config)
        text = pipeline.process(msg)
        # → "[表情] 一只猫在摇头" / "[链接] 某某新闻" / None
    """

    def __init__(self, config) -> None:
        """
        Args:
            config: BotConfig instance.
        """
        self._vision_on = bool(getattr(config, "media_vision_enabled", False))
        self._vision: Optional[VisionClient] = (
            self._create_vision(config) if self._vision_on else None
        )
        self._emoji: Optional[EmojiStore] = (
            EmojiStore(
                resolve_wechat_data_dir(config),
                PROJECT_ROOT / "data" / "emoji_cache",
            )
            if self._vision
            else None
        )
        self._image: Optional[ImageStore] = (
            self._create_image(config) if self._vision else None
        )
        self._stats = {
            "emoji": 0, "image": 0, "described": 0, "failed": 0,
        }
        if self._vision_on:
            logger.info(
                "MediaPipeline vision %s (model=%s); image decryption %s",
                "ready" if self._vision else "unavailable",
                getattr(config, "media_vision_model", "") or "backend default",
                "ready" if (self._image and self._image.can_decrypt)
                else "no keys",
            )

    # -- Public ------------------------------------------------------------

    def process(self, msg: dict, chat_id: str = "") -> Optional[str]:
        """Describe a media message.

        Args:
            msg: Raw WCDB message dict.
            chat_id: Session the message belongs to.  Image files live under
                ``msg/attach/{md5(chat_id)}/``, so an image message cannot be
                located without it.

        Returns:
            A short description, or None when nothing could be extracted
            (the caller should then use a plain placeholder).
        """
        try:
            local_type = int(msg.get("localType", msg.get("msg_type", 1)))
        except (TypeError, ValueError):
            return None

        if local_type == LOCAL_TYPE_IMAGE:
            return self._describe_image(msg, chat_id)

        if local_type == LOCAL_TYPE_EMOJI:
            return self._describe_emoji(msg, chat_id)

        if is_binary(msg) or local_type >= LOCAL_TYPE_SYSTEM:
            # A reply carries the quoted message inside <refermsg>.  For an
            # image that is the only place its payload appears, so it has to
            # be tried before the generic appmsg reduction — otherwise the
            # whole message reduces to "[直播]" and the picture is lost.
            quoted = self._describe_quoted(msg, chat_id)
            if quoted:
                return quoted
            return self._reduce_binary(msg, local_type)

        return None

    def stats(self) -> dict:
        return dict(self._stats)

    def _vision_model(self) -> str:
        """Model name for log lines; "?" when there is no client."""
        if self._vision is None:
            return "?"
        return getattr(self._vision, "model", "?")

    def _vision_error(self) -> str:
        """Why the last vision call produced nothing, for the log line."""
        if self._vision is None:
            return "vision disabled"
        return getattr(self._vision, "last_error", "") or "unknown reason"

    @staticmethod
    def _msg_id(msg: dict) -> str:
        """Best available id for a log line.

        Raw WCDB messages have no ``message_id`` — that is assigned later,
        during standardization.  Reading only that key logged ``msg=?`` for
        every failure, which made a specific broken sticker impossible to
        find in the log.
        """
        return str(
            msg.get("message_id")
            or msg.get("server_id")
            or msg.get("local_id")
            or "?"
        )

    # -- Internals ---------------------------------------------------------

    def _create_vision(self, config) -> Optional[VisionClient]:
        """Build a vision client from whichever backend is configured."""
        backend = (getattr(config, "ai_backend", "") or "").lower()
        override = getattr(config, "media_vision_model", "")

        if backend == "deepseek":
            key = getattr(config, "deepseek_api_key", "")
            model = override or getattr(config, "deepseek_model", "")
            base = getattr(config, "deepseek_base_url", "")
            disable_thinking = True
        elif backend == "openai":
            key = getattr(config, "openai_api_key", "")
            model = override or getattr(config, "openai_model", "")
            base = getattr(config, "openai_base_url", "")
            # The ``openai`` backend also covers proxies that front a
            # reasoning model (one such proxy reported every image as
            # ``finish_reason=length`` with empty content, 19 of 22).  Ask
            # for reasoning off here too; a real OpenAI endpoint rejects the
            # field and the client drops it for the rest of the run.
            disable_thinking = True
        else:
            # Claude's vision API is shaped differently; skip rather than
            # guess.  Emoji still degrade to "[表情]".
            logger.info("MediaPipeline: no vision support for backend=%r", backend)
            return None

        if not key:
            logger.warning(
                "MediaPipeline: %s API key missing — vision disabled", backend
            )
            return None
        return VisionClient(key, model, base, disable_thinking)

    def _create_image(self, config) -> Optional[ImageStore]:
        """Build the image store, or None when nothing can be decrypted.

        The XOR key no longer has to be configured: it is recoverable from
        the ``.dat`` files themselves, so a store is still worth building
        when only the AES key is present.  With neither key, a V2 container
        cannot be opened at all, so scanning the tree would be pure cost.

        The key table is passed through even when it looks empty: with
        ``IMAGE_KEY_PROVIDER=auto`` the keys are read from the running WeChat
        client and a per-account table is the only correct form on a machine
        with several accounts.
        """
        table = resolve_image_key_table(config)
        keys = table.for_wxid(None)
        if not table.usable and not keys.usable:
            logger.info(
                "MediaPipeline: no image keys — images stay [图片]. "
                "Set IMAGE_KEY_PROVIDER=auto, or fill IMAGE_AES_KEY."
            )
            return None
        if len(table):
            logger.info(
                "MediaPipeline: image keys loaded for %d account(s)", len(table)
            )
        return ImageStore(
            resolve_wechat_data_dir(config),
            PROJECT_ROOT / "data" / "image_cache",
            keys.xor_key,
            keys.aes_key_text,
            # Only worth deriving when the user did not already pin it down.
            allow_derive_xor=keys.xor_key is None,
            key_table=table,
        )

    def _describe_image(self, msg: dict, chat_id: str) -> Optional[str]:
        """Decrypt the image and describe it."""
        if not self._vision or not self._vision.available or not self._image:
            return None

        self._stats["image"] += 1
        # The raw message carries no session id, and the file lives under a
        # directory named after it, so hand the pipeline's caller a copy
        # rather than mutating the message the backend still needs.
        located = dict(msg)
        if chat_id:
            located.setdefault("chat_id", chat_id)

        image = self._image.fetch(located)
        if not image:
            self._stats["failed"] += 1
            logger.warning(
                "MediaPipeline: image not decrypted (chat=%s, msg=%s)",
                chat_id or "?", msg.get("message_id", "?"),
            )
            return None

        description = self._vision.describe(image)
        if description:
            self._stats["described"] += 1
            return f"[图片] {description}"
        self._stats["failed"] += 1
        logger.warning(
            "MediaPipeline: vision gave no text for %s (model=%s): %s",
            image.name, self._vision_model(), self._vision_error(),
        )
        return None

    def _describe_quoted(self, msg: dict, chat_id: str) -> Optional[str]:
        """Describe the media this message quotes, if it quotes any.

        WeChat 4.x sends a reply as an appmsg whose ``<refermsg>`` block
        holds the original message.  For media, that block is the only place
        the payload appears — the reply's own body carries just the sender's
        wording.  So the quoted ``<img>`` / ``<emoji>`` XML is fed back
        through the same locate → decrypt → describe path a standalone
        message takes.

        Both media kinds that a member actually quotes are handled.  A
        sticker is the one people ask about most ("这张表情包什么内容"),
        and it arrives as ``<refermsg><type>47</type>`` carrying an
        ``<emoji md5=…>`` payload.  Handling only ``type=3`` images made
        the reply reduce to the sender's own words with no media in it at
        all, so the model could only answer that it saw no picture.

        Returns:
            The sender's wording plus the media's content, or None when this
            message quotes no describable media.
        """
        if not self._vision or not self._vision.available:
            return None

        xml = self._body_xml(msg)
        if not xml:
            return None
        body = strip_sender_prefix(xml)
        refer = extract_refermsg(body)
        if not refer:
            return None
        quoted_xml = refer.get("content") or ""
        if not quoted_xml:
            return None

        kind = int(refer.get("type") or 0)
        # A sticker payload is recognisable on its own: some clients report
        # the quote's type as the reply envelope's, but the ``<emoji>`` tag
        # is unambiguous.  Checking the payload as well keeps a sticker from
        # being dropped when only its type field is odd.
        if kind == LOCAL_TYPE_EMOJI or "<emoji" in quoted_xml:
            label, prefix, describe = (
                "表情包", "[表情] ", self._describe_emoji
            )
        elif kind == LOCAL_TYPE_IMAGE:
            if not self._image:
                return None
            label, prefix, describe = "图片", "[图片] ", self._describe_image
        else:
            return None

        # Present the quoted media as if it were its own message.  The
        # session and timestamp still come from the reply, which is what
        # names the account folder and month directory.
        proxy = dict(msg)
        proxy["message_content"] = quoted_xml
        proxy["content_kind"] = "text"
        proxy.pop("content", None)

        title = appmsg_title(body)
        description = describe(proxy, chat_id)
        if not description:
            logger.warning(
                "MediaPipeline: quoted %s not described (chat=%s, msg=%s)",
                label, chat_id or "?", self._msg_id(msg),
            )
            return f"{title}（引用的{label}没能解析，看不到内容）".strip()

        detail = description
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
        return f"{title}（引用的{label}：{detail}）".strip()

    def _describe_emoji(self, msg: dict, chat_id: str = "") -> Optional[str]:
        """Download/locate the emoji image and describe it."""
        if not self._vision or not self._vision.available or not self._emoji:
            return None

        xml = self._body_xml(msg)
        if not xml:
            return None

        self._stats["emoji"] += 1
        image = self._emoji.fetch(xml)
        if not image:
            self._stats["failed"] += 1
            logger.warning(
                "MediaPipeline: emoji image not found (chat=%s, msg=%s)",
                chat_id or msg.get("chat_id") or "?", self._msg_id(msg),
            )
            return None

        description = self._vision.describe(image)
        if description:
            self._stats["described"] += 1
            return f"[表情] {description}"
        self._stats["failed"] += 1
        logger.warning(
            "MediaPipeline: vision gave no text for emoji %s (model=%s): %s",
            image.name, self._vision_model(), self._vision_error(),
        )
        return None

    def _reduce_binary(self, msg: dict, local_type: int) -> Optional[str]:
        """Reduce a BLOB body to text, a label, or None."""
        text = self._body_xml(msg)
        if not text:
            return None

        body = strip_sender_prefix(text)

        # System notices are join/leave/revoke events — the backend filters
        # handle those, and returning markup would leak XML into prompts.
        if local_type == LOCAL_TYPE_SYSTEM:
            return None

        if looks_like_xml(body):
            return reduce_appmsg(body)

        # A compressed plain-text message — return the text itself.
        return body.strip() or None

    @staticmethod
    def _body_xml(msg: dict) -> Optional[str]:
        """Decode the message body to text, if it is a BLOB."""
        raw = msg.get("message_content") or msg.get("content") or ""
        if not is_binary(msg):
            # Already text (e.g. an old-format emoji message).
            return str(raw) if raw else None
        return decode_blob(str(raw))
