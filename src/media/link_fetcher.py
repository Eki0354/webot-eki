"""Fetch a shared link and reduce the page to text for the AI prompt.

Why this exists: the bot is handed ``https://mp.weixin.qq.com/s/xxx`` and the
model is asked "这文章讲了啥？".  A chat completion has **no** web access — the
URL is just characters in the prompt, so without this module the model can
only repeat the link or guess from the title.  Measured behaviour: asked to
read a link, it answered "我打不开链接" every time.

Design constraints, all of them load-bearing:

* **Only public HTTP(S).**  The URL comes from a group chat, so anyone in the
  group can make the server issue a request.  ``is_public_url`` rejects
  non-HTTP schemes, odd ports, credentials in the URL, and any host that
  resolves to a private / loopback / link-local / reserved address — the
  classic SSRF probe ``http://192.168.1.1/admin`` never leaves the process.
  Every redirect hop is re-validated, because a public URL may 302 to
  ``http://127.0.0.1``.
* **Bounded work.**  A fixed timeout, a byte cap, and only textual content
  types: a 4 GB video or an endless stream must not stall the reply path.
* **No new dependencies.**  urllib + the stdlib HTML parser only; this
  project ships as a single EXE and its dependency list is already heavy.

Known limitation, stated rather than hidden: validation resolves the host and
then urllib resolves it again to connect, so a DNS-rebinding attacker could
serve a private address on the second lookup.  Closing that needs the socket
pinned to the vetted IP (custom ``HTTPSConnection``), which buys little here —
the attacker would have to control the domain *and* win a race, and they can
only read pages they already control.  The cheap probes above are what matter.

This module does network I/O but no AI calls and no config reading.
"""

import gzip
import html
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Optional

logger = logging.getLogger(__name__)

#: An article page, not a download.  WeChat's own pages are ~200-600 KB.
_MAX_BYTES = 2 * 1024 * 1024

#: Blocking call — the caller sits in the reply path, so stay well under the
#: time a group chat will wait for an answer.
_TIMEOUT_SEC = 6

_MAX_REDIRECTS = 3
_MAX_URL_LEN = 2048

#: Characters of body text handed to the model.  Enough for "what is this
#: about", short enough not to crowd out the conversation it must answer in.
_MAX_TEXT = 1800

#: Titles are one line; longer means we picked up the wrong tag.
_MAX_TITLE = 120

#: Floor on a usable body.  A page that yields less than this is a footer, a
#: redirect stub or a "open this in the app" notice, not an article — measured
#: on real shares: a Bilibili short link yields 14 characters ("*以选手发布
#: 直播预约时间为准").  Handing that to the model as "你已经读过了" makes it
#: answer from page furniture, which is worse than admitting the page could not
#: be read, so the fetch is reported as a failure instead.
_MIN_USABLE_TEXT = 80

#: Phrases a site serves *instead of* the content when it refuses a scripted
#: request.  Measured: WeChat article pages answer every User-Agent tried
#: (including the real WeChat clients') with "环境异常，完成验证后即可继续
#: 访问"; Xiaohongshu titles the page "你访问的页面不见了".  Both used to be
#: reported as successful reads, so the prompt said the article had been read
#: and the model summarised a verification wall.
#:
#: Checked against title *and* body: Xiaohongshu's marker sits in the title,
#: while WeChat's sits in the body.
_BLOCKED_MARKERS = (
    "环境异常",
    "完成验证",
    "安全验证",
    "滑动验证",
    "访问的页面不见了",
    "该内容已被发布者删除",
    "请在微信客户端打开",
    "参数错误",
    "access denied",
    "403 forbidden",
    "checking your browser",
    "enable javascript",
    "just a moment",
    "verify you are human",
    "unusual traffic",
)

_CACHE_TTL_SEC = 600
_CACHE_MAX = 64

_ALLOWED_SCHEMES = ("http", "https")
#: An explicit odd port is a probe (or a service), not a public article.
_ALLOWED_PORTS = (None, 80, 443)

#: Hosts that are private by name, no DNS lookup needed.
_PRIVATE_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal",
})
_PRIVATE_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa", ".lan")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: Second User-Agent tried when the desktop page comes back as an empty shell.
#: Measured on a Xiaohongshu video share: the desktop UA gets a 37 KB
#: JavaScript shell — no text, no ``og:title``, no ``og:description`` — while
#: the iPhone UA gets 71 KB carrying the note's title and its whole copy in
#: ``og:description``.  The site decides by UA, so asking as a phone is the
#: only way to read it without a browser engine.
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

#: Timeout for the mobile retry.  It only runs when the first request already
#: answered quickly with a shell, so this is a small extra cost — kept short
#: anyway so one page cannot eat the whole per-question fetch budget.
_MOBILE_TIMEOUT_SEC = 3

#: Elements whose *text* is noise (code, styling) or not prose at all.
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "iframe", "template", "canvas",
    "video", "audio", "object", "embed", "form", "button", "select", "textarea",
})

#: Elements that carry no end tag, so depth bookkeeping must skip them.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

#: Block-level boundaries — text either side belongs to different lines.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
})

#: WeChat puts the article body here; other sites get the generic path.
_MAIN_ID = "js_content"

#: ``<meta charset>`` / ``charset=`` in a ``<meta>`` tag.
_META_CHARSET_RE = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I
)

#: A URL inside chat text.  Stops at whitespace, markup, quotes, and CJK
#: punctuation — a group member typing "看这个 https://x.com/a，厉害了" must not
#: hand us the trailing "，厉害了" as part of the path.
_URL_IN_TEXT_RE = re.compile(
    r"https?://[^\s<>\"'`（）【】《》「」，。；：！？、\\^|]+", re.I
)

#: Trailing punctuation belongs to the sentence, not the URL.  Applied
#: repeatedly so "https://x.com/a)." loses both.
_TRAILING_JUNK = ".,;:!?)]}'\"…"

#: A single JSON string value, e.g. ``"desc":"一段关系…"``.  Chinese text is
#: carried escaped (``\n``, ``\u002F``), so the value is decoded afterwards
#: rather than used raw.  The value may contain any escape, hence the alternation.
_JSON_VALUE_PATTERN = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"'

#: Keys that hold a page's prose in an embedded JSON state.  ``desc`` is what
#: Xiaohongshu calls a note's copy; ``description``/``content`` cover the
#: common alternatives.  Ordered by how likely each is to be the real text.
_JSON_BODY_KEYS = ("desc", "description", "content", "text")

