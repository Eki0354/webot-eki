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

#: ``<type>`` value for a shared link / article.  The quoted payload of a
#: reply uses the same numbering, so this doubles as the check for
#: "the quoted message is a link".
_APPMSG_LINK_TYPE = "5"

#: ``<type>`` value for an ordinary text message.  A member very often quotes
#: a bare link and asks about it, so this payload must be read too.
_APPMSG_TEXT_TYPE = 1

#: ``localType`` of an appmsg — a link, video, file or mini-program card.
#: A reply that quotes such a card reports *this* type in ``<refermsg>``, and
#: the card's own numbering (``5`` = link, ``4`` = video …) lives inside the
#: quoted ``<content>``.  Checking only the inner numbering made every quoted
#: card look unquotable: the payload was dropped and the question reached the
#: model with no title and no URL at all, so it could only answer that it saw
#: nothing.  Measured on a Xiaohongshu video share.
_APPMSG_LOCAL_TYPE = 49

#: Label used when the message quotes another message.
_QUOTE_LABEL = "[引用]"

#: Label for a quoted message's own text.  WeChat shows the quoted message
#: above a reply; without it the quote reduces to the *reply's* wording only,
#: and a quoted bare link — the "分析下这篇文章" case — arrives with no URL at
#: all, which is exactly what made the bot ask for a link it could not see.
_QUOTE_BODY_LABEL = "[原文]"

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_DES_RE = re.compile(r"<des>(.*?)</des>", re.S)
#: The article/file target.  ``<cdnattachurl>`` (files) does not match: the
#: pattern needs the literal tag ``<url>``.
_URL_RE = re.compile(r"<url>(.*?)</url>", re.S)
#: Used to read a quote's *own* wording without picking up the quoted
#: message's ``<title>``, which sits after it inside ``<refermsg>``.
_REFERMSG_STRIP_RE = re.compile(r"<refermsg>.*?</refermsg>", re.S)
_APPTYPE_RE = re.compile(r"<appmsg\b.*?<type>\s*(\d+)\s*</type>", re.S)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.S)
_REFERMSG_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
#: A bare URL inside quoted chat text.  Same boundary set as the fetcher's,
#: so a quoted "https://x.com/a，厉害" does not hand over the trailing comma.
_BARE_URL_RE = re.compile(
    r"https?://[^\s<>\"'`（）【】《》「」，。；：！？、\\^|]+", re.I
)
_REFER_TYPE_RE = re.compile(r"<type>\s*(\d+)\s*</type>")
_REFER_CONTENT_RE = re.compile(r"<content>(.*?)</content>", re.S)
_REFER_NAME_RE = re.compile(r"<displayname>(.*?)</displayname>", re.S)

# A body is sometimes stored as "<sender id>: <payload>".  Only strip what
# looks like a real WeChat id — a chat line such as "12:30 开会" must survive.
_SENDER_PREFIX_RE = re.compile(
    r"^(?:wxid_[A-Za-z0-9]+|\d+@chatroom|gh_[A-Za-z0-9]+):\s*"
)

_MAX_TEXT = 120
#: URLs are longer than titles and must not be truncated into a dead link.
_MAX_URL = 500
#: A quoted chat line is a sentence, not a title — keep more of it.
_MAX_QUOTED_TEXT = 200

#: Trailing punctuation belongs to the sentence, not the URL ("https://x/a，").
_TRAILING_JUNK = ".,;:!?)]}'\"…，。；：！？、）】」"


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


def appmsg_url(xml: str) -> str:
    """Return the ``<url>`` of an appmsg — the article/file target.

    The URL is the only thing a fetcher can act on, so it has to survive
    reduction.  ``reduce_appmsg`` used to keep the title and drop this, which
    left the model unable to even name the page it was asked about.

    The value is HTML-escaped in the stored XML — every query separator
    arrives as ``&amp;``.  Passing that through unchanged made the fetcher
    request a literal ``&amp;`` in the query, so a site that does not ignore
    it silently dropped every parameter after the first one (measured:
    ``?a=1&amp;b=2`` parses as ``{'a': '1', 'amp;b': '2'}``).  WeChat's own
    pages happened to tolerate it; other sites do not.
    """
    if not xml:
        return ""
    match = _URL_RE.search(xml)
    if not match:
        return ""
    return _clean(html.unescape(match.group(1)), _MAX_URL)


def _quoted_text_summary(content: str) -> str:
    """Summarise a quoted *plain-text* message, keeping any URL intact.

    The common case this exists for: someone pastes a bare link as a message,
    a second person quotes it and asks "分析下这篇".  The quoted payload has
    ``type=1`` and its ``<content>`` is the link itself — dropping it left the
    reply reading "引用里没带链接啊，把文章链接甩过来", because the model was
    telling the truth about the prompt it was given.

    Returns:
        ``"[原文] <文本> <URL…>"``, or "" when the payload carries nothing
        usable (empty, or markup that should have been a different type).
    """
    text = strip_sender_prefix(content or "").strip()
    if not text or looks_like_xml(text):
        return ""

    urls: list[str] = []
    for match in _BARE_URL_RE.finditer(text):
        url = match.group(0)
        while url and url[-1] in _TRAILING_JUNK:
            url = url[:-1]
        if url and url not in urls:
            urls.append(url)

    # Take the links out before collapsing whitespace so a long URL cannot
    # be cut in half by the text limit below.
    rest = _clean(_BARE_URL_RE.sub(" ", text), _MAX_QUOTED_TEXT)

    parts = [_QUOTE_BODY_LABEL]
    if rest:
        parts.append(rest)
    parts.extend(urls)
    return " ".join(parts) if len(parts) > 1 else ""


