"""Reading a shared link: keep its URL, fetch it safely, put the text in the prompt.

Reported bug: a group member quotes an article and asks "这文章讲了啥", and the
bot answers that it cannot open links.  Two causes, both tested here —

1. ``reduce_appmsg`` kept the title and dropped ``<url>``, so the model never
   even saw the address.
2. The chat API has no web access at all (``_call_chat_api`` passes no tools),
   so the address alone changes nothing: the page has to be fetched
   server-side.  ``link_fetcher`` is that fetcher, and because its input comes
   from a group chat, a stranger can choose the URL — the SSRF checks are the
   part of this file that matters most.

No network access anywhere: ``is_public_url`` is called with ``resolve=False``
and ``fetch_article`` is monkeypatched, so these run offline and fast.
"""

import unittest


_LINK = "https://mp.weixin.qq.com/s/AbCdEf123"
_TITLE = "DeepSeek 发布新模型"

_PLAIN_APPMSG = (
    '<msg><appmsg>'
    f'<title>{_TITLE}</title><des>性能提升 30%</des>'
    f'<url>{_LINK}</url><type>5</type>'
    '</appmsg></msg>'
)

_QUOTED_LINK = (
    '<msg><appmsg>'
    '<title>这个文章讲了啥？</title>'
    '<type>57</type>'
    '<refermsg>'
    '<type>5</type>'
    '<displayname>张三</displayname>'
    '<content>&lt;msg&gt;&lt;appmsg&gt;'
    f'&lt;title&gt;{_TITLE}&lt;/title&gt;'
    f'&lt;url&gt;{_LINK}&lt;/url&gt;'
    '&lt;type&gt;5&lt;/type&gt;&lt;/appmsg&gt;&lt;/msg&gt;'
    '</content>'
    '</refermsg>'
    '</appmsg></msg>'
)

_ARTICLE_HTML = """<html><head>
<title>页面标题 - 公众号</title>
<meta property="og:title" content="DeepSeek 发布新模型">
<style>.x { color: red }</style>
<script>var tracking = 1;</script>
</head><body>
<nav>导航 首页 登录</nav>
<div id="js_content">
  <p>第一段：性能提升 30%。</p>
  <p>第二段：推理成本明显下降。</p>
</div>
<footer>版权所有</footer>
</body></html>"""


