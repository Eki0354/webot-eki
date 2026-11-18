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
* **image (localType=3)** — *not* supported.  The ``.dat`` container is
  neither XOR (payload length is not block-aligned under either scheme) nor
  AES with the XML ``aeskey`` (0/5 on targeted samples), and its CDN
  thumbnails answer HTTP 400 because the filekey needs client-side signing.
  These fall back to ``[图片]``.
"""

import logging
from typing import Optional

from src.config import PROJECT_ROOT

from .appmsg import looks_like_xml, reduce_appmsg, strip_sender_prefix
from .blob import decode_blob, is_binary
from .emoji import EmojiStore
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
                getattr(config, "wechat_data_dir", ""),
                PROJECT_ROOT / "data" / "emoji_cache",
            )
            if self._vision
            else None
        )
        self._stats = {"emoji": 0, "described": 0, "failed": 0}
        if self._vision_on:
            logger.info(
                "MediaPipeline vision %s (model=%s)",
                "ready" if self._vision else "unavailable",
                getattr(config, "media_vision_model", "") or "backend default",
            )

    # -- Public ------------------------------------------------------------

    def process(self, msg: dict) -> Optional[str]:
        """Describe a media message.

        Args:
            msg: Raw WCDB message dict.

        Returns:
            A short description, or None when nothing could be extracted
            (the caller should then use a plain placeholder).
        """
        try:
            local_type = int(msg.get("localType", msg.get("msg_type", 1)))
        except (TypeError, ValueError):
            return None

        if local_type == LOCAL_TYPE_IMAGE:
            # Container not decryptable — say so plainly rather than
            # spending a vision call on an unreadable file.
            return None

        if local_type == LOCAL_TYPE_EMOJI:
            return self._describe_emoji(msg)

        if is_binary(msg) or local_type >= LOCAL_TYPE_SYSTEM:
            return self._reduce_binary(msg, local_type)

        return None

    def stats(self) -> dict:
        return dict(self._stats)

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
            disable_thinking = False
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

    def _describe_emoji(self, msg: dict) -> Optional[str]:
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
            return None

        description = self._vision.describe(image)
        if description:
            self._stats["described"] += 1
            return f"[表情] {description}"
        self._stats["failed"] += 1
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