#: Keys that hold a page's headline in an embedded JSON state.  Used only
#: when the markup itself carried no usable title.
_JSON_TITLE_KEYS = ("title", "noteTitle", "headline")

#: How many links one question may pull.  Each costs a blocking fetch, and a
#: message with five links is a link dump, not a question about one article.
_MAX_LINKS_PER_QUESTION = 2

#: Total wall-clock budget for a question's fetches.  ``_TIMEOUT_SEC`` is per
#: request; without this, two dead links would block the reply for 12s.
_FETCH_BUDGET_SEC = 8

#: Wall clock the headless renderer may take.  A script-built page plus a cold
#: browser start is slow by nature (measured 3-6s on a warm Edge), so this is
#: deliberately larger than ``_TIMEOUT_SEC`` — but it only runs once the cheap
#: static attempts have already come back empty.
_RENDER_TIMEOUT_SEC = 12.0

#: Least remaining budget worth starting a browser with.  A render that is
#: killed halfway returns nothing, so the gate cannot be "the full timeout
#: still fits" — that was the bug: the batch budget (8s) was smaller than the
#: render timeout (12s), so ``left >= render_timeout`` was never true and the
#: renderer never ran on the live path, in any configuration.  What actually
#: matters is whether the render can finish: the timeout handed down is
#: clamped to what is left, and below this floor even a clamped run cannot
#: paint a page, so the browser is not started at all.
_MIN_RENDER_BUDGET_SEC = 3.0

#: Words that mean the asker expects *moving pictures* from the link.  Only a
#: question carrying one of these may spend a browser start on a page whose
#: static HTML already had readable prose: the static read is the cheap path
#: and covers every article, while a clip's address is increasingly built by
#: script alone (measured: a page that sets ``<video src>`` from JavaScript
#: and injects ``masterUrl`` into a JSON blob 400 ms after load — the static
#: bytes contain neither).  English terms are matched case-insensitively.
_VIDEO_INTENT_RE = re.compile(
    "|".join(
        (
            "视频", "短片", "片段", "影片", "片子", "录像", "录屏", "镜头",
            "抽帧", "画面", "动画",
            r"\bvlog\b", r"\bvideo\b", r"\bclip\b", r"\bmp4\b", r"\bmovie\b",
        )
    ),
    re.IGNORECASE,
)


def wants_video(text: str) -> bool:
    """True when *text* asks about a clip rather than an article.

    Deterministic and cheap: this decides whether a readable page may cost one
    extra browser start to find a media address its static HTML did not carry.
    Plain article questions ("这篇讲了啥") stay on the static path entirely.
    """
    return bool(text) and bool(_VIDEO_INTENT_RE.search(text))


@dataclass(frozen=True)
class Article:
    """What a fetched page gave us: where, what it is called, what it says.

    ``video_urls`` carries the media addresses found in *this same response*.
    It has to be captured here rather than re-derived later: measured on a
    Xiaohongshu video note, a second request for the same page answers with a
    degraded 23 KB shell that no longer contains ``masterUrl``, so hunting
    for the video after the fact silently loses every clip.
    """

    url: str
    title: str
    text: str
    video_urls: tuple[str, ...] = ()

    def to_prompt_block(self, max_text: int = _MAX_TEXT) -> str:
        """Render as the block inserted into the prompt.

        The leading tag is what makes the model treat this as *content it has
        already read* rather than a link it must open — the chat prompt
        documents ``[链接正文]`` for exactly that reason.
        """
        head = f"[链接正文] {self.title or '（无标题）'} — {self.url}"
        body = self.text[:max_text].strip()
        return f"{head}\n{body}" if body else head


# ── URL vetting ──────────────────────────────────────────────────────


def _is_public_ip(addr: str) -> bool:
    """True when *addr* is a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _host_is_public(host: str, *, resolve: bool) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    if host in _PRIVATE_HOSTS or host.endswith(_PRIVATE_SUFFIXES):
        return False

    # A literal address needs no lookup — and must be judged as an address.
    try:
        ipaddress.ip_address(host)
        return _is_public_ip(host)
    except ValueError:
        pass

    # A single-label name ("intranet", "router") is never a public article.
    if "." not in host:
        return False
    if not resolve:
        return True

    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError) as exc:
        # WARNING, not DEBUG: when fetching yields nothing the only clue left
        # is this line, and the reply then says "我打不开链接" with no way to
        # tell why.  The bot logs at INFO, so DEBUG is invisible in practice.
        logger.warning("Link skipped: cannot resolve %s (%s)", host, exc)
        return False
    if not infos:
        logger.warning("Link skipped: %s resolved to no address", host)
        return False
    # Every address must be public: a host that resolves to both 1.2.3.4 and
    # 127.0.0.1 would otherwise be able to reach the loopback on a retry.
    for info in infos:
        if not _is_public_ip(info[4][0]):
            logger.warning("Link skipped: %s resolves to %s", host, info[4][0])
            return False
    return True


def is_public_url(url: str, *, resolve: bool = True) -> bool:
    """True when *url* is an HTTP(S) URL on a public host.

    Args:
        url: Candidate link.
        resolve: Also resolve DNS and require every address to be public.
            Pass False in tests (and when only the URL's shape matters).
    """
    if not url or len(url) > _MAX_URL_LEN:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    # ``http://user:pass@host/`` is a credential-phishing shape; refuse it.
    if parts.username or parts.password:
        return False
    if port not in _ALLOWED_PORTS:
        return False
    if not parts.hostname:
        return False
    return _host_is_public(parts.hostname, resolve=resolve)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop instead of trusting the first URL."""

    max_repeats = _MAX_REDIRECTS

    def __init__(self) -> None:
        super().__init__()
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > _MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                newurl, code, "too many redirects", headers, fp
            )
        if not is_public_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, f"redirect to non-public URL: {newurl}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ── Bytes → text ─────────────────────────────────────────────────────


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Undo gzip/deflate; return b"" when the body cannot be decoded."""
    encoding = (encoding or "").lower().strip()
    if not raw:
        return b""
    try:
        if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # Servers routinely send raw deflate despite the header.
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        logger.debug("Link body decompression failed (%s)", encoding)
        return b""
    return raw


def _decode(raw: bytes, charset: str) -> str:
    """Decode with the declared charset, then UTF-8, then GB18030."""
    for encoding in (charset, "utf-8", "gb18030"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _is_textual(content_type: str) -> bool:
    """Only HTML and plain text are articles; anything else is a download."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not ctype:
        # Some sites omit it; accept and let extraction decide.
        return True
    return ctype.startswith("text/") or ctype in (
        "application/xhtml+xml", "application/xml",
    )