class AppmsgUrlTests(unittest.TestCase):
    """The URL has to survive reduction — it is the only actionable part."""

    def test_shared_link_keeps_its_url(self):
        from src.media.appmsg import reduce_appmsg

        reduced = reduce_appmsg(_PLAIN_APPMSG)
        self.assertIn(_TITLE, reduced)
        self.assertIn(_LINK, reduced)

    def test_quoted_link_carries_the_quoted_url(self):
        """A quoted article must arrive with its own title *and* URL."""
        from src.media.appmsg import reduce_appmsg

        reduced = reduce_appmsg(_QUOTED_LINK)
        self.assertTrue(reduced.startswith("[引用] 这个文章讲了啥？"), reduced)
        self.assertIn("[链接]", reduced)
        self.assertIn(_TITLE, reduced)
        self.assertIn(_LINK, reduced)

    def test_long_url_is_not_truncated_into_a_dead_link(self):
        """The 120-char text limit would cut a URL mid-path."""
        from src.media.appmsg import appmsg_url, reduce_appmsg

        long_url = "https://example.com/s/" + "a" * 300
        xml = (
            '<msg><appmsg><title>标题</title>'
            f'<url>{long_url}</url><type>5</type></appmsg></msg>'
        )
        self.assertEqual(appmsg_url(xml), long_url)
        self.assertIn(long_url, reduce_appmsg(xml))

    def test_escaped_ampersands_are_unescaped(self):
        """A shared card stores its URL HTML-escaped; ``&amp;`` is not a query separator.

        Reported live: a WeChat article card arrived as
        ``...?__biz=X&amp;mid=Y&amp;idx=1``.  Sending that literally makes the
        server see one parameter whose *name* is ``amp;mid``, so every query
        argument after the first is silently dropped (measured:
        ``?a=1&amp;b=2`` parses as ``{'a': '1', 'amp;b': '2'}``).
        """
        from src.media.appmsg import appmsg_url, reduce_appmsg

        url = "https://mp.weixin.qq.com/s?__biz=MzI5&amp;mid=2247&amp;idx=1"
        xml = f'<msg><appmsg><title>新政</title><url>{url}</url><type>5</type></appmsg></msg>'
        expected = "https://mp.weixin.qq.com/s?__biz=MzI5&mid=2247&idx=1"

        self.assertEqual(appmsg_url(xml), expected)
        reduced = reduce_appmsg(xml)
        self.assertIn(expected, reduced)
        self.assertNotIn("&amp;", reduced)
        # The fetcher must see real separators, or the request is mangled.
        from src.media.link_fetcher import extract_urls
        self.assertEqual(extract_urls(reduced), [expected])

    def test_file_url_does_not_match_the_url_tag(self):
        """``<cdnattachurl>`` must not be mistaken for ``<url>``."""
        from src.media.appmsg import appmsg_url

        xml = (
            '<msg><appmsg><title>文件</title><type>6</type>'
            '<cdnattachurl>https://cdn.example.com/f.zip</cdnattachurl>'
            '</appmsg></msg>'
        )
        self.assertEqual(appmsg_url(xml), "")

    def test_appmsg_without_url_is_unchanged(self):
        """Old payloads carry no ``<url>``; they must not gain a stray space."""
        from src.media.appmsg import reduce_appmsg

        xml = '<msg><appmsg><title>总决赛你的队友</title><type>5</type></appmsg></msg>'
        self.assertEqual(reduce_appmsg(xml), "[链接] 总决赛你的队友")

    def test_quoted_bare_link_keeps_the_quoted_url(self):
        """Quoting a pasted URL must hand the model that URL.

        Reported live: a member quoted a bare link and asked "分析下这篇",
        and the bot answered "引用里没带链接啊" — the quoted ``type=1`` payload
        (the link itself) was dropped, so it was telling the truth.
        """
        from src.media.appmsg import reduce_appmsg

        xml = (
            '<msg><appmsg><title>@Dobby 分析下这篇文章的内容</title>'
            '<type>57</type>'
            '<refermsg><type>1</type><displayname>Eki</displayname>'
            f'<content>{_LINK}</content>'
            '</refermsg></appmsg></msg>'
        )
        reduced = reduce_appmsg(xml)
        self.assertTrue(reduced.startswith("[引用] @Dobby 分析下这篇文章的内容"), reduced)
        self.assertIn("[原文]", reduced)
        self.assertIn(_LINK, reduced)

    def test_quoted_text_with_link_keeps_wording_and_url(self):
        """A quoted sentence with a trailing link keeps both halves."""
        from src.media.appmsg import reduce_appmsg

        xml = (
            '<msg><appmsg><title>这篇讲了啥</title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            f'<content>看这个 {_LINK}</content>'
            '</refermsg></appmsg></msg>'
        )
        reduced = reduce_appmsg(xml)
        self.assertIn("看这个", reduced)
        self.assertIn(_LINK, reduced)

    def test_quoted_text_trailing_punctuation_is_not_part_of_the_url(self):
        """``https://x/a，`` must not carry the Chinese comma into the URL."""
        from src.media.appmsg import reduce_appmsg

        xml = (
            '<msg><appmsg><title>看下</title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            f'<content>{_LINK}，很值得读</content>'
            '</refermsg></appmsg></msg>'
        )
        reduced = reduce_appmsg(xml)
        self.assertIn(_LINK, reduced)
        self.assertNotIn(f"{_LINK}，", reduced)

    def test_quoted_text_without_link_adds_no_empty_label(self):
        """A quoted plain sentence still arrives, just without a URL."""
        from src.media.appmsg import reduce_appmsg

        xml = (
            '<msg><appmsg><title>你说啥</title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            '<content>今天好热</content>'
            '</refermsg></appmsg></msg>'
        )
        self.assertEqual(reduce_appmsg(xml), "[引用] 你说啥 [原文] 今天好热")

    def test_quoted_markup_payload_is_not_treated_as_text(self):
        """A ``type=1`` payload that is really markup must not leak as-is."""
        from src.media.appmsg import reduce_appmsg

        xml = (
            '<msg><appmsg><title>看这个</title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            '<content>&lt;msg&gt;&lt;appmsg&gt;x&lt;/appmsg&gt;&lt;/msg&gt;</content>'
            '</refermsg></appmsg></msg>'
        )
        self.assertEqual(reduce_appmsg(xml), "[引用] 看这个")


class ExtractUrlsTests(unittest.TestCase):
    """Chat text wraps links in punctuation; the path must not absorb it."""

    def test_chinese_punctuation_is_not_part_of_the_url(self):
        from src.media.link_fetcher import extract_urls

        self.assertEqual(
            extract_urls("看看这个 https://x.com/a，讲得不错"),
            ["https://x.com/a"],
        )
        self.assertEqual(
            extract_urls("链接：https://a.com/1。还有 https://b.com/2）"),
            ["https://a.com/1", "https://b.com/2"],
        )

    def test_bare_url_in_parens(self):
        from src.media.link_fetcher import extract_urls

        self.assertEqual(extract_urls("（https://c.com/x）"), ["https://c.com/x"])

    def test_duplicates_collapse_and_order_is_kept(self):
        from src.media.link_fetcher import extract_urls

        self.assertEqual(
            extract_urls("https://a.com/1 https://b.com/2 https://a.com/1"),
            ["https://a.com/1", "https://b.com/2"],
        )

    def test_no_link_yields_nothing(self):
        from src.media.link_fetcher import extract_urls

        self.assertEqual(extract_urls("今天天气不错"), [])
        self.assertEqual(extract_urls(""), [])