def _quoted_card_summary(content: str) -> str:
    """One-line summary of a quoted appmsg card (link, video, file …).

    The card's *own* ``<type>`` picks the label and its ``<title>`` / ``<url>``
    carry the content.  Both live inside the quoted payload, which may be a
    bare ``<appmsg>`` or wrapped in a whole ``<msg>`` block — the search is
    unanchored, so either shape works.

    Returns:
        ``"[链接] 标题 URL"`` (or the card's own label), or "" when the
        payload carries neither a title nor a URL.
    """
    if not content:
        return ""
    type_match = _APPTYPE_RE.search(content)
    label = _APPMSG_LABELS.get(
        type_match.group(1) if type_match else "", _DEFAULT_LABEL
    )

    title_match = _TITLE_RE.search(content)
    title = _clean(title_match.group(1) if title_match else "")
    url = appmsg_url(content)

    parts = [label]
    if title:
        parts.append(title)
    if url:
        parts.append(url)
    return " ".join(parts) if len(parts) > 1 else ""


def _quoted_summary(xml: str) -> str:
    """One-line summary of the message a reply quotes.

    Links, cards and plain text are all summarised: quoting a shared article
    card and quoting a pasted URL are the same question to the person asking.
    A quoted image is handled earlier by :meth:`MediaPipeline._describe_quoted`,
    and the remaining kinds carry nothing the model can use — inventing a
    label for them would be worse than saying nothing.

    A card is reported by the reply as ``<refermsg><type>49</type>`` (the
    appmsg envelope), not by the card's own numbering, which sits *inside*
    the quoted payload.  Accepting only the inner numbering is what made a
    quoted Xiaohongshu video share reduce to the sender's question alone —
    title and URL both dropped, so the model could only say it saw nothing.

    Returns:
        ``"[链接] 标题 URL"`` for a quoted card, ``"[原文] 文本 URL"`` for a
        quoted text message, or "" when the quoted message is neither.
    """
    refer = extract_refermsg(xml)
    if not refer:
        return ""
    # ``extract_refermsg`` reports ``type`` as an int; the label table is
    # keyed by the string form, so compare on ints rather than mixing them
    # (which silently never matched).
    kind = int(refer.get("type") or 0)
    content = refer.get("content") or ""

    if kind == _APPMSG_TEXT_TYPE:
        return _quoted_text_summary(content)

    if kind in (_APPMSG_LOCAL_TYPE, int(_APPMSG_LINK_TYPE)):
        return _quoted_card_summary(content)

    return ""


def reduce_appmsg(xml: str) -> Optional[str]:
    """Return a one-line label for an appmsg/sysmsg payload, or None.

    Args:
        xml: The decoded message body.

    Returns:
        ``"[链接] 标题 URL"`` for appmsg payloads (URL only when present),
        ``"[引用] <用户的话> [链接] <被引用的标题 URL>"`` for a reply,
        None for system notices and anything unrecognisable.
    """
    if not xml:
        return None

    if "<sysmsg" in xml:
        # Join / leave / revoke notices — handled by the backend's filters.
        return None

    if _REFERMSG_RE.search(xml):
        # A quote.  Its own <type> is 57, which the table calls "[直播]";
        # saying "直播" about a reply is worse than useless — the model reads
        # it as the message's whole content.  The quoted payload sits after
        # the sender's wording, so it must be stripped before reading the
        # *reply's* title, otherwise the quoted article's <title> would be
        # mistaken for the question.
        own_part = _REFERMSG_STRIP_RE.sub("", xml)
        own_match = _TITLE_RE.search(own_part)
        own_title = _clean(own_match.group(1) if own_match else "")
        quoted = _quoted_summary(xml)
        if own_title and quoted:
            return f"{_QUOTE_LABEL} {own_title} {quoted}"
        if own_title:
            return f"{_QUOTE_LABEL} {own_title}"
        return f"{_QUOTE_LABEL} {quoted}" if quoted else None

    type_match = _APPTYPE_RE.search(xml)
    app_type = type_match.group(1) if type_match else ""
    label = _APPMSG_LABELS.get(app_type, _DEFAULT_LABEL)

    title_match = _TITLE_RE.search(xml)
    title = _clean(title_match.group(1) if title_match else "")

    parts = [label]
    if title:
        parts.append(title)
    else:
        des_match = _DES_RE.search(xml)
        des = _clean(des_match.group(1) if des_match else "")
        if des:
            parts.append(des)

    url = appmsg_url(xml)
    if url:
        parts.append(url)

    # No title, description or URL — a bare label beats dumping XML.
    if len(parts) > 1 or app_type:
        return " ".join(parts)
    return None
