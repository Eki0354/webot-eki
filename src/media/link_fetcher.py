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
import ipaddress
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
from dataclasses import dataclass
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

#: How many links one question may pull.  Each costs a blocking fetch, and a
#: message with five links is a link dump, not a question about one article.
_MAX_LINKS_PER_QUESTION = 2

#: Total wall-clock budget for a question's fetches.  ``_TIMEOUT_SEC`` is per
#: request; without this, two dead links would block the reply for 12s.
_FETCH_BUDGET_SEC = 8


@dataclass(frozen=True)
class Article:
    """What a fetched page gave us: where, what it is called, what it says."""

    url: str
    title: str
    text: str

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
    except (OSError, UnicodeError):
        logger.debug("Link skipped: cannot resolve %s", host)
        return False
    if not infos:
        return False
    # Every address must be public: a host that resolves to both 1.2.3.4 and
    # 127.0.0.1 would otherwise be able to reach the loopback on a retry.
    for info in infos:
        if not _is_public_ip(info[4][0]):
            logger.debug("Link skipped: %s resolves to %s", host, info[4][0])
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
        return title, ""
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


def _build_opener() -> urllib.request.OpenerDirector:
    """Opener with the guarded redirect handler in place of the default one."""
    return urllib.request.build_opener(
        _GuardedRedirectHandler(), urllib.request.HTTPSHandler(context=_ssl_context())
    )


def _ssl_context() -> ssl.SSLContext:
    """Verifying TLS context, trusting ``certifi``'s bundle when available.

    Windows CPython builds here ship no ``cafile``/``capath`` (measured:
    ``ssl.get_default_verify_paths()`` returns None for both), and the default
    context then rejects sites whose chain needs an intermediate the local
    store lacks — ``example.com`` fails, ``python.org`` passes.  certifi is
    already on disk through other packages, so prefer its complete Mozilla
    bundle; without it, fall back to the system defaults rather than disabling
    verification.
    """
    try:
        import certifi  # optional: only used when already installed
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def clear_cache() -> None:
    """Drop every cached page (used by tests)."""
    _cache.clear()


def fetch_article(url: str) -> Optional[Article]:
    """Fetch *url* and reduce it to an :class:`Article`, or None.

    Blocking: the caller is expected to run this off the reply path or accept
    up to ``_TIMEOUT_SEC`` of latency.  Never raises — a link that cannot be
    read is a normal outcome, not an error.

    Args:
        url: The shared link.  Must pass :func:`is_public_url`.

    Returns:
        The article, or None when the URL is refused, unreachable, not
        textual, or has no readable body.
    """
    url = (url or "").strip()
    if not url:
        return None

    hit, cached = _cache_get(url)
    if hit:
        return cached

    if not is_public_url(url):
        # Rejected URLs are cached too: the decision is a pure function of the
        # URL, and a group that keeps sharing an intranet address must not
        # cost a DNS lookup per mention.
        _cache_put(url, None)
        return None

    request = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        # Ask for an uncompressed body; the decode path still handles gzip.
        "Accept-Encoding": "identity",
    })

    article: Optional[Article] = None
    try:
        with _build_opener().open(request, timeout=_TIMEOUT_SEC) as response:
            if not _is_textual(response.headers.get("Content-Type", "")):
                logger.debug(
                    "Link skipped: non-textual %s",
                    response.headers.get("Content-Type"),
                )
            else:
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
                title, text = extract_article(page)
                if text:
                    article = Article(url=url, title=title, text=text)
                else:
                    logger.debug("Link skipped: no readable body in %s", url)
    except urllib.error.HTTPError as exc:
        logger.debug("Link fetch HTTP %s: %s", exc.code, url)
    except Exception:
        logger.debug("Link fetch failed: %s", url, exc_info=True)

    _cache_put(url, article)
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
) -> list[Article]:
    """Fetch the pages a question links to.

    The caller is in the reply path, so the whole group of fetches shares one
    deadline: a question with two dead links must not cost two full timeouts.
    Remaining links are skipped once the budget is gone.

    Args:
        text: The user's question, URLs embedded.
        max_links: Most pages to fetch for one question.
        budget_sec: Total wall clock allowed for the batch.

    Returns:
        The articles that could be read, in the order the links appeared.
        Empty when there is nothing to fetch or nothing worked.
    """
    urls = extract_urls(text)[:max(0, max_links)]
    if not urls:
        return []

    deadline = time.monotonic() + max(0.0, budget_sec)
    articles: list[Article] = []
    for url in urls:
        if time.monotonic() >= deadline:
            logger.info("Link fetch budget exhausted, skipping %s", url[:80])
            break
        article = fetch_article(url)
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
