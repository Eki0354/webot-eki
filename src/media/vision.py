"""Describe an image file with the configured AI backend.

Uses the same OpenAI-compatible client as summarization, so whichever
backend the user configured (DeepSeek / OpenAI / any compatible provider)
serves vision too — no second set of credentials to manage.

Measured on real data, an empty description is almost never the model's
fault: a reasoning model spends ``max_tokens`` on reasoning tokens *first*,
so a 500-token budget produces ``finish_reason="length"`` with empty
``content``.  Behind one OpenAI-compatible proxy that hit 19 of 22 cached
images, while the same images with reasoning switched off answered every
time.

So :class:`VisionClient` negotiates *how* to ask instead of assuming.  It
starts with the cheapest request, escalates when the reply is truncated
(reasoning switched off, then a large budget), and remembers whichever
shape worked so later images pay no extra round trip.  Endpoints that
reject the ``thinking`` field outright (plain OpenAI) are detected from the
error and handled without it.
"""

import base64
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Budget for a reply that only has to carry the answer.
_MAX_TOKENS = 500

#: Budget when the endpoint spends tokens on reasoning first.  Measured on
#: a real image: 379 reasoning + 443 total on such a model, so 500 truncates
#: while 4000 leaves comfortable headroom.
_MAX_TOKENS_REASONING = 4000

# Refuse to inline huge files: base64 inflates by 4/3 and the request is
# rejected upstream long before this helps.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Magic bytes → MIME type for the data URL.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)

PROMPT = (
    "这是微信群聊里发的一张表情/图片。用一句简短的中文描述它表达了什么，"
    "像跟朋友说话那样，不要罗列元素，不要加引号。"
)

#: Prompt for a shared video, described from frames sampled across the clip.
#: The frames are the *only* thing the model sees, so it must be told they
#: are ordered stills of one clip — otherwise it narrates three unrelated
#: pictures ("第一张……第二张……") instead of what the video shows.
FRAME_PROMPT = (
    "这是群聊里分享的一段短视频，按时间顺序抽了几帧。"
    "用两三句中文说说这段视频大概拍了什么、在讲什么事，"
    "像跟朋友转述那样，不要把每帧分开描述，不要罗列元素，不要加引号。"
)

#: Error text meaning "this endpoint does not accept the ``thinking`` field".
#: Matching stays loose: providers word it differently ("unknown parameter",
#: "extra fields not permitted", "unrecognized request argument"), but all of
#: them name the field or the word parameter.
_THINKING_REJECT_MARKERS = (
    "thinking", "unrecognized", "unknown field", "unknown parameter",
    "extra fields", "unexpected keyword", "invalid_request_error",
)

#: Empty content with this finish reason means the budget ran out before any
#: text appeared — the signature of reasoning eating ``max_tokens``.
_TRUNCATED = "length"


