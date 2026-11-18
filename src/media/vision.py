"""Describe an image file with the configured AI backend.

Uses the same OpenAI-compatible client as summarization, so whichever
backend the user configured (DeepSeek / OpenAI / any compatible provider)
serves vision too — no second set of credentials to manage.

Measured on DeepSeek's endpoint: ``deepseek-flash`` accepts an
``image_url`` data URL and describes it correctly, but spends part of
``max_tokens`` on reasoning tokens, so a small budget yields an empty
``content``.  ``_MAX_TOKENS`` therefore stays generous and an empty reply
is treated as failure (the caller falls back to a plain placeholder).
"""

import base64
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_TOKENS = 500

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
    """Minimal image → description client over an OpenAI-compatible API."""

    def __init__(self, api_key: str, model: str,
                 base_url: str = "", disable_thinking: bool = False) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._disable_thinking = disable_thinking
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._model)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            kwargs = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def describe(self, image_path: Path, prompt: str = PROMPT) -> Optional[str]:
        """Return a one-line description of *image_path*, or None."""
        if not self.available:
            return None
        data_url = to_data_url(image_path)
        if not data_url:
            return None

        try:
            response = self._get_client().chat.completions.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                extra_body=(
                    {"thinking": {"type": "disabled"}}
                    if self._disable_thinking else None
                ),
            )
        except Exception:
            logger.debug("Vision request failed", exc_info=True)
            return None

        text = (response.choices[0].message.content or "").strip()
        if not text:
            # DeepSeek spends max_tokens on reasoning first; an empty body
            # here means the budget ran out, not that the image is empty.
            logger.debug("Vision returned no text (model=%s)", self._model)
            return None
        return text