class SsrfGuardTests(unittest.TestCase):
    """A group member picks the URL, so anything internal must be refused.

    ``resolve=False`` keeps this offline; name-based cases are covered by
    :class:`SsrfDnsTests`.
    """

    def test_internal_targets_are_refused(self):
        from src.media.link_fetcher import is_public_url

        for url in (
            "http://127.0.0.1/admin",
            "http://127.0.0.1:8000/api/status",
            "http://localhost/",
            "http://192.168.1.1/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",   # cloud metadata
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
            "http://255.255.255.255/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url, resolve=False))

    def test_non_http_schemes_and_odd_shapes_are_refused(self):
        from src.media.link_fetcher import is_public_url

        for url in (
            "file:///C:/Windows/win.ini",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "http://user:pass@example.com/",   # credentials in the URL
            "http://example.com:22/",          # service port, not a web page
            "http://example.com:6379/",
            "http://intranet/",                # single-label host
            "http://router.local/",
            "http://printer.internal/",
            "https://",                        # no host at all
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_public_url(url, resolve=False))

    def test_overlong_url_is_refused(self):
        from src.media.link_fetcher import is_public_url

        self.assertFalse(
            is_public_url("https://a.com/" + "x" * 3000, resolve=False)
        )

    def test_normal_public_links_pass(self):
        from src.media.link_fetcher import is_public_url

        for url in (
            _LINK,
            "http://example.com/a?b=1#x",
            "https://example.com:443/path",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_public_url(url, resolve=False))


class SsrfDnsTests(unittest.TestCase):
    """A hostname that resolves inward must be refused like a literal IP."""

    def test_localhost_names_need_no_lookup(self):
        from src.media import link_fetcher

        # resolve=True path, but these never reach DNS.
        for url in ("http://localhost/", "http://ip6-localhost/",
                    "http://metadata.google.internal/"):
            with self.subTest(url=url):
                self.assertFalse(link_fetcher.is_public_url(url))

    def test_host_resolving_to_loopback_is_refused(self):
        from src.media import link_fetcher

        original = link_fetcher.socket.getaddrinfo

        def _fake(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        link_fetcher.socket.getaddrinfo = _fake
        try:
            self.assertFalse(link_fetcher.is_public_url("http://evil.example.com/"))
        finally:
            link_fetcher.socket.getaddrinfo = original

    def test_mixed_resolution_is_refused(self):
        """One private address among several is enough to refuse."""
        from src.media import link_fetcher

        original = link_fetcher.socket.getaddrinfo

        def _fake(host, port, *args, **kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("10.0.0.7", 0)),
            ]

        link_fetcher.socket.getaddrinfo = _fake
        try:
            self.assertFalse(link_fetcher.is_public_url("http://split.example.com/"))
        finally:
            link_fetcher.socket.getaddrinfo = original

    def test_public_resolution_passes(self):
        from src.media import link_fetcher

        original = link_fetcher.socket.getaddrinfo

        def _fake(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        link_fetcher.socket.getaddrinfo = _fake
        try:
            self.assertTrue(link_fetcher.is_public_url("https://example.com/"))
        finally:
            link_fetcher.socket.getaddrinfo = original

    def test_unresolvable_host_is_refused_not_raised(self):
        from src.media import link_fetcher

        original = link_fetcher.socket.getaddrinfo

        def _fake(host, port, *args, **kwargs):
            raise OSError("no such host")

        link_fetcher.socket.getaddrinfo = _fake
        try:
            self.assertFalse(link_fetcher.is_public_url("https://nope.invalid/"))
        finally:
            link_fetcher.socket.getaddrinfo = original


class RedirectGuardTests(unittest.TestCase):
    """A public URL may 302 to the loopback — every hop is re-checked."""

    def _handle(self):
        from src.media.link_fetcher import _GuardedRedirectHandler

        return _GuardedRedirectHandler()

    def test_redirect_to_internal_host_is_blocked(self):
        import urllib.error

        for target in ("http://127.0.0.1/admin", "http://192.168.1.1/",
                       "http://169.254.169.254/latest/meta-data/"):
            with self.subTest(target=target):
                with self.assertRaises(urllib.error.HTTPError):
                    self._handle().redirect_request(
                        None, None, 302, "Found", {}, target,
                    )

    def test_redirect_chain_is_capped(self):
        import urllib.error
        import urllib.request

        from src.media import link_fetcher

        handler = self._handle()
        request = urllib.request.Request("https://example.com/start")
        original = link_fetcher.is_public_url
        link_fetcher.is_public_url = lambda url, **kw: True
        try:
            for _ in range(link_fetcher._MAX_REDIRECTS):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "https://example.com/next",
                )
            with self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "https://example.com/next",
                )
        finally:
            link_fetcher.is_public_url = original


class ExtractArticleTests(unittest.TestCase):
    """Page HTML → title + prose, with scripts/styles/nav dropped."""

    def test_article_container_wins_over_page_noise(self):
        from src.media.link_fetcher import extract_article

        title, text = extract_article(_ARTICLE_HTML)
        self.assertEqual(title, "DeepSeek 发布新模型")  # og:title preferred
        self.assertIn("性能提升 30%", text)
        self.assertIn("推理成本明显下降", text)
        # Nav/footer/style/script are noise, not article text.
        for noise in ("导航", "版权所有", "color: red", "tracking"):
            self.assertNotIn(noise, text)

    def test_page_without_article_container_still_reads(self):
        from src.media.link_fetcher import extract_article

        html = (
            "<html><head><title>普通文章</title></head>"
            "<body><h1>标题</h1><p>正文内容在这里。</p></body></html>"
        )
        title, text = extract_article(html)
        self.assertEqual(title, "普通文章")
        self.assertIn("正文内容在这里。", text)

    def test_empty_and_data_bodies_yield_nothing(self):
        from src.media.link_fetcher import extract_article

        self.assertEqual(extract_article(""), ("", ""))
        # A JSON blob is not an article — pasting it into the prompt helps
        # nobody, so it must fail rather than "succeed" with junk.
        self.assertEqual(extract_article('{"code": 0, "data": []}'), ("", ""))

    def test_unclosed_tag_does_not_hang(self):
        """Malformed markup is expected; extraction must still terminate."""
        from src.media.link_fetcher import extract_article

        title, text = extract_article("<html><body><p>只有一段<div><p>没闭合")
        self.assertIn("只有一段", text)

    def test_long_body_is_truncated(self):
        from src.media.link_fetcher import _MAX_TEXT, extract_article

        html = "<html><body><div id='js_content'><p>" + ("字" * 5000) + "</p></div></body></html>"
        _, text = extract_article(html)
        self.assertLessEqual(len(text), _MAX_TEXT)


