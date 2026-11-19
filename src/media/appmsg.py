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

import html
import re
from typing import Optional

# <appmsg> type → human label.  Unknown types fall back to a generic label.
# ``57`` is the 4.x *reply* envelope, not a live share: a message that
# quotes another one carries its original inside ``<refermsg>``.  It is
# labelled separately below, because ``type`` alone would call it "[直播]".
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

#: Label used when the message quotes another message.
_QUOTE_LABEL = "[引用]"

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_DES_RE = re.compile(r"<des>(.*?)</des>", re.S)
_APPTYPE_RE = re.compile(r"<appmsg\b.*?<type>\s*(\d+)\s*</type>", re.S)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.S)
_REFERMSG_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
_REFER_TYPE_RE = re.compile(r"<type>\s*(\d+)\s*</type>")
_REFER_CONTENT_RE = re.compile(r"<content>(.*?)</content>", re.S)
_REFER_NAME_RE = re.compile(r"<displayname>(.*?)</displayname>", re.S)

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


def extract_refermsg(xml: str) -> Optional[dict]:
    """Return the message this one quotes, or None when it quotes nothing.

    WeChat 4.x sends a reply as an ``<appmsg>`` whose ``<refermsg>`` block
    holds the original.  Its ``<content>`` is the *same payload* the quoted
    message carried — for an image that means the full ``<img>`` tag with
    ``md5`` / ``hevc_mid_size`` / ``cdnthumblength``, which is exactly what
    the image store needs.  Nothing else in the envelope exposes it.

    Returns:
        ``{"type": int, "content": str, "displayname": str}``, or None when
        the body has no ``<refermsg>``.  ``content`` is HTML-unescaped: the
        quoted payload is stored escaped (``&lt;msg&gt;``), and leaving it
        escaped would make it unrecognisable to the XML readers downstream.
    """
    if not xml:
        return None
    block = _REFERMSG_RE.search(xml)
    if not block:
        return None

    inner = block.group(1)
    type_match = _REFER_TYPE_RE.search(inner)
    content_match = _REFER_CONTENT_RE.search(inner)
    name_match = _REFER_NAME_RE.search(inner)
    return {
        "type": int(type_match.group(1)) if type_match else 0,
        "content": html.unescape(content_match.group(1))
        if content_match else "",
        "displayname": _clean(name_match.group(1)) if name_match else "",
    }


def appmsg_title(xml: str) -> str:
    """Return the ``<title>`` of an appmsg — the sender's own wording.

    For a quote this is the question the user typed alongside it, which is
    the part the model must answer; the quoted payload is separate.
    """
    if not xml:
        return ""
    match = _TITLE_RE.search(xml)
    return _clean(match.group(1)) if match else ""


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
    if _REFERMSG_RE.search(xml):
        # A quote.  Its <type> is 57, which the table calls "[直播]"; saying
        # "直播" about a reply is worse than useless — the model reads it as
        # the message's whole content.
        label = _QUOTE_LABEL
    else:
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
