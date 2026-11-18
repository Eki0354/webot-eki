"""Reduce WeChat appmsg / sysmsg XML to one line of text.

WeChat packs links, files, mini-programs, live shares and "拍一拍" into a
single ``<appmsg>`` envelope.  Dumping that XML into an AI prompt is useless
(it is mostly metadata), so it is reduced to ``"[链接] 标题"``.

System notices (``<sysmsg>``) are dropped: they are join/leave/revoke events,
which the backend's own filters handle.  Returning None here restores the
behaviour from before BLOB columns were emitted — those messages were simply
invisible.

This module is pure text processing: no network, no config, no vision.
"""

import re
from typing import Optional

# <appmsg> type → human label.  Unknown types fall back to a generic label.
_APPMSG_LABELS = {
    "3": "[音乐]",
    "4": "[视频]",
    "5": "[链接]",
    "6": "[文件]",
    "8": "[表情]",
    "19": "[聊天记录]",
    "33": "[小程序]",
    "36": "[小程序]",
    "57": "[直播]",
    "62": "[拍一拍]",
}

_DEFAULT_LABEL = "[链接]"

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_DES_RE = re.compile(r"<des>(.*?)</des>", re.S)
_APPTYPE_RE = re.compile(r"<appmsg\b.*?<type>\s*(\d+)\s*</type>", re.S)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.S)

# A body is sometimes stored as "<sender id>: <payload>".  Only strip what
# looks like a real WeChat id — a chat line such as "12:30 开会" must survive.
_SENDER_PREFIX_RE = re.compile(
    r"^(?:wxid_[A-Za-z0-9]+|\d+@chatroom|gh_[A-Za-z0-9]+):\s*"
)

_MAX_TEXT = 120


def strip_sender_prefix(text: str) -> str:
    """Remove a leading ``<wxid>:`` from a stored message body."""
    return _SENDER_PREFIX_RE.sub("", text, count=1)


def _clean(text: Optional[str], limit: int = _MAX_TEXT) -> str:
    """Strip CDATA, collapse whitespace, truncate."""
    if not text:
        return ""
    match = _CDATA_RE.match(text.strip())
    if match:
        text = match.group(1)
    return " ".join(text.split())[:limit]


def looks_like_xml(text: str) -> bool:
    """True when *text* is markup rather than chat."""
    return text.lstrip().startswith("<")


def reduce_appmsg(xml: str) -> Optional[str]:
    """Return a one-line label for an appmsg/sysmsg payload, or None.

    Args:
        xml: The decoded message body.

    Returns:
        ``"[链接] 标题"`` for appmsg payloads, None for system notices and
        anything unrecognisable.
    """
    if not xml:
        return None

    if "<sysmsg" in xml:
        # Join / leave / revoke notices — handled by the backend's filters.
        return None

    type_match = _APPTYPE_RE.search(xml)
    app_type = type_match.group(1) if type_match else ""
    label = _APPMSG_LABELS.get(app_type, _DEFAULT_LABEL)

    title_match = _TITLE_RE.search(xml)
    title = _clean(title_match.group(1) if title_match else "")
    if title:
        return f"{label} {title}"

    des_match = _DES_RE.search(xml)
    des = _clean(des_match.group(1) if des_match else "")
    if des:
        return f"{label} {des}"

    # No title or description — a bare label beats dumping XML.
    return label if app_type else None