class FetchForQuestionTests(unittest.TestCase):
    """Batch fetching is bounded; a failure never escapes."""

    def setUp(self):
        from src.media import link_fetcher

        self.fetcher = link_fetcher
        self._original = link_fetcher.fetch_article
        link_fetcher.clear_cache()

    def tearDown(self):
        self.fetcher.fetch_article = self._original
        self.fetcher.clear_cache()

    def test_only_the_first_links_are_fetched(self):
        from src.media.link_fetcher import Article

        calls = []

        def _fake(url):
            calls.append(url)
            return Article(url=url, title="t", text="文" * 10)

        self.fetcher.fetch_article = _fake
        articles = self.fetcher.fetch_for_question(
            "https://a.com/1 https://b.com/2 https://c.com/3"
        )
        self.assertEqual(calls, ["https://a.com/1", "https://b.com/2"])
        self.assertEqual(len(articles), 2)

    def test_unreadable_link_is_skipped_not_fatal(self):
        from src.media.link_fetcher import Article

        def _fake(url):
            if "dead" in url:
                return None
            return Article(url=url, title="活着", text="正文")

        self.fetcher.fetch_article = _fake
        articles = self.fetcher.fetch_for_question(
            "https://dead.com/1 https://alive.com/2"
        )
        self.assertEqual([a.url for a in articles], ["https://alive.com/2"])

    def test_no_url_means_no_fetch(self):
        def _explode(url):  # pragma: no cover - must not be called
            raise AssertionError("fetch_article must not run without a URL")

        self.fetcher.fetch_article = _explode
        self.assertEqual(self.fetcher.fetch_for_question("今天天气不错"), [])

    def test_deadline_stops_the_batch(self):
        """Two dead links must not cost two full timeouts."""
        from src.media.link_fetcher import Article

        calls = []

        def _fake(url):
            calls.append(url)
            return Article(url=url, title="t", text="x")

        self.fetcher.fetch_article = _fake
        articles = self.fetcher.fetch_for_question(
            "https://a.com/1 https://b.com/2", budget_sec=-1,
        )
        self.assertEqual(articles, [])
        self.assertEqual(calls, [])


class FormatLinkContextTests(unittest.TestCase):
    """The block must read as content already seen, not as a bare URL."""

    def test_block_is_labelled_as_already_read(self):
        from src.media.link_fetcher import Article, format_link_context

        block = format_link_context([
            Article(url=_LINK, title=_TITLE, text="正文内容。"),
        ])
        self.assertIn("[链接正文]", block)
        self.assertIn(_TITLE, block)
        self.assertIn("正文内容。", block)
        # The instruction is what stops "我打不开链接" from being the answer.
        self.assertIn("不要说你打不开", block)

    def test_nothing_fetched_yields_empty_string(self):
        from src.media.link_fetcher import Article, format_link_context

        self.assertEqual(format_link_context([]), "")
        # An article with no body is not usable content.
        self.assertEqual(
            format_link_context([Article(url=_LINK, title=_TITLE, text="  ")]),
            "",
        )


class PromptInjectionTests(unittest.TestCase):
    """The fetched text has to reach the prompt — that is the whole point."""

    @staticmethod
    def _capture_summarizer(captured):
        from src.summarize.base import AbstractSummarizer

        class _Capture(AbstractSummarizer):
            def _summarize_direct(self, messages, requester_name=""):
                raise NotImplementedError

            def _summarize_chunk(self, chunk, chunk_num, total,
                                requester_name=""):
                raise NotImplementedError

            def _merge_chunk_summaries(self, chunk_summaries,
                                       requester_name=""):
                raise NotImplementedError

            def consolidate_memory(self, existing_memory, new_messages):
                raise NotImplementedError

            def _call_chat_api(self, system_prompt, messages):
                captured["prompt"] = system_prompt
                return "ok"

        return _Capture()

    def test_link_context_lands_in_the_system_prompt(self):
        captured = {}
        summarizer = self._capture_summarizer(captured)
        summarizer.chat(
            message=f"{_LINK} 这个文章讲了啥？",
            requester_name="张三",
            bot_name="Eki",
            group_name="测试群",
            link_context=(
                "对方提到的链接你已经读过了，内容如下（直接据此回答，不要说你打不开）：\n"
                f"[链接正文] {_TITLE} — {_LINK}\n第一段：性能提升 30%。"
            ),
        )
        prompt = captured["prompt"]
        self.assertIn("[链接正文]", prompt)
        self.assertIn("性能提升 30%", prompt)
        # It must sit before the question, so the question reads as asking
        # about text that is already available.
        self.assertLess(prompt.index("[链接正文]"), prompt.index("对方消息："))

    def test_braces_in_article_text_do_not_break_formatting(self):
        """``str.format`` would raise on a stray brace from a web page."""
        captured = {}
        summarizer = self._capture_summarizer(captured)
        summarizer.chat(
            message="看这个 {链接}", requester_name="张三",
            link_context="正文里有 {花括号} 和半截 }",
        )
        self.assertIn("{花括号}", captured["prompt"])

    def test_no_link_context_leaves_no_artefacts(self):
        captured = {}
        summarizer = self._capture_summarizer(captured)
        summarizer.chat(message="你好", requester_name="张三", link_context="")
        prompt = captured["prompt"]
        self.assertNotIn("[链接正文]", prompt)
        self.assertNotIn("\n\n\n\n", prompt)