def detect_mime(data: bytes) -> Optional[str]:
    """Guess the image MIME type from its leading bytes."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    return None


def to_data_url(path: Path) -> Optional[str]:
    """Read *path* and return a ``data:`` URL, or None if not an image."""
    try:
        data = path.read_bytes()
    except OSError:
        logger.debug("Cannot read image %s", path, exc_info=True)
        return None
    if not data or len(data) > _MAX_IMAGE_BYTES:
        logger.debug("Image rejected (%s, %d bytes)", path, len(data))
        return None
    mime = detect_mime(data)
    if not mime:
        logger.debug("Not a recognised image: %s", path)
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


class VisionClient:
    """Minimal image → description client over an OpenAI-compatible API.

    The request shape is negotiated on the first images and then reused.
    ``last_error`` explains the most recent failure in human terms, so a
    caller can log *why* a picture came back undescribed instead of leaving
    the empty result unexplained.
    """

    def __init__(self, api_key: str, model: str,
                 base_url: str = "", disable_thinking: bool = False) -> None:
        """
        Args:
            api_key: Endpoint credential.
            model:   Vision-capable model name.
            base_url: Endpoint root; empty uses the client's default.
            disable_thinking: Ask for reasoning to be off up front.  Backends
                known to reason by default (DeepSeek) pass True; others are
                negotiated after the first truncated reply.
        """
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        # Whether this endpoint accepts ``thinking: {type: disabled}``.  It is
        # cleared for good once one request is rejected.
        self._thinking_supported = True
        # Whether reasoning is currently switched off for this endpoint.
        self._thinking_disabled = bool(disable_thinking)
        self._client = None
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._model)

    @property
    def model(self) -> str:
        """The model name, for log lines that must say what was asked."""
        return self._model

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            kwargs = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def describe(self, image_path: Path, prompt: str = PROMPT) -> Optional[str]:
        """Return a one-line description of *image_path*, or None.

        Tries the cheapest request first and escalates only when the endpoint
        proves it needs it, so an ordinary backend never pays for a retry.
        """
        if not self.available:
            self.last_error = "no api key or model configured"
            return None
        data_url = to_data_url(image_path)
        if not data_url:
            self.last_error = f"not a readable image: {image_path.name}"
            return None
        return self._describe([data_url], prompt)

    def describe_frames(
        self, frames: list[Path], prompt: str = FRAME_PROMPT
    ) -> Optional[str]:
        """Describe a clip from frames sampled across it, or None.

        The frames go in *one* request: the model has to see them together to
        say what happens over the clip, and three separate calls would cost
        three times as much to produce three disjoint captions.

        Args:
            frames: Ordered stills, first to last.
            prompt: Wording that tells the model these are one clip.

        Returns:
            A short description, or None when there is no client or the
            frames cannot be read.
        """
        data_urls = [u for u in (to_data_url(f) for f in frames) if u]
        if not data_urls:
            self.last_error = "no readable frame"
            return None
        return self._describe(data_urls, prompt)

    def _describe(self, data_urls: list[str], prompt: str) -> Optional[str]:
        """Ask about one or more images, negotiating the request shape."""
        if not self.available:
            self.last_error = "no api key or model configured"
            return None
        if not data_urls:
            self.last_error = "no image to describe"
            return None

        self.last_error = ""
        for max_tokens, disable_thinking in self._attempts():
            text, error = self._ask(data_urls, prompt, max_tokens,
                                    disable_thinking)
            if text:
                self._remember(disable_thinking)
                return text
            self.last_error = error
            if error == "truncated":
                # Reasoning is on for this endpoint; skip straight to the
                # shape that leaves room for text next time.
                if self._thinking_supported:
                    self._thinking_disabled = True
                continue
            if error == "rejected-thinking":
                continue
            # A real failure (auth, network, unusable image): another budget
            # will not fix the same picture.
            break
        return None

    # -- Negotiation -------------------------------------------------------

    def _attempts(self) -> list[tuple[int, bool]]:
        """Request shapes to try, cheapest and most likely first."""
        attempts: list[tuple[int, bool]] = []
        if self._thinking_supported and self._thinking_disabled:
            attempts.append((_MAX_TOKENS, True))
        attempts.append((_MAX_TOKENS, False))
        if self._thinking_supported and not self._thinking_disabled:
            attempts.append((_MAX_TOKENS, True))
        # Last resort: leave reasoning on and pay for it.
        attempts.append((_MAX_TOKENS_REASONING, False))
        return attempts

    def _remember(self, disable_thinking: bool) -> None:
        """Cache the shape that worked so later images start there."""
        if disable_thinking and self._thinking_supported:
            self._thinking_disabled = True

    def _ask(self, data_urls: list[str], prompt: str, max_tokens: int,
             disable_thinking: bool) -> tuple[Optional[str], str]:
        """One request.  Returns ``(text, error)``; error is "" on success.

        *data_urls* is a list so a video's frames travel in a single request:
        several ``image_url`` parts with one prompt is the shape every
        multimodal endpoint accepts, and the model needs them side by side to
        describe what happens across the clip.
        """
        if disable_thinking and not self._thinking_supported:
            return None, "rejected-thinking"

        extra_body = (
            {"thinking": {"type": "disabled"}} if disable_thinking else None
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": url}}
            for url in data_urls
        )
        try:
            response = self._get_client().chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
                extra_body=extra_body,
            )
        except Exception as exc:
            message = str(exc).lower()
            if disable_thinking and any(
                marker in message for marker in _THINKING_REJECT_MARKERS
            ):
                # The endpoint does not know the field at all: forget it for
                # the rest of the run, or every image fails the same way.
                logger.info(
                    "Vision: endpoint rejects 'thinking' (model=%s) — "
                    "reasoning stays on, using a larger budget",
                    self._model,
                )
                self._thinking_supported = False
                return None, "rejected-thinking"
            logger.debug("Vision request failed", exc_info=True)
            return None, f"request failed: {type(exc).__name__}: {exc}"

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if text:
            return text, ""

        finish = getattr(choice, "finish_reason", "") or ""
        if finish == _TRUNCATED:
            # Only reasoning tokens came back; ask again in a shape that
            # leaves room for actual text.
            return None, "truncated"
        return None, f"empty content (finish_reason={finish or '?'})"
