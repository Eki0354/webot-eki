"""Decode WeChat 4.x binary message bodies.

WeChat 4.x stores non-text bodies (image, emoji, appmsg, system notice) as
BLOB columns rather than text.  The native DLL emits them base64-encoded
(``content_kind == "blob"``), and the payload is a zstd-compressed XML
document (or, for some types, raw bytes).

Anything that fails to decode must fall back to a placeholder — a binary
body is never message text, and passing base64 into an AI prompt is worse
than saying "[图片]".
"""

import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Guard against a malformed length prefix making zstd allocate wildly.
_MAX_DECODED_BYTES = 4 * 1024 * 1024


def is_binary(msg: dict) -> bool:
    """True when *msg* carries a BLOB body rather than text."""
    return msg.get("content_kind") == "blob"


def decode_blob(raw: str) -> Optional[str]:
    """Decode a base64+zstd message body into its XML text.

    Args:
        raw: The base64 string the native layer produced.

    Returns:
        The decoded UTF-8 text, or None when *raw* is not a zstd payload
        (some types store plain base64) or decoding fails.
    """
    if not raw:
        return None

    try:
        packed = base64.b64decode(raw, validate=False)
    except Exception:
        logger.debug("Blob is not valid base64", exc_info=True)
        return None

    if not packed:
        return None

    try:
        import zstandard
    except ImportError:  # pragma: no cover - dependency is in requirements
        logger.warning("zstandard not installed — binary bodies stay opaque")
        return None

    try:
        text = zstandard.ZstdDecompressor().decompress(
            packed, max_output_size=_MAX_DECODED_BYTES
        )
    except Exception:
        # Not compressed — try the bytes as text directly.
        logger.debug("Blob is not zstd-compressed", exc_info=True)
        try:
            return packed.decode("utf-8")
        except UnicodeDecodeError:
            return None

    return text.decode("utf-8", "replace")