class RouterLinkContextTests(unittest.TestCase):
    """The live @bot path must gate fetching and never fail the reply."""

    def _router(self, enabled=True):
        from src.router import MessageRouter

        class _Cfg:
            link_fetch_enabled = enabled

        router = MessageRouter.__new__(MessageRouter)
        router._config = _Cfg()
        return router

    def test_disabled_switch_skips_the_network(self):
        from src.media import link_fetcher

        original = link_fetcher.fetch_for_question
        calls = []

        def _explode(text, **kwargs):  # pragma: no cover - must not run
            calls.append(text)
            raise AssertionError("fetching must be skipped when disabled")

        link_fetcher.fetch_for_question = _explode
        try:
            self.assertEqual(self._router(enabled=False)._link_context(_LINK), "")
        finally:
            link_fetcher.fetch_for_question = original
        self.assertEqual(calls, [])

    def test_question_without_link_does_no_fetch(self):
        from src.media import link_fetcher

        original = link_fetcher.fetch_for_question
        link_fetcher.fetch_for_question = lambda text, **kw: []
        try:
            self.assertEqual(self._router()._link_context("今天天气不错"), "")
        finally:
            link_fetcher.fetch_for_question = original

    def test_fetch_failure_does_not_break_the_reply(self):
        """Network trouble degrades to "no article", never to an exception."""
        from src.media import link_fetcher

        original = link_fetcher.fetch_for_question

        def _boom(text, **kwargs):
            raise RuntimeError("network down")

        link_fetcher.fetch_for_question = _boom
        try:
            self.assertEqual(self._router()._link_context(_LINK), "")
        finally:
            link_fetcher.fetch_for_question = original


class ConfigSwitchTests(unittest.TestCase):
    def test_link_fetch_defaults_on(self):
        """Off by default would restore the reported bug silently."""
        import importlib
        import os

        import src.config as config_module

        saved = os.environ.pop("LINK_FETCH_ENABLED", None)
        try:
            importlib.reload(config_module)
            self.assertTrue(config_module.load_config().link_fetch_enabled)
        finally:
            if saved is not None:
                os.environ["LINK_FETCH_ENABLED"] = saved
            else:
                os.environ.pop("LINK_FETCH_ENABLED", None)
            importlib.reload(config_module)

    def test_switch_can_be_turned_off(self):
        import importlib
        import os

        import src.config as config_module

        saved = os.environ.get("LINK_FETCH_ENABLED")
        os.environ["LINK_FETCH_ENABLED"] = "false"
        try:
            importlib.reload(config_module)
            self.assertFalse(config_module.load_config().link_fetch_enabled)
        finally:
            if saved is not None:
                os.environ["LINK_FETCH_ENABLED"] = saved
            else:
                os.environ.pop("LINK_FETCH_ENABLED", None)
            importlib.reload(config_module)