# ── HTML → text ──────────────────────────────────────────────────────


class _Extractor(HTMLParser):
    """Collect a page's title and prose, preferring the article container.

    Two buffers are kept: ``main`` holds text inside ``#js_content`` (WeChat's
    article body), ``body`` everything else.  The caller uses ``main`` when it
    is non-empty, so a site that marks its article needs no per-site rules,
    and one that does not still yields a usable excerpt.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.og_description = ""
        self._in_title = False
        self._skip_depth = 0
        self._main_depth = 0
        self._pending: list[str] = []
        self._main: list[str] = []
        self._body: list[str] = []

    # -- HTMLParser hooks ---------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)

        # Depth bookkeeping first, so skip/main nesting stays balanced.
        if self._main_depth:
            if tag not in _VOID_TAGS:
                self._main_depth += 1
        elif tag == "div" and (attrs_d.get("id") or "") == _MAIN_ID:
            self._main_depth = 1

        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return

        if tag == "meta":
            key = (
                attrs_d.get("property") or attrs_d.get("name") or ""
            ).lower()
            if key in ("og:title", "twitter:title") and not self.og_title:
                self.og_title = (attrs_d.get("content") or "").strip()
            elif (
                key in ("og:description", "twitter:description", "description")
                and not self.og_description
            ):
                self.og_description = (attrs_d.get("content") or "").strip()
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        # Self-closing markup never opens a scope; honour block breaks only.
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if self._main_depth and tag not in _VOID_TAGS:
            self._main_depth -= 1
        if self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._pending.append(text)

    # -- Accumulation -------------------------------------------------

    def _flush(self) -> None:
        if not self._pending:
            return
        chunk = " ".join(self._pending)
        self._pending = []
        (self._main if self._main_depth else self._body).append(chunk)

    def finish(self) -> None:
        self._flush()

    @property
    def main_text(self) -> str:
        return "\n".join(self._main)

    @property
    def body_text(self) -> str:
        return "\n".join(self._body)

    @property
    def best_title(self) -> str:
        return self.og_title or self.title


def _tidy(text: str, limit: int) -> str:
    """Collapse whitespace, drop stub lines, truncate."""
    lines: list[str] = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        # Single characters are stray bullets/dots, never prose.
        if len(line) < 2:
            continue
        lines.append(line)
    return "\n".join(lines)[:limit].strip()


def _blocked_marker(title: str, text: str) -> str:
    """Return the phrase that marks *title*/*text* as a blocked page, or "".

    A site that refuses a scripted request almost always answers HTTP 200 with
    a template instead of the article, so the transport looks fine and the
    extracted text is non-empty.  Without this check that template is injected
    into the prompt as "对方提到的链接你已经读过了", which makes the model
    summarise a verification wall — measurably worse than saying nothing, and
    the reason a WeChat article question got the answer "环境异常，完成验证后
    即可继续访问".

    Args:
        title: Page title as extracted.
        text: Page body as extracted.

    Returns:
        The matched marker (for the log line), or "" when the page looks like
        real content.
    """
    haystack = f"{title}\n{text}".lower()
    # Only the head is inspected: a real article may quote "完成验证" in its
    # prose, and a marker found deep in a long body is the topic, not a wall.
    head = haystack[:600]
    for marker in _BLOCKED_MARKERS:
        if marker.lower() in head:
            return marker
    return ""


def _decode_json_value(raw: str) -> str:
    """Decode the body of a JSON string literal (``\\n``, ``\\u002F`` …).

    Sites embed their state as JSON inside ``<script>``, so the prose arrives
    escaped: a line break is ``\\n`` and Xiaohongshu's CDN hosts are written
    ``\\u002F``.  Falls back to HTML unescaping when the literal does not
    round-trip through :mod:`json` — some pages carry bare ``undefined``.
    """
    try:
        value = json.loads(f'"{raw}"')
    except Exception:
        value = raw
    return html.unescape(value) if isinstance(value, str) else ""


def _embedded_state_text(page: str, keys: tuple[str, ...]) -> str:
    """Pull the longest readable value of *keys* out of an embedded state.

    Tried only after the HTML itself yielded nothing usable: a site that
    paints its page with JavaScript leaves the prose in a JSON blob and ships
    only UI labels as markup.  Measured on a Xiaohongshu video note — the
    mobile page's markup was 36 characters of "发弹幕 / App内打开" while the
    note's whole copy sat in ``"desc"``.

    Keys are tried in the caller's priority order and the first one that
    yields a body-length value wins, so the very generic ``"text"`` cannot
    beat the specific ``"desc"``.

    Args:
        page: Whole HTML document.
        keys: Candidate JSON keys, most specific first.

    Returns:
        The decoded text, or "" when no key carries a usable body.
    """
    if not page:
        return ""
    for key in keys:
        pattern = re.compile(_JSON_VALUE_PATTERN % re.escape(key))
        best = ""
        for match in pattern.finditer(page):
            value = _decode_json_value(match.group(1)).strip()
            if len(value) > len(best):
                best = value
        if len(best) >= _MIN_USABLE_TEXT:
            return best
    return ""


def extract_article(html_text: str) -> tuple[str, str]:
    """Return ``(title, text)`` from an HTML page.

    Both are "" when the page has no readable prose (a login wall, a JSON
    blob, an empty shell) — the caller then treats the fetch as a failure.
    """
    if not html_text:
        return "", ""
    parser = _Extractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # malformed markup is expected, not exceptional
        logger.debug("HTML parse failed", exc_info=True)
    parser.finish()

    title = _tidy(parser.best_title, _MAX_TITLE)
    # ``main`` is the article body when the site marks one; otherwise fall
    # back to the whole page, which is noisy but still readable.
    body = _tidy(parser.main_text or parser.body_text, _MAX_TEXT)
    # A body starting with a brace is the JSON/JS the parser chewed up, not an
    # article.  Content-Type usually catches this first; sites that omit the
    # header do not, and pasting a JSON blob into the prompt helps nobody.
    if body[:1] in ("{", "["):
        logger.debug("Link skipped: body looks like data, not prose")
        body = ""

    # A script-only shell: the article is painted by JavaScript, so the parsed
    # body is empty or a strip of UI labels.  Sites that do this still ship the
    # real text in a meta summary or an embedded JSON state, and either beats
    # reporting the link as unreadable — measured on a Xiaohongshu video note,
    # where markup held 36 characters of button labels and the note's copy sat
    # in ``"desc"``.
    if len(body) < _MIN_USABLE_TEXT:
        lede = parser.og_description or _embedded_state_text(html_text,
                                                             _JSON_BODY_KEYS)
        if len(lede) > len(body):
            body = _tidy(lede, _MAX_TEXT)
        if not title:
            title = _tidy(
                _embedded_state_text(html_text, _JSON_TITLE_KEYS), _MAX_TITLE
            )
    return title, body


# ── Fetching ─────────────────────────────────────────────────────────

#: url → (monotonic fetch time, article or None).  A link shared in a group is
#: usually asked about more than once; None is cached too, so a dead link is
#: not retried on every mention.
_cache: "OrderedDict[str, tuple[float, Optional[Article]]]" = OrderedDict()


def _cache_get(url: str) -> tuple[bool, Optional[Article]]:
    entry = _cache.get(url)
    if not entry:
        return False, None
    fetched_at, article = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SEC:
        _cache.pop(url, None)
        return False, None
    _cache.move_to_end(url)
    return True, article


def _cache_put(url: str, article: Optional[Article]) -> None:
    _cache[url] = (time.monotonic(), article)
    _cache.move_to_end(url)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _cache_key(url: str, render: bool, want_video: bool = False) -> str:
    """Cache key that keeps the static and rendered verdicts apart.

    Without the flag, a page first asked about with rendering *off* would
    cache "nothing readable" and that negative result would then block every
    later mention from ever trying the renderer.  Screened URLs are the one
    exception: that decision is a pure function of the URL, so both modes
    share it and a group that keeps posting an intranet address pays one
    lookup rather than two.

    ``want_video`` splits one more entry out, and only where it can change
    the answer: "这文章讲了啥" caches an Article with no media address, and
    the same link asked later as "视频里是什么" must not inherit that empty
    tuple.  Two questions about one link in a group chat is the normal case,
    not the exotic one.
    """
    key = f"{url}\x00render" if render else url
    return f"{key}\x00video" if want_video and render else key


def _build_opener(host: str = "") -> urllib.request.OpenerDirector:
    """Opener with the guarded redirect handler in place of the default one.

    Args:
        host: Passed to :func:`_ssl_context` so the chain-repair path can
            install this host's missing intermediates.  "" means a plain
            verifying context.
    """
    return urllib.request.build_opener(
        _GuardedRedirectHandler(),
        urllib.request.HTTPSHandler(context=_ssl_context(host)),
    )


def _ssl_context(host: str = "") -> ssl.SSLContext:
    """Verifying TLS context: system trust store **plus** ``certifi``'s bundle.

    Both are needed, and each alone is measurably wrong on this machine:

    * The bare default context rejects sites whose chain needs an
      intermediate that Windows' own store does not carry (measured:
      ``www.example.com`` fails with "unable to get local issuer certificate").
    * ``certifi`` alone rejects sites signed by a CA issued in China that
      exists in the Windows store but not in Mozilla's (measured:
      ``matrix67.com`` — the very link in the bug report — fails, while
      ``mp.weixin.qq.com`` and ``python.org`` both pass).

    So start from the system context and *add* certifi's roots to it.  If
    certifi is absent (it is optional for the frozen build), the system store
    is still used rather than turning verification off.

    Args:
        host: When given, also install that host's AIA-fetched intermediate
            certificates (see :func:`_verified_intermediates`).  This is the
            repair path for servers that send the leaf alone: browsers build
            the missing part of the chain themselves, Python's ``ssl`` never
            does, so a clean machine fails where this one succeeds.
    """
    context = ssl.create_default_context()
    try:
        import certifi  # optional: only used when already installed
        context.load_verify_locations(cafile=certifi.where())
    except Exception as exc:
        # Visible on purpose: without certifi the frozen build trusts only
        # Windows' store, which is exactly the setup where a site whose chain
        # needs an intermediate the store lacks starts failing.
        logger.warning(
            "Link fetch: certifi unavailable (%s: %s) — using the system "
            "trust store only", type(exc).__name__, exc,
        )
    if host:
        # One PEM blob, not a sequence: iterating it would hand
        # ``load_verify_locations`` a single character as a certificate.
        pem = _verified_intermediates(host)
        if pem:
            try:
                context.load_verify_locations(cadata=pem)
            except Exception as exc:
                logger.warning(
                    "Link fetch: could not install the repaired chain for %s "
                    "(%s: %s)", host, type(exc).__name__, exc,
                )
    return context


# ── Certificate chain repair ─────────────────────────────────────────
#
# Some servers send only the leaf certificate and leave the intermediates out
# (measured: matrix67.com sends one cert, ``CN=matrix67.com`` issued by
# ``CN=YR2``).  Browsers and Windows fetch the rest automatically; Python's
# ``ssl`` module does not, so the handshake dies with "unable to get local
# issuer certificate" — on *any* machine whose trust store does not happen to
# have YR2 cached, which is what a freshly provisioned VM looks like.  That is
# the whole bug: the bot reported "我打不开链接" on the VM while the same link
# worked on the dev machine.

#: AIA walking is bounded: a chain longer than this is broken or hostile.
_MAX_CHAIN_DEPTH = 5
#: Per-certificate AIA download budget.  Repair runs after a failed
#: handshake, i.e. already off the fast path, so this stays short.
_AIA_TIMEOUT_SEC = 3

#: host → PEM of verified intermediates ("" when repair is impossible).
_intermediate_cache: dict[str, str] = {}


def _aia_ca_issuers(cert) -> list[str]:
    """Return the *CA Issuers* URLs from a certificate's AIA extension."""
    try:
        from cryptography import x509
    except Exception:
        return []
    try:
        ext = cert.extensions.get_extension_for_class(
            x509.AuthorityInformationAccess
        )
    except Exception:
        return []
    return [
        desc.access_location.value
        for desc in ext.value
        if desc.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS
        and isinstance(desc.access_location, x509.UniformResourceIdentifier)
    ]


def _trust_anchors() -> list:
    """Every **root** this process would accept, as ``cryptography`` objects.

    Mirrors :func:`_ssl_context` in coverage — certifi's bundle *and* the
    system store — but keeps only self-signed certificates.

    Filtering to self-signed is load-bearing, not tidiness: Windows' store
    also caches *intermediate* certificates (measured: it carries ``YR2`` on
    the dev box), and an intermediate accepted as an anchor makes the repair
    step stop one hop too early.  The chain then looks verified here while
    still being incomplete for OpenSSL, which is exactly how a repaired fetch
    failed on the VM with the intermediates present.
    """
    anchors: list = []
    seen: set[bytes] = set()

    def _add(cert) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            # ``subject == issuer`` is the definition of a root.
            if cert.subject != cert.issuer:
                return
            fingerprint = cert.public_bytes(serialization.Encoding.DER)
        except Exception:
            return
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        anchors.append(cert)

    try:
        from cryptography import x509
        import certifi
        pem = open(certifi.where(), "rb").read()
        for block in re.findall(
            rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S
        ):
            try:
                _add(x509.load_pem_x509_certificate(block))
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Link fetch: certifi roots unavailable (%s)", exc)
    for store in ("ROOT", "CA"):
        try:
            for der, encoding, _trust in ssl.enum_certificates(store):
                if encoding != "x509_asn":
                    continue
                try:
                    _add(x509.load_der_x509_certificate(der))
                except Exception:
                    continue
        except Exception:
            # Non-Windows, or the store is locked; certifi alone will do.
            continue
    return anchors


def _verified_intermediates(host: str) -> str:
    """Fetch *host*'s missing intermediates via AIA and prove they are valid.

    Returns the PEM of the intermediates to install, or "" when the chain
    cannot be completed or does not verify.

    **Why this cannot be used to MITM the bot** — the obvious objection, and
    the reason the verification step is not optional: a certificate pulled
    from an AIA URL is attacker-controlled the moment DNS or the plain-HTTP
    AIA URL is, and :meth:`SSLContext.load_verify_locations` promotes whatever
    it is handed into a **trust anchor**.  Adding an unverified certificate
    would therefore let anyone with a forged chain impersonate any site.  So
    the full path is validated first — leaf → intermediates → an anchor this
    process already trusts for *this* hostname — using the same policy engine
    OpenSSL uses.  Only certificates that are part of a chain terminating in a
    pre-existing trust anchor are installed, and even then they are
    intermediates, never roots: a forged leaf still fails verification.
    """
    if host in _intermediate_cache:
        return _intermediate_cache[host]
    # Cache the failure too: an unreachable chain should not be re-walked on
    # every mention of the same link.
    _intermediate_cache[host] = ""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        from cryptography.x509.verification import DNSName, PolicyBuilder, Store
    except Exception as exc:
        logger.debug("Link fetch: chain repair unavailable (%s)", exc)
        return ""

    try:
        # Step 1 — the leaf.  Verification is off *only* to read the
        # certificate; nothing is trusted on the strength of this handshake.
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe.check_hostname = False
        probe.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=_AIA_TIMEOUT_SEC) as raw:
            with probe.wrap_socket(raw, server_hostname=host) as tls:
                chain = tls.get_unverified_chain() or []
        if not chain:
            logger.warning("Link fetch: %s sent no certificate", host)
            return ""
        leaf = x509.load_der_x509_certificate(bytes(chain[0]))

        # Step 2 — walk AIA upwards.  Each hop is followed only while the
        # next certificate is signed by the current one, so a hostile AIA
        # cannot splice in an unrelated certificate.
        intermediates = [
            x509.load_der_x509_certificate(bytes(der)) for der in chain[1:]
        ]
        current = leaf
        for _depth in range(_MAX_CHAIN_DEPTH):
            urls = _aia_ca_issuers(current)
            if not urls:
                break
            fetched = None
            for url in urls:
                if not is_public_url(url, resolve=False):
                    continue
                try:
                    with urllib.request.urlopen(url, timeout=_AIA_TIMEOUT_SEC) as resp:
                        blob = resp.read(64 * 1024)
                    fetched = x509.load_der_x509_certificate(blob)
                    break
                except Exception as exc:
                    logger.debug("Link fetch: AIA %s failed (%s)", url, exc)
            if fetched is None:
                break
            if fetched.subject != current.issuer:
                logger.warning(
                    "Link fetch: AIA for %s returned %r, expected issuer %r — "
                    "refusing the chain", host,
                    fetched.subject.rfc4514_string(),
                    current.issuer.rfc4514_string(),
                )
                return ""
            intermediates.append(fetched)
            current = fetched

        if not intermediates:
            logger.info("Link fetch: no intermediates needed for %s", host)
            return ""

        # Step 3 — the load-bearing check.  Anything that does not chain to a
        # trust anchor we already had is discarded, which is what keeps this
        # from becoming a way to trust attacker-supplied certificates.
        verifier = (
            PolicyBuilder()
            .store(Store(_trust_anchors()))
            .build_server_verifier(DNSName(host))
        )
        verifier.verify(leaf, intermediates)
    except Exception as exc:
        logger.warning(
            "Link fetch: could not repair the certificate chain for %s "
            "(%s: %s)", host, type(exc).__name__, exc,
        )
        return ""

    pem = "".join(
        cert.public_bytes(Encoding.PEM).decode("ascii") for cert in intermediates
    )
    _intermediate_cache[host] = pem
    logger.info(
        "Link fetch: repaired the TLS chain for %s with %d intermediate(s)",
        host, len(intermediates),
    )
    return pem


def clear_cache() -> None:
    """Drop every cached page (used by tests)."""
    _cache.clear()


def _is_tls_error(exc: BaseException) -> bool:
    """True when *exc* is a TLS failure, whether wrapped by urllib or not.

    ``urllib`` wraps handshake failures in ``URLError`` and leaves the real
    cause in ``.reason``, so a plain ``isinstance(exc, SSLError)`` misses the
    very case this repair path exists for.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ssl.SSLError, ssl.CertificateError)):
            return True
        current = getattr(current, "reason", None)
        if not isinstance(current, BaseException):
            return False
    return False


def _http_fallback(url: str) -> str:
    """The same page over plain HTTP, or "" when *url* is already http.

    Used only when HTTPS failed for a *certificate* reason: some servers send
    an incomplete chain, and reading the piece over plaintext beats not
    reading it at all.  The scheme and nothing else changes — same host, same
    path — so this cannot be turned into a request to somewhere new, and the
    result still passes :func:`is_public_url` before it is fetched.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https":
        return ""
    return urllib.parse.urlunsplit(
        ("http", parts.netloc, parts.path, parts.query, "")
    )


def _request_page(
    url: str, *, ua: str, timeout: float, host: str = ""
) -> tuple[str, int, str]:
    """GET *url* and decode it.  Raises on transport failure.

    Args:
        url: Page to read.
        ua: User-Agent to present.  Which one is sent is the whole point of
            the mobile retry — see :data:`_MOBILE_UA`.
        timeout: Socket timeout in seconds.
        host: Passed to :func:`_build_opener` to enable chain repair.

    Returns:
        ``(page_text, raw_byte_count, content_type)``.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        # Ask for an uncompressed body; the decode path still handles gzip.
        "Accept-Encoding": "identity",
    })
    with _build_opener(host).open(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if not _is_textual(content_type):
            # Do not read the body: a 4 GB video with a text-ish URL must not
            # be pulled into memory just to be rejected.
            return "", 0, content_type
        raw = response.read(_MAX_BYTES + 1)[:_MAX_BYTES]
        charset = response.headers.get_content_charset() or ""
        page = _decode(
            _decompress(raw, response.headers.get("Content-Encoding", "")),
            charset,
        )
        if not charset:
            meta = _META_CHARSET_RE.search(page[:4096])
            if meta:
                page = _decode(raw, meta.group(1))
    return page, len(raw), content_type


def _video_urls(page: str) -> list[str]:
    """Media addresses a page embeds, taken from *this* response.

    Imported inside the call: :mod:`src.media.video` imports
    ``is_public_url`` from here, so a module-level import would be circular.
    Extracting the addresses costs only a few regex passes and does not need
    ffmpeg, so a build without the binary still records them; sampling the
    frames later is what degrades.

    Args:
        page: The page HTML that was just fetched.

    Returns:
        Candidate media URLs, possibly empty.
    """
    try:
        from .video import video_urls_from_page
    except Exception:  # pragma: no cover - import cannot realistically fail
        logger.debug("video module unavailable", exc_info=True)
        return []
    return video_urls_from_page(page)


def _judge_page(
    url: str, page: str, *, raw_len: int, content_type: str
) -> tuple[Optional[Article], str]:
    """Turn a fetched page into an article, or say why not.

    Returns:
        ``(article, "")`` when the page is readable, otherwise
        ``(None, reason)`` where *reason* is one of ``"non-textual"``,
        ``"blocked"``, ``"stub"``, ``"empty"``.
    """
    if not _is_textual(content_type):
        logger.warning(
            "Link skipped: non-textual %s (%s)", content_type, url[:80],
        )
        return None, "non-textual"

    title, text = extract_article(page)
    blocked = _blocked_marker(title, text)
    if blocked:
        # The site answered 200 with a verification wall, a "page not found"
        # template or an app-launch notice.  Treating that as a read article
        # is what made the bot summarise "环境异常，完成验证后即可继续访问"
        # as if it were the article's content.
        logger.warning(
            "Link blocked by the site: %s (%r) — %s", url[:80],
            blocked, "not passing page furniture to the model",
        )
        return None, "blocked"

    if text and len(text) >= _MIN_USABLE_TEXT:
        return Article(
            url=url, title=title, text=text,
            video_urls=tuple(_video_urls(page)),
        ), ""

    # Too little prose to call an article — but a video *note* often ships
    # exactly that: a title, a caption of a few characters and the clip's
    # address.  Dropping the page here used to throw the address away with
    # it, so a shared clip that the site describes in one line could never
    # be sampled.  The Article carries no text (``format_link_context``
    # skips empty bodies) and only feeds the frame path.
    video_urls = tuple(_video_urls(page))
    if video_urls:
        logger.info(
            "Link has a video address but only %d chars of text: %s "
            "(%d candidate(s))", len(text), url[:80], len(video_urls),
        )
        return Article(url=url, title=title, text="", video_urls=video_urls), ""

    if text:
        logger.warning(
            "Link skipped: only %d chars in %s — that is a stub, "
            "not an article", len(text), url[:80],
        )
        return None, "stub"

    logger.warning(
        "Link skipped: no readable body in %s (%d bytes, content-type=%s)",
        url[:80], raw_len, content_type,
    )
    return None, "empty"


def _with_video_urls(
    article: Article, *, url: str, timeout: float,
) -> Article:
    """Return *article* carrying a media address, rendering only if it has none.

    Called for questions that actually ask about a clip (:func:`wants_video`).
    A page whose prose was readable never reaches the shell-render branch
    below, so without this the address a script built would be lost exactly on
    the sites that *do* write their caption out — the note describes itself in
    text and sets ``<video src>`` from JavaScript, which the static bytes never
    contain (measured: a page doing both 400 ms after load).

    Only ``video_urls`` is borrowed.  The static prose is kept because it is
    the fuller of the two: a rendered dump of a scripted page routinely paints
    less text than the server sent, and the answer summarises that text.

    Bilibili is asked through its JSON API when rendering yields nothing.
    The browser is the unreliable half there: it has been seen to exit 0
    with an empty DOM in about a second, where its own API answers the
    stream addresses without any browser at all (see
    :func:`~src.media.video.bilibili_video_urls`).
    """
    if article.video_urls:
        return article
    rendered = _render_page(url, timeout=timeout)
    if rendered is not None and rendered.video_urls:
        logger.info(
            "Video address found by rendering: %s (%d candidate(s))",
            url[:80], len(rendered.video_urls),
        )
        return replace(article, video_urls=rendered.video_urls)

    # Imported here: ``video`` imports this module for its URL fence, so a
    # module-level import would be circular.
    try:
        from .video import bilibili_video_urls
    except Exception:  # pragma: no cover - import cannot fail in practice
        logger.debug("video module unavailable", exc_info=True)
        return article
    api_urls = bilibili_video_urls(url)
    if not api_urls:
        return article
    logger.info(
        "Video address found through the Bilibili API: %s (%d candidate(s))",
        url[:80], len(api_urls),
    )
    return replace(article, video_urls=tuple(api_urls))


def _fetch_once(
    url: str, *, host: str = "", render: bool = False,
    render_timeout: float = 12.0, want_video: bool = False,
) -> tuple[Optional[Article], Optional[BaseException]]:
    """One fetch attempt.  Returns ``(article, error)`` and never raises.

    Two requests at most: the page as a desktop browser would see it, and —
    only when that yields nothing readable — the same page as a phone.  Some
    sites serve a JavaScript shell to desktops and real content to mobile
    clients; measured on a Xiaohongshu video note, where the desktop page had
    no title and no body at all and the mobile page carried both.

    Args:
        url: Page to read.
        host: Passed to :func:`_build_opener` to enable chain repair.  "" for
            the plain verifying context, which is the normal first attempt.
        render: Allow the script-executing fallback when both static views
            are empty.  Only meaningful on the first attempt; the TLS-repair
            retry passes False because rendering does not fix a certificate.
        render_timeout: Wall clock the renderer may use.

    Returns:
        ``(article, None)`` on success, ``(None, exception)`` when the request
        itself failed, and ``(None, None)`` when it was answered but had
        nothing readable in it — the last case must not trigger repairs,
        because the connection was fine.
    """
    try:
        page, raw_len, content_type = _request_page(
            url, ua=_UA, timeout=_TIMEOUT_SEC, host=host,
        )
    except urllib.error.HTTPError as exc:
        # The server answered, it just refused: no repair or fallback helps.
        logger.warning("Link fetch HTTP %s: %s", exc.code, url[:80])
        return None, None
    except Exception as exc:
        return None, exc

    article, reason = _judge_page(url, page, raw_len=raw_len,
                                  content_type=content_type)
    if article is not None:
        # Readable prose, but the question is about the clip: the player may
        # still be script-built.  Only this branch pays for that, and only
        # when the question asked for it.
        if want_video and render:
            article = _with_video_urls(
                article, url=url, timeout=render_timeout,
            )
        return article, None
    if reason == "non-textual":
        return article, None

    # The desktop view was a wall, a stub or a shell.  Ask again as a phone:
    # that is what turns "读不到" into the note's title and copy.
    try:
        mobile_page, mobile_raw, mobile_type = _request_page(
            url, ua=_MOBILE_UA, timeout=_MOBILE_TIMEOUT_SEC, host=host,
        )
    except urllib.error.HTTPError as exc:
        logger.info(
            "Link fetch: mobile retry answered HTTP %s for %s, giving up",
            exc.code, url[:80],
        )
        return None, None
    except Exception as exc:
        # The first request already failed to give usable content; a failed
        # retry is not worth escalating into a TLS repair.
        logger.info(
            "Link fetch: mobile retry failed for %s (%s)",
            url[:80], type(exc).__name__,
        )
        return None, None

    article, reason = _judge_page(url, mobile_page, raw_len=mobile_raw,
                                  content_type=mobile_type)
    if article is not None:
        logger.info(
            "Link fetched with a mobile User-Agent: %s (%d chars, title=%r)",
            url[:80], len(article.text), article.title[:40],
        )
        if want_video and render:
            article = _with_video_urls(
                article, url=url, timeout=render_timeout,
            )
        return article, None

    # Neither text view had anything in it.  That is the signature of a page
    # whose body *and* player are built by script — the media address is in
    # no static response at all.  Let a real browser run the page and judge
    # the finished DOM instead.  ``blocked`` is deliberately excluded: a
    # verification wall or a "deleted" notice is an answer, and re-rendering
    # it only burns wall clock on the reply path.
    if reason in ("empty", "stub") and render:
        rendered = _render_page(url, timeout=render_timeout)
        if rendered is not None:
            return rendered, None
    return None, None


#: Whether the script-executing fallback may run.  ``fetch_article`` takes it
#: as a parameter so the caller (which owns the config) stays in charge; this
#: is only the default for direct calls such as tests and one-off tools.
_RENDER_ENABLED = True


def set_render_enabled(enabled: bool) -> None:
    """Turn the rendering fallback on or off for direct ``fetch_article`` calls.

    The live path passes ``render=`` explicitly, so this exists for callers
    that have no config object — the web UI's one-off fetch, scripts, tests.
    """
    global _RENDER_ENABLED
    _RENDER_ENABLED = bool(enabled)


def _render_page(url: str, *, timeout: float = 12.0) -> Optional[Article]:
    """Render *url* in a headless browser and judge the finished DOM.

    Imported inside the call: :mod:`src.media.browser_render` locates a
    browser on first use, and a build without one must still read static
    pages.  Every failure degrades to None — this runs inside the reply path.

    Security note, stated rather than hidden: the page's *own* scripts run in
    the browser, and that browser's network stack is not filtered by
    :func:`is_public_url`.  The URL handed here has already been vetted, but a
    page could redirect the renderer to an internal address.  That is a
    different threat model from the ``urllib`` path, which re-checks every
    hop (:class:`_GuardedRedirectHandler`); the mitigation is that anything
    extracted from the result (a media address) is vetted again before it is
    fetched.
    """
    try:
        from . import browser_render
    except Exception:  # pragma: no cover - import cannot realistically fail
        logger.debug("browser_render unavailable", exc_info=True)
        return None
    if not browser_render.available():
        return None

    dom = browser_render.render_html(url, timeout=timeout)
    if not dom:
        logger.info("Link render produced nothing for %s", url[:80])
        return None

    article, reason = _judge_page(
        url, dom, raw_len=len(dom.encode("utf-8", errors="replace")),
        content_type="text/html",
    )
    if article is None:
        logger.info("Link render gave no readable page (%s): %s", reason, url[:80])
        return None
    logger.info(
        "Link fetched by rendering: %s (%d chars, title=%r, %d video address(es))",
        url[:80], len(article.text), article.title[:40], len(article.video_urls),
    )
    return article


def fetch_article(
    url: str, *, render: Optional[bool] = None,
    render_allowed: bool = True,
    render_timeout: float = _RENDER_TIMEOUT_SEC,
    want_video: bool = False,
) -> Optional[Article]:
    """Fetch *url* and reduce it to an :class:`Article`, or None.

    Blocking: the caller is expected to run this off the reply path or accept
    up to ``_TIMEOUT_SEC`` of latency.  Never raises — a link that cannot be
    read is a normal outcome, not an error.

    Three attempts at most, each strictly harder to reach than the last:

    1. Plain verifying fetch.  This is what nearly every site needs.
    2. On a *TLS* failure only, repair the chain via AIA and retry.  Some
       servers send the leaf certificate alone (measured: ``matrix67.com``);
       browsers fill the gap in silently, Python's ``ssl`` does not, so
       whether the fetch works otherwise depends on what the machine happens
       to have cached — a dev box passed, a fresh VM failed.
    3. Still TLS-broken, then read the same page over plain HTTP.

    Args:
        url: The shared link.  Must pass :func:`is_public_url`.
        render: Allow the headless-browser fallback.  ``None`` = module
            default, which is what direct callers (tests, one-off tools) get.
        render_allowed: False when the caller's wall-clock budget cannot
            cover a render any more.  Separate from ``render`` because the
            two answer different questions: "may I" versus "can I afford it".
        render_timeout: Wall clock the renderer may use.
        want_video: The question asks about a clip, so a readable page that
            names no media address may spend one render to find one.  Off by
            default: plain article questions must not pay for a browser.

    Returns:
        The article, or None when the URL is refused, unreachable, not
        textual, or has no readable body.
    """
    url = (url or "").strip()
    if not url:
        return None

    # ``render=None`` means "whatever the module default is": the live path
    # always passes the config value, so this only covers direct callers.
    use_render = _RENDER_ENABLED if render is None else bool(render)
    use_render = use_render and render_allowed
    use_video = bool(want_video) and use_render
    key = _cache_key(url, use_render, use_video)

    hit, cached = _cache_get(key)
    if hit:
        return cached
    if use_render:
        # A screened URL is the one entry both modes share.  Without this the
        # "refused" verdict would be stored under the bare URL and never read
        # back by the rendering mode, costing a DNS lookup per mention.
        hit, cached = _cache_get(url)
        if hit and cached is None:
            return None

    if not is_public_url(url):
        # Rejected URLs are cached too: the decision is a pure function of the
        # URL, and a group that keeps sharing an intranet address must not
        # cost a DNS lookup per mention.  Both modes share this entry.
        _cache_put(url, None)
        return None

    try:
        hostname = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        hostname = ""

    article, error = _fetch_once(
        url, render=use_render, render_timeout=render_timeout,
        want_video=use_video,
    )

    if article is None and error is not None and _is_tls_error(error) and hostname:
        logger.info(
            "Link fetch: TLS failed for %s (%s), retrying with a repaired "
            "certificate chain", url[:80], type(error).__name__,
        )
        # ``render=False`` on the retry: rendering runs the page in a browser
        # that has its own trust store, so it is no repair for *our* TLS
        # failure.  The renderer gets its own attempt further down instead.
        article, retry_error = _fetch_once(url, host=hostname)
        if article is None and retry_error is not None:
            error = retry_error

    fallback = _http_fallback(url)
    if article is None and fallback and error is not None and _is_tls_error(error):
        # Logged as a warning: the page is now readable, but it travelled in
        # clear text and a network attacker could have altered what the model
        # will summarise.  Silence here would hide that.
        logger.warning(
            "Link fetch: reading %s over plain HTTP because its certificate "
            "chain could not be verified — content is not authenticated",
            fallback[:80],
        )
        article, fallback_error = _fetch_once(
            fallback, render=use_render, render_timeout=render_timeout,
        )
        if article is None and fallback_error is not None:
            error = fallback_error

    if article is None and error is not None:
        # The reply degrades to "我打不开链接" and this line is the only trace
        # of *why* — TLS, DNS, timeout, proxy.  Keep it at WARNING so it
        # survives the bot's INFO-level log, and name the exception type: a
        # bare repr of an SSLError and of a timeout read nothing alike.
        logger.warning(
            "Link fetch failed: %s (%s: %s)", url[:80],
            type(error).__name__, error,
        )
        logger.debug("Link fetch traceback", exc_info=error)

    _cache_put(key, article)
    if article:
        logger.info(
            "Link fetched: %s (%d chars, title=%r)",
            url[:80], len(article.text), article.title[:40],
        )
    return article


# ── Question-level entry point ───────────────────────────────────────


def extract_urls(text: str) -> list[str]:
    """Return the URLs in *text*, in order, deduplicated.

    Chat text carries punctuation around links ("看看这个 https://x.com/a，厉害吧"),
    so trailing sentence marks are peeled off rather than becoming part of the
    path — a URL with a stray "，" in it 404s.
    """
    if not text:
        return []
    found: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0)
        # ``rstrip`` with the junk set handles "...)." and "...!!" in one go;
        # loop because stripping may expose more junk underneath.
        while url and url[-1] in _TRAILING_JUNK:
            url = url[:-1]
        if url and url not in found:
            found.append(url)
    return found


def fetch_for_question(
    text: str,
    *,
    max_links: int = _MAX_LINKS_PER_QUESTION,
    budget_sec: float = _FETCH_BUDGET_SEC,
    render: Optional[bool] = None,
    render_timeout: float = _RENDER_TIMEOUT_SEC,
) -> list[Article]:
    """Fetch the pages a question links to.

    The caller is in the reply path, so the whole group of fetches shares one
    deadline: a question with two dead links must not cost two full timeouts.
    Remaining links are skipped once the budget is gone.

    Args:
        text: The user's question, URLs embedded.
        max_links: Most pages to fetch for one question.
        budget_sec: Total wall clock allowed for the batch.
        render: Allow the headless-browser fallback.  ``None`` = module
            default.  Rendering is also *skipped* when the remaining budget
            cannot cover ``render_timeout`` — starting a browser it will be
            killed for is worse than not starting it.
        render_timeout: Wall clock one render may take.

    Returns:
        The articles that could be read, in the order the links appeared.
        Empty when there is nothing to fetch or nothing worked.
    """
    urls = extract_urls(text)[:max(0, max_links)]
    if not urls:
        return []

    # Decided once for the whole batch: the question is the only thing that
    # says whether a media address is wanted, and every link in it is being
    # asked about the same way.
    wants_clip = wants_video(text)
    deadline = time.monotonic() + max(0.0, budget_sec)
    articles: list[Article] = []
    for url in urls:
        left = deadline - time.monotonic()
        if left <= 0:
            logger.info("Link fetch budget exhausted, skipping %s", url[:80])
            break
        # The static attempts are cheap and come first inside ``fetch_article``;
        # the render happens last, so it only runs when the batch can still
        # afford it.  Starting a browser this deadline will kill is worse than
        # not starting it: the reply pays the wait and gets nothing.
        #
        # The gate is "enough left for a *clamped* render", not "the full
        # timeout still fits".  Comparing against ``render_timeout`` made the
        # two defaults self-cancelling — 8s of batch budget never covers a 12s
        # timeout, so ``allowance`` was False on every live question and the
        # renderer was dead code in production.  ``render_timeout`` is clamped
        # to ``left`` below, so a short remainder is usable as long as it
        # clears the floor.
        allowance = left >= min(render_timeout, _MIN_RENDER_BUDGET_SEC)
        article = fetch_article(
            url,
            render=render if render is not None else None,
            render_allowed=allowance,
            render_timeout=min(render_timeout, max(1.0, left)),
            want_video=wants_clip,
        )
        if article:
            articles.append(article)
    return articles


def format_link_context(articles: list[Article], max_text: int = _MAX_TEXT) -> str:
    """Render fetched articles as the prompt block, or "" when none.

    The block is labelled ``[链接正文]`` in its own section so the model reads
    it as content it has *already seen* — the failure this whole module exists
    to prevent is the model answering "我打不开链接" while the text sits in the
    prompt unlabelled.
    """
    blocks = [a.to_prompt_block(max_text) for a in articles if a.text.strip()]
    if not blocks:
        return ""
    return "对方提到的链接你已经读过了，内容如下（直接据此回答，不要说你打不开）：\n" + "\n\n".join(blocks)
