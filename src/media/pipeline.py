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

import concurrent.futures
import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

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

#: Candidate media addresses tried per clip.  A page usually lists the same
#: rendition on two CDNs; the first is what the site itself plays, the second
#: is worth one retry when it refuses the request.  More than that only adds
#: latency to a reply.
_MAX_VIDEO_SOURCES = 2

#: How long a clip's speech may take to transcribe before the reply goes on
#: without it, in seconds.
#:
#: Someone is waiting for this answer, so the cap is a deadline and not an
#: estimate: measured on this machine, two minutes of audio takes ~7.5 s with
#: ``small`` at ``int8`` on 6 threads (16x realtime), and a slower VM can
#: take several times that — plus ~23 s for the first model load.  At 90 s
#: a clip that would have made the reply feel stuck instead degrades to the
#: frame-only description the bot already produced without this feature.
_VIDEO_ASR_TIMEOUT_SEC = 90.0


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
        #: Transcribing a clip's speech.  Off by default: it costs CPU time on
        #: the reply path and, on a first run, a several-hundred-megabyte
        #: model download — neither should happen behind someone's back.
        self._video_asr = bool(
            getattr(config, "media_video_asr_enabled", False)
        )
        #: Built on first use, not here: constructing a local Whisper model
        #: loads ~500 MB of weights, which a bot that never sees a video
        #: should not pay for at startup.
        self._asr = None
        self._config = config
        self._stats = {
            "emoji": 0, "image": 0, "video": 0, "asr": 0,
            "described": 0, "failed": 0,
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

    def set_emoji_url_lookup(
        self, lookup: Optional[Callable[[str, str], Optional[str]]],
    ) -> None:
        """Let the caller resolve a stripped sticker's download address.

        A chat may store an emoji body as just ``<emoji md5=… len=…/>`` while
        the same sticker arrived elsewhere with a signed ``cdnurl``.  Without
        this hook such a message can only fall back to its local file, which
        is usually an opaque container.
        """
        if self._emoji is not None:
            self._emoji.set_url_lookup(lookup)

    def remember_emoji_urls(self, entries: dict[str, str]) -> None:
        """Persist sticker download addresses harvested by the caller."""
        if self._emoji is not None:
            self._emoji.remember_urls(entries)

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

    def describe_video(
        self, video_urls: tuple[str, ...] | list[str], page_url: str,
    ) -> Optional[str]:
        """Sample a shared clip and describe what it shows and says.

        A chat completion receives text only, so "你能看到这个视频吗" used to
        be answered with "看不到" even when the page's media address was
        right there.  The frames are the only thing vision can look at, and
        they go to the model in one request so it narrates the clip rather
        than three unrelated stills.

        When ``MEDIA_VIDEO_ASR_ENABLED`` is on, the clip's own speech is
        transcribed and sent in that *same* request.  A clip with no
        subtitles often only makes sense once you hear it, and two separate
        calls would cost twice the latency to produce two disconnected
        summaries — one of the picture, one of the sound — that the model
        then cannot line up.

        Every step degrades to None instead of raising: this runs inside the
        reply path, and a clip that will not download must not cost the
        answer.  Frames and speech are independent: either alone still
        produces a description.

        Args:
            video_urls: Candidate media addresses, best first (from
                :class:`~src.media.link_fetcher.Article` — they must come
                from the response that carried the page text).
            page_url: The page they were found on; used as referer.

        Returns:
            A short description of the clip, or None when there is no
            decoder, nothing downloads, or the model returns no text.
        """
        if not self._vision or not self._vision.available:
            return None
        try:
            # Imported lazily: the module locates ffmpeg at first use, and a
            # build without the binary must still work for text links.
            from .video import available, samples_from_urls
        except Exception:
            logger.debug("video module unavailable", exc_info=True)
            return None
        if not available():
            return None

        urls = [u for u in video_urls if u][:_MAX_VIDEO_SOURCES]
        if not urls:
            return None

        self._stats["video"] += 1
        samples = samples_from_urls(
            urls, page_url,
            want_audio=self._video_asr,
            audio_max_sec=self._audio_budget(),
            # Resolved only if the download turns out to have no sound at
            # all; Bilibili needs three API calls to produce these.
            audio_urls=lambda: self._audio_urls(page_url),
        )
        if not samples:
            self._stats["failed"] += 1
            logger.warning(
                "MediaPipeline: video unavailable for %s", page_url[:80],
            )
            return None

        try:
            if not samples.frames and not samples.audio:
                self._stats["failed"] += 1
                logger.warning(
                    "MediaPipeline: no frames and no audio for %s",
                    page_url[:80],
                )
                return None
            transcript = self._transcribe(samples.audio) if samples.audio else ""
            description = self._vision.describe_frames(
                samples.frames, transcript=transcript,
            )
        finally:
            # ``samples_from_urls`` hands over a per-call temp directory; the
            # caller owns it (see its note).
            shutil.rmtree(samples.workdir, ignore_errors=True)

        if description:
            self._stats["described"] += 1
            return description
        self._stats["failed"] += 1
        logger.warning(
            "MediaPipeline: vision gave no text for video "
            "(frames=%d, transcript=%d chars, model=%s): %s",
            len(samples.frames), len(transcript), self._vision_model(),
            self._vision_error(),
        )
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

    def _audio_budget(self) -> Optional[float]:
        """Seconds of a clip's sound worth extracting, or None for the default.

        The configured value wins when set; 0 (the default) means "whatever
        :mod:`src.media.video` budgets", so the two cannot drift apart.  Read
        per call rather than cached, so a config reload takes effect without
        a restart.
        """
        try:
            configured = float(
                getattr(self._config, "media_video_asr_max_sec", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            configured = 0.0
        return configured if configured > 0 else None

    def _audio_urls(self, page_url: str) -> list[str]:
        """Sound-track addresses for the clip on *page_url*, or [].

        Only Bilibili needs this: its DASH payload serves picture and sound as
        separate transfers, so the downloaded video has no audio at all.
        Everywhere else the sound rides inside the file and this is never
        asked — which is why it is a callable, resolved by
        :func:`~src.media.video.samples_from_urls` only after a silent track.
        """
        try:
            from .video import bilibili_audio_urls
        except Exception:
            logger.debug("video module unavailable", exc_info=True)
            return []
        try:
            return bilibili_audio_urls(page_url)
        except Exception:
            # Never let address resolution cost the description.
            logger.debug("Bilibili audio addresses failed", exc_info=True)
            return []

    def _transcribe(self, audio: Path) -> str:
        """What the clip says, or "" when it cannot be transcribed.

        Bounded by a deadline rather than left to run: someone is waiting for
        this reply.  A clip whose transcription overruns the budget falls back
        to the frame-only description the bot produced before this feature
        existed, which is a far better outcome than a reply that arrives
        minutes late.

        Every failure returns "": the caller then describes the picture alone.
        """
        asr = self._get_asr()
        if asr is None or not audio or not audio.exists():
            return ""

        language = getattr(self._config, "voice_asr_language", "zh")
        # "zh-en" / "auto" mean "let Whisper decide" on the voice path too.
        if language in ("zh-en", "auto"):
            language = None

        started = time.monotonic()
        # Manual pool management: a with-block would join the worker even
        # after the timeout fires, which is exactly the stall being avoided.
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="video-asr",
        )
        try:
            future = pool.submit(asr.transcribe_media, audio, language)
            result = future.result(timeout=_VIDEO_ASR_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "MediaPipeline: video transcription exceeded %.0fs — "
                "describing the picture only", _VIDEO_ASR_TIMEOUT_SEC,
            )
            pool.shutdown(wait=False)
            return ""
        except Exception as exc:
            logger.warning(
                "MediaPipeline: video transcription failed (%s: %s)",
                type(exc).__name__, exc,
            )
            pool.shutdown(wait=False)
            return ""
        pool.shutdown(wait=False)

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            logger.info("MediaPipeline: video had no recognisable speech")
            return ""
        self._stats["asr"] += 1
        logger.info(
            "MediaPipeline: video speech %d chars in %.1fs (confidence=%.2f)",
            len(text), time.monotonic() - started,
            getattr(result, "confidence", 0.0),
        )
        return text

    def _get_asr(self):
        """The transcriber, built on first use; None when unavailable.

        Building it loads the model, so it is deferred until a clip actually
        has sound.  A machine without ``faster_whisper`` installed (or with
        the model download blocked) gets None once and keeps it — the failure
        is not retried on every video.
        """
        if self._asr is not None:
            return self._asr or None
        try:
            from src.voice.asr import create_asr
        except Exception:
            logger.warning(
                "MediaPipeline: ASR unavailable — describing clips from "
                "their picture only", exc_info=True,
            )
            self._asr = False
            return None
        try:
            self._asr = create_asr(
                self._config,
                model=getattr(self._config, "media_video_asr_model", "") or "",
            )
        except Exception:
            logger.warning(
                "MediaPipeline: ASR construction failed — describing clips "
                "from their picture only", exc_info=True,
            )
            self._asr = False
            return None
        return self._asr

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
        # The chat id widens the address search: some chats receive a
        # stripped sticker body with no ``cdnurl`` at all.
        image = self._emoji.fetch(xml, chat_id or msg.get("chat_id") or "")
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