class TlsRepairTests(unittest.TestCase):
    """The build must not depend on the machine's cached intermediates.

    Measured failure this covers: ``matrix67.com`` sends the leaf certificate
    only (``CN=matrix67.com``, issued by ``CN=YR2``).  The dev box happened to
    have ``YR2`` cached in Windows' store, so fetching worked there; the VM had
    a clean store, the handshake died with "unable to get local issuer
    certificate", and the bot answered "我打不开链接".

    All offline: the network is not touched, only the decision logic that
    surrounds it.
    """

    def test_urllib_wrapped_tls_error_is_recognised(self):
        """urllib buries an SSLError in URLError.reason; that must still count."""
        import ssl
        import urllib.error

        from src.media.link_fetcher import _is_tls_error

        cause = ssl.SSLCertVerificationError("unable to get local issuer certificate")
        wrapped = urllib.error.URLError(cause)
        self.assertTrue(_is_tls_error(wrapped))

    def test_plain_network_error_is_not_a_tls_error(self):
        """A DNS failure must not trigger chain repair or a plaintext retry."""
        import urllib.error

        from src.media.link_fetcher import _is_tls_error

        self.assertFalse(_is_tls_error(urllib.error.URLError("getaddrinfo failed")))
        self.assertFalse(_is_tls_error(TimeoutError("timed out")))

    def test_http_fallback_keeps_host_and_path(self):
        """Only the scheme changes — the fallback cannot reach a new target."""
        from src.media.link_fetcher import _http_fallback

        self.assertEqual(
            _http_fallback("https://matrix67.com/blog/archives/336?p=1"),
            "http://matrix67.com/blog/archives/336?p=1",
        )

    def test_http_fallback_ignores_http_urls(self):
        from src.media.link_fetcher import _http_fallback

        self.assertEqual(_http_fallback("http://example.com/a"), "")

    def test_trust_anchors_exclude_intermediates(self):
        """An intermediate accepted as an anchor ends the repair one hop early.

        Windows' store caches intermediates, so without this filter the dev box
        "verified" a chain containing just ``YR2`` while the same chain still
        failed the actual handshake — the repair silently did nothing.
        """
        from src.media.link_fetcher import _trust_anchors

        anchors = _trust_anchors()
        self.assertTrue(anchors, "no trust anchors at all: TLS could never work")
        for cert in anchors:
            self.assertEqual(
                cert.subject, cert.issuer,
                msg=f"non-root in the anchor set: {cert.subject.rfc4514_string()}",
            )

    def test_failed_repair_is_cached_not_retried(self):
        """An unreachable chain must not be re-walked on every mention."""
        from src.media import link_fetcher

        link_fetcher._intermediate_cache.clear()
        try:
            # Pre-seeded failure: the function must return it without doing
            # any network work, which is what makes a dead link cheap.
            link_fetcher._intermediate_cache["dead.example"] = ""
            self.assertEqual(link_fetcher._verified_intermediates("dead.example"), "")
        finally:
            link_fetcher._intermediate_cache.clear()


class FetchAttemptTests(unittest.TestCase):
    """fetch_article's escalation order, exercised without a network."""

    def setUp(self):
        from src.media import link_fetcher

        self.fetcher = link_fetcher
        link_fetcher.clear_cache()
        link_fetcher._intermediate_cache.clear()

    def tearDown(self):
        self.fetcher.clear_cache()
        self.fetcher._intermediate_cache.clear()

    def _patch(self, fake):
        original = self.fetcher._fetch_once
        self.fetcher._fetch_once = fake
        self.addCleanup(setattr, self.fetcher, "_fetch_once", original)
        original_is_public = self.fetcher.is_public_url
        self.fetcher.is_public_url = lambda url, **kw: True
        self.addCleanup(setattr, self.fetcher, "is_public_url", original_is_public)

    def test_repaired_retry_succeeds_without_plaintext(self):
        """A TLS failure triggers one repaired retry; success stops there."""
        import ssl

        from src.media.link_fetcher import Article

        calls = []

        def _fake(url, *, host=""):
            calls.append(host)
            if not host:
                return None, ssl.SSLCertVerificationError("unable to get local issuer")
            return Article(url=url, title="t", text="正文"), None

        self._patch(_fake)
        article = self.fetcher.fetch_article("https://matrix67.com/blog/archives/336")
        self.assertIsNotNone(article)
        self.assertEqual(article.text, "正文")
        # Plain attempt, then exactly one repaired attempt — no http fallback.
        self.assertEqual(calls, ["", "matrix67.com"])

    def test_plaintext_fallback_after_failed_repair(self):
        """Both HTTPS attempts failing still reads the page over http."""
        import ssl

        from src.media.link_fetcher import Article

        urls = []

        def _fake(url, *, host=""):
            urls.append(url)
            if url.startswith("https://"):
                return None, ssl.SSLCertVerificationError("unable to get local issuer")
            return Article(url=url, title="t", text="明文正文"), None

        self._patch(_fake)
        article = self.fetcher.fetch_article("https://matrix67.com/blog/archives/336")
        self.assertIsNotNone(article)
        self.assertEqual(
            urls,
            [
                "https://matrix67.com/blog/archives/336",
                "https://matrix67.com/blog/archives/336",
                "http://matrix67.com/blog/archives/336",
            ],
        )

    def test_dns_failure_does_not_degrade_to_plaintext(self):
        """Only a TLS problem justifies reading the page unauthenticated."""
        import urllib.error

        calls = []

        def _fake(url, *, host=""):
            calls.append(url)
            return None, urllib.error.URLError("getaddrinfo failed")

        self._patch(_fake)
        self.assertIsNone(
            self.fetcher.fetch_article("https://matrix67.com/blog/archives/336")
        )
        self.assertEqual(calls, ["https://matrix67.com/blog/archives/336"])

    def test_empty_page_does_not_trigger_repair(self):
        """A 200 with nothing readable is not a TLS problem."""
        calls = []

        def _fake(url, *, host=""):
            calls.append(url)
            return None, None

        self._patch(_fake)
        self.assertIsNone(
            self.fetcher.fetch_article("https://matrix67.com/blog/archives/336")
        )
        self.assertEqual(calls, ["https://matrix67.com/blog/archives/336"])


