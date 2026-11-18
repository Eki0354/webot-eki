"""Media message module — binary bodies, emoji images, vision descriptions.

WeChat 4.x stores non-text bodies as zstd-compressed BLOBs, which the native
layer emits base64-encoded.  This package turns them back into something the
bot can reason about:

    from src.media import MediaPipeline
    pipeline = MediaPipeline(config)
    text = pipeline.process(msg)   # "[表情] 猫在摇头", "[链接] 标题", or None

``None`` means "nothing useful extracted" — the caller should fall back to a
plain placeholder such as ``[图片]``.
"""

from .pipeline import MediaPipeline
from .blob import decode_blob, is_binary
from .emoji import EmojiStore, parse_emoji_xml
from .vision import VisionClient, to_data_url, detect_mime

__all__ = [
    "MediaPipeline",
    "decode_blob",
    "is_binary",
    "EmojiStore",
    "parse_emoji_xml",
    "VisionClient",
    "to_data_url",
    "detect_mime",
]
