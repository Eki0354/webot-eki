"""Resolve the per-machine keys needed to decrypt WeChat ``.dat`` images.

The keys are **not** in the message XML and cannot be derived from anything
the bot already reads: they are generated per WeChat installation.  So they
have to be supplied by the user, and this module is the single place that
decides where they come from.

Two providers, selected by ``IMAGE_KEY_PROVIDER``:

``env`` (default)
    Read ``IMAGE_XOR_KEY`` / ``IMAGE_AES_KEY`` from the config, i.e. ``.env``.

``file``
    Read ``data/image_keys.json``, so a third-party extractor's output can be
    dropped in without editing ``.env``::

        {"xor_key": "0x37", "aes_key": "491e17ca4a5f665d..."}

Keys already present in the config always win, whatever the provider — that
way an explicit ``.env`` entry is never silently overridden by a stale file.

A missing or malformed key is not an error: it resolves to ``None`` and the
caller degrades to the ``[图片]`` placeholder.
"""

import json
import logging
from pathlib import Path
from typing import NamedTuple, Optional

from .dat_decrypt import aes_key_from_text, parse_xor_key

logger = logging.getLogger(__name__)

#: Filename of the ``file`` provider's payload, relative to the project root.
KEY_FILE_NAME = "image_keys.json"

#: Accepted spellings in the JSON payload — extractors differ.
_XOR_KEYS = ("xor_key", "xorKey", "image_xor_key")
_AES_KEYS = ("aes_key", "aesKey", "image_aes_key")


class ImageKeys(NamedTuple):
    """A resolved pair of image keys; either side may be None."""

    xor_key: Optional[int]
    aes_key_text: str

    @property
    def usable(self) -> bool:
        """True when at least one key resolved, i.e. decrypting is worth trying."""
        return self.xor_key is not None or bool(self.aes_key_text)


def resolve_image_keys(config, data_dir: Optional[Path] = None) -> ImageKeys:
    """Return the image keys configured on *config*.

    Args:
        config: BotConfig instance.
        data_dir: Directory holding ``image_keys.json`` for the ``file``
            provider.  Defaults to ``PROJECT_ROOT / "data"``.

    Returns:
        An :class:`ImageKeys`; both fields None/empty when nothing is set.
    """
    xor_text = str(getattr(config, "image_xor_key", "") or "").strip()
    aes_text = str(getattr(config, "image_aes_key", "") or "").strip()

    provider = (getattr(config, "image_key_provider", "env") or "env").strip().lower()
    if provider == "file":
        file_xor, file_aes = _read_key_file(data_dir)
        # Config values win, so an explicit .env entry stays authoritative.
        xor_text = xor_text or file_xor
        aes_text = aes_text or file_aes
    elif provider != "env":
        logger.warning(
            "Unknown IMAGE_KEY_PROVIDER=%r — using env only", provider
        )

    xor_key = parse_xor_key(xor_text)
    if xor_text and xor_key is None:
        logger.warning("IMAGE_XOR_KEY %r is not a byte value — ignoring", xor_text)

    aes_key = aes_text if aes_key_from_text(aes_text) else ""
    if aes_text and not aes_key:
        logger.warning(
            "IMAGE_AES_KEY must be at least 16 characters — ignoring"
        )

    return ImageKeys(xor_key, aes_key)


def _read_key_file(data_dir: Optional[Path]):
    """Return ``(xor_text, aes_text)`` from ``image_keys.json``, or empty pair."""
    path = Path(data_dir) if data_dir else _default_data_dir()
    path = path / KEY_FILE_NAME
    if not path.exists():
        return "", ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Cannot read %s — image keys ignored", path, exc_info=True)
        return "", ""
    if not isinstance(payload, dict):
        logger.warning("%s must contain a JSON object — image keys ignored", path)
        return "", ""
    return _first(payload, _XOR_KEYS), _first(payload, _AES_KEYS)


def _first(payload: dict, names) -> str:
    """Return the first present, non-empty value among *names*."""
    for name in names:
        value = payload.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _default_data_dir() -> Path:
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT / "data"