class _FakeHeaders:
    def __init__(self, mapping):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key, default=""):
        return self._m.get(key.lower(), default)

    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    def __init__(self, body: str, content_type="text/html"):
        self._raw = body.encode("utf-8")
        self.headers = _FakeHeaders({
            "Content-Type": content_type,
            "Content-Encoding": "identity",
        })

    def read(self, n=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BlockedPageTests(unittest.TestCase):
    """A verification wall or page furniture must not read as an article.

    Reported live: asking about a shared WeChat article produced the reply
    "环境异常，完成验证后即可继续访问…" — because the anti-bot page was
    extracted, injected under "你已经读过了", and summarised.  Saying the link
    could not be read is the honest answer and the one the model can act on.
    """

    def setUp(self):
        from src.media import link_fetcher

        self.fetcher = link_fetcher
        link_fetcher.clear_cache()

    def tearDown(self):
        self.fetcher.clear_cache()

    def _serve(self, page: str):
        original = self.fetcher._build_opener
        self.fetcher._build_opener = lambda host="": type(
            "Opener", (), {"open": lambda *a, **k: _FakeResponse(page)}
        )()
        self.addCleanup(setattr, self.fetcher, "_build_opener", original)

    def test_wechat_verification_wall_is_a_failure(self):
        self._serve(
            "<html><head><title></title></head><body>"
            "<div>环境异常</div><div>当前环境异常，完成验证后即可继续访问。</div>"
            "<a>去验证</a></body></html>"
        )
        self.assertIsNone(self.fetcher.fetch_article("https://mp.weixin.qq.com/s/abc"))

    def test_deleted_page_notice_is_a_failure(self):
        self._serve(
            "<html><head><title>该内容已被发布者删除</title></head>"
            "<body><p>该内容已被发布者删除</p></body></html>"
        )
        self.assertIsNone(self.fetcher.fetch_article("https://mp.weixin.qq.com/s/abc"))

    def test_footer_only_page_is_a_failure(self):
        """A 200 with a few dozen characters of footer is not an article."""
        self._serve(
            "<html><head><title>某站</title></head><body>"
            "<footer>© 2014-2026 某公司 地址：某路 电话：9501-3888</footer>"
            "</body></html>"
        )
        self.assertIsNone(self.fetcher.fetch_article("https://example.com/gone"))

    def test_real_article_still_reads(self):
        """The guard must not reject a page that is a real article."""
        self._serve(
            "<html><head><title>一篇真文章</title></head><body>"
            "<div id='js_content'>"
            "<p>第一段：这篇文章讲的是模型推理成本的变化，性能提升 30%。</p>"
            "<p>第二段：推理成本明显下降，同时延迟也更稳定，适合在群里做实时回复。</p>"
            "<p>第三段：文章还讨论了部署时需要注意的几个细节和常见坑。</p>"
            "</div></body></html>"
        )
        article = self.fetcher.fetch_article("https://matrix67.com/blog/archives/336")
        self.assertIsNotNone(article)
        self.assertIn("性能提升 30%", article.text)

    def test_marker_deep_in_a_long_body_is_not_a_wall(self):
        body = "正文。" * 400
        plain = self.fetcher._blocked_marker("标题", body + "完成验证")
        self.assertEqual(plain, "")
        wall = self.fetcher._blocked_marker("", "环境异常 " + body)
        self.assertEqual(wall, "环境异常")


class ScriptOnlyShellTests(unittest.TestCase):
    """A JavaScript shell still carries the note in its meta tags."""

    def test_og_description_is_the_body_when_there_is_no_prose(self):
        from src.media.link_fetcher import extract_article

        html = (
            '<html><head><title>小红书 - 你的生活兴趣社区</title>'
            '<meta property="og:title" content="GTA6教会我最重要的一件事就是 - 小红书">'
            '<meta property="og:description" content="'
            '一段关系里对方的人格底色很重要。当外界环境极度高压、常规道德和'
            '社会规则统统失效时，一个人最真实的人格底色就会彻底暴露出来。'
            '好的搭档，是一起找出路，不是一起往深渊加速。">'
            '</head><body><div id="app"></div><script>render()</script></body></html>'
        )
        title, text = extract_article(html)
        self.assertEqual(title, "GTA6教会我最重要的一件事就是 - 小红书")
        self.assertIn("人格底色很重要", text)
        self.assertIn("一起找出路", text)

    def test_real_prose_beats_og_description(self):
        """The meta summary must not replace an article we can actually read."""
        from src.media.link_fetcher import extract_article

        html = (
            "<html><head>"
            '<meta property="og:description" content="这是站点摘要，不是正文。">'
            "</head><body><p>真正的正文第一段，讲的是性能提升 30%。</p></body></html>"
        )
        _, text = extract_article(html)
        self.assertIn("真正的正文第一段", text)
        self.assertNotIn("站点摘要", text)


class MobileRetryTests(unittest.TestCase):
    """Some sites serve a shell to desktops and the content to phones.

    Measured on a Xiaohongshu video share: the desktop request answered 37 KB
    with no title and no body, the iPhone request answered 71 KB with both.
    Treating the first answer as final is what made the bot say it could not
    see the video at all.
    """

    _SHELL = (
        '<html><head><title>小红书 - 你的生活兴趣社区</title></head>'
        '<body><div id="app"></div><script>render()</script></body></html>'
    )
    _MOBILE = (
        '<html><head><title>GTA6教会我最重要的一件事就是 - 小红书</title>'
        '<meta property="og:description" content="'
        '一段关系里对方的人格底色很重要。当外界环境极度高压、常规道德和'
        '社会规则统统失效时，一个人最真实的人格底色就会彻底暴露出来。'
        '好的搭档，是一起找出路，不是一起往深渊加速。">'
        '</head><body></body></html>'
    )
    _URL = "https://www.xiaohongshu.com/discovery/item/6b0348dc000000001b01c79d"

    def setUp(self):
        from src.media import link_fetcher

        self.fetcher = link_fetcher
        link_fetcher.clear_cache()
        self.addCleanup(link_fetcher.clear_cache)
        original_public = link_fetcher.is_public_url
        link_fetcher.is_public_url = lambda url, **kw: True
        self.addCleanup(setattr, link_fetcher, "is_public_url", original_public)

    def _serve(self, pages):
        """Serve *pages* keyed by User-Agent and record which were asked."""
        asked = []
        original = self.fetcher._request_page

        def _fake(url, *, ua, timeout, host=""):
            asked.append(ua)
            page = pages[ua]
            return page, len(page), "text/html; charset=utf-8"

        self.fetcher._request_page = _fake
        self.addCleanup(setattr, self.fetcher, "_request_page", original)
        return asked

    def test_desktop_shell_is_retried_as_a_phone(self):
        asked = self._serve({
            self.fetcher._UA: self._SHELL,
            self.fetcher._MOBILE_UA: self._MOBILE,
        })
        article = self.fetcher.fetch_article(self._URL)
        self.assertIsNotNone(article)
        self.assertIn("人格底色很重要", article.text)
        self.assertEqual(asked, [self.fetcher._UA, self.fetcher._MOBILE_UA])

    def test_readable_desktop_page_costs_one_request(self):
        asked = self._serve({
            self.fetcher._UA: (
                "<html><head><title>真文章</title></head><body><div id='js_content'>"
                "<p>第一段：这篇文章讲的是推理成本的变化，性能提升 30%，延迟更稳定，"
                "适合在群里做实时回复。</p>"
                "<p>第二段：部署时需要注意证书链、超时、编码和内存占用这几个常见坑，"
                "否则线上会悄悄失败。</p>"
                "</div></body></html>"
            ),
            self.fetcher._MOBILE_UA: self._MOBILE,
        })
        article = self.fetcher.fetch_article(self._URL)
        self.assertIsNotNone(article)
        self.assertEqual(asked, [self.fetcher._UA])

    def test_mobile_retry_that_is_also_empty_still_fails_honestly(self):
        asked = self._serve({
            self.fetcher._UA: self._SHELL,
            self.fetcher._MOBILE_UA: self._SHELL,
        })
        self.assertIsNone(self.fetcher.fetch_article(self._URL))
        self.assertEqual(len(asked), 2)


class VideoOnlyPageTests(unittest.TestCase):
    """A clip with almost no prose must still reach the frame sampler.

    Reported live: the bot answered "就看得到标题，视频本身加载不出来" for a
    note whose caption is one line.  ``_judge_page`` used to reject any page
    under ``_MIN_USABLE_TEXT`` as a stub, which threw the media address away
    along with the (nonexistent) article body — so no frames were ever
    sampled.  A page that names a video is now kept as a video-only Article.
    """

    _URL = "https://www.xiaohongshu.com/discovery/item/6b0348dc000000001b01c79d"
    _PAGE = (
        "<html><head><title>随手拍的一段</title></head><body>"
        "<p>今天随手拍的一段</p>"
        '<script>window.__INITIAL_STATE__ = {"masterUrl":'
        ' "https:\\u002F\\u002Fcdn.example.com\\u002Fa.mp4?sign=1"}'
        '</script></body></html>'
    )

    def setUp(self):
        from src.media import link_fetcher

        self.fetcher = link_fetcher
        link_fetcher.clear_cache()
        self.addCleanup(link_fetcher.clear_cache)
        original_public = link_fetcher.is_public_url
        link_fetcher.is_public_url = lambda url, **kw: True
        self.addCleanup(setattr, link_fetcher, "is_public_url", original_public)

    def _serve(self, page: str):
        original = self.fetcher._build_opener
        self.fetcher._build_opener = lambda host="": type(
            "Opener", (), {"open": lambda *a, **k: _FakeResponse(page)}
        )()
        self.addCleanup(setattr, self.fetcher, "_build_opener", original)

    def test_short_caption_with_a_video_address_is_kept(self):
        self._serve(self._PAGE)
        article = self.fetcher.fetch_article(self._URL)
        self.assertIsNotNone(article)
        self.assertEqual(article.title, "随手拍的一段")
        # No prose means no [链接正文] block; the frames are the whole answer.
        self.assertEqual(article.text, "")
        self.assertEqual(
            article.video_urls, ("https://cdn.example.com/a.mp4?sign=1",)
        )

    def test_short_page_without_a_video_is_still_a_stub(self):
        self._serve(
            "<html><head><title>某站</title></head><body><p>短。</p></body></html>"
        )
        self.assertIsNone(self.fetcher.fetch_article(self._URL))


class PromptDocumentationTests(unittest.TestCase):
    """The prompt must mention the block the code now inserts."""

    def test_chat_prompt_has_the_link_slot(self):
        from src.summarize.base import AbstractSummarizer

        self.assertIn("{link_section}", AbstractSummarizer.CHAT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
