"""Rendering a script-built page so the video address is actually there.

Reported need: a shared video page whose player (and therefore its media
address) is created by JavaScript.  ``urllib`` gets the empty shell, so
``link_fetcher`` finds no ``<video src>`` and the reply degrades to "看不到".
A headless Chromium executes the script and ``--dump-dom`` hands back the
finished document.

No browser is started here: ``_run_browser`` is replaced, so the tests cover
the locating, the argument shape, the fallback spelling, the truncation and
the profile cleanup without depending on what this machine has installed.
"""

import os
import unittest
from pathlib import Path
from unittest import mock


class _BrowserFinderTests(unittest.TestCase):
    """Locating the browser, and caching the answer."""

    def setUp(self):
        from src.media import browser_render

        self.module = browser_render
        browser_render.reset_browser_cache()
        self.addCleanup(browser_render.reset_browser_cache)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_override_wins_when_it_exists(self):
        """``WEBOT_BROWSER_PATH`` is honoured, so a portable install works."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "msedge.exe"
            exe.write_bytes(b"stub")
            os.environ["WEBOT_BROWSER_PATH"] = str(exe)
            self.assertEqual(self.module.browser_path(), str(exe))
            self.assertTrue(self.module.available())

    def test_missing_override_falls_through_to_the_search(self):
        """A stale override must not make rendering unavailable forever."""
        os.environ["WEBOT_BROWSER_PATH"] = r"C:\does\not\exist\msedge.exe"
        found = Path(r"C:\real\msedge.exe")
        with mock.patch.object(
            self.module, "_candidate_browsers", return_value=[found],
        ), mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(self.module.browser_path(), str(found))

    def test_no_browser_means_unavailable_not_an_error(self):
        """Without Edge/Chrome the caller keeps its static behaviour."""
        with mock.patch.object(self.module, "_candidate_browsers", return_value=[]):
            self.assertIsNone(self.module.browser_path())
            self.assertFalse(self.module.available())
            # And rendering is a quiet None, not a raise.
            self.assertIsNone(self.module.render_html("https://example.com/x"))

    def test_lookup_is_cached(self):
        """Located once: the candidate list does not change mid-run."""
        found = Path(r"C:\real\msedge.exe")
        with mock.patch.object(
            self.module, "_candidate_browsers", return_value=[found],
        ) as candidates, mock.patch.object(Path, "is_file", return_value=True):
            self.module.browser_path()
            first = candidates.call_count
            self.module.browser_path()
        self.assertEqual(candidates.call_count, first)

    def test_candidates_prefer_edge_on_windows(self):
        """Edge ships with Windows, so it is the one to rely on."""
        if os.name != "nt":
            self.skipTest("Windows-specific ordering")
        candidates = [c.name.lower() for c in self.module._candidate_browsers()]
        self.assertIn("msedge.exe", candidates)
        if "chrome.exe" in candidates:
            self.assertLess(
                candidates.index("msedge.exe"), candidates.index("chrome.exe"),
            )


class _RenderHtmlTests(unittest.TestCase):
    """What ``render_html`` asks the browser for, and what it gives back."""

    _URL = "https://www.bilibili.com/video/BV1xx411c7mD"

    def setUp(self):
        from src.media import browser_render

        self.module = browser_render
        browser_render.reset_browser_cache()
        self.addCleanup(browser_render.reset_browser_cache)
        patcher = mock.patch.object(
            browser_render, "browser_path", return_value=r"C:\edge\msedge.exe",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub_run(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        """Replace the browser run and record every call it receives."""
        calls = []

        def _fake(exe, args, profile, timeout):
            calls.append({"exe": exe, "args": list(args),
                          "profile": Path(profile), "timeout": timeout})
            return stdout, returncode, stderr

        patcher = mock.patch.object(self.module, "_run_browser", side_effect=_fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_script_built_markup_comes_back(self):
        """The whole point: the DOM is what the script produced."""
        dom = (
            '<html><body><div id="app">'
            '<video src="https://cdn.example.com/a.mp4?sign=1"></video>'
            "</div></body></html>"
        )
        self._stub_run(dom.encode("utf-8"))
        self.assertEqual(self.module.render_html(self._URL), dom)

    def test_arguments_ask_for_the_dom_after_a_bounded_wait(self):
        """``--dump-dom`` plus a virtual time budget, in a throwaway profile."""
        calls = self._stub_run(b"<html></html>")
        self.module.render_html(self._URL, timeout=7.5, budget_ms=1234)
        self.assertEqual(len(calls), 1)
        args = calls[0]["args"]
        self.assertIn("--headless=new", args)
        self.assertIn("--dump-dom", args)
        self.assertIn("--virtual-time-budget=1234", args)
        self.assertEqual(args[-1], self._URL)
        self.assertEqual(calls[0]["timeout"], 7.5)
        # A fresh profile per call: a shared one would attach to a running
        # instance and dump nothing.
        self.assertTrue(calls[0]["profile"].name.startswith("webot_render_"))

    def test_non_http_scheme_never_starts_a_browser(self):
        for url in ("file:///C:/Windows/System32/drivers/etc/hosts",
                    "ftp://example.com/a.mp4", "", "javascript:alert(1)"):
            calls = self._stub_run(b"<html></html>")
            self.assertIsNone(self.module.render_html(url), url)
            self.assertEqual(calls, [], url)

    def test_empty_output_is_none(self):
        """A run that printed nothing is a failure, not an empty page."""
        self._stub_run(b"   \n")
        self.assertIsNone(self.module.render_html(self._URL))

    def test_oversized_dom_is_truncated(self):
        """A hostile page cannot make the renderer hold unbounded text."""
        huge = b"<html>" + b"x" * (self.module._MAX_DOM_BYTES + 5000)
        self._stub_run(huge)
        dom = self.module.render_html(self._URL)
        self.assertIsNotNone(dom)
        self.assertEqual(len(dom), self.module._MAX_DOM_BYTES)

    def test_legacy_headless_spelling_is_retried(self):
        """Chromium < 112 rejects ``--headless=new``; one retry fixes it."""
        calls = []

        def _fake(exe, args, profile, timeout):
            calls.append(list(args))
            if "--headless=new" in args:
                return b"", 1  # newer spelling unsupported
            return b"<html><video src='a.mp4'></video></html>", 0

        with mock.patch.object(self.module, "_run_browser", side_effect=_fake):
            dom = self.module.render_html(self._URL)
        self.assertIsNotNone(dom)
        self.assertIn("a.mp4", dom)
        self.assertEqual(len(calls), 2)
        self.assertIn("--headless", calls[1])
        self.assertNotIn("--headless=new", calls[1])

    def test_no_retry_when_the_first_run_succeeded(self):
        """A working browser must not be started twice."""
        calls = self._stub_run(b"<html>ok</html>")
        self.module.render_html(self._URL)
        self.assertEqual(len(calls), 1)

    def test_silent_zero_exit_is_still_retried(self):
        """Measured in the VM: exit 0 with empty stdout, in about a second.

        The old retry test was ``returncode != 0``, so this exact shape — a
        browser that ignored ``--headless=new`` and printed nothing — skipped
        the legacy-spelling fallback and returned None.  Output is the only
        thing the caller can use, so an empty run is a failure regardless of
        exit status.
        """
        calls = []

        def _fake(exe, args, profile, timeout):
            calls.append(list(args))
            if "--headless=new" in args:
                return b"", 0  # exited cleanly, said nothing
            return b"<html><video src='a.mp4'></video></html>", 0, b""

        with mock.patch.object(self.module, "_run_browser", side_effect=_fake):
            dom = self.module.render_html(self._URL)
        self.assertIsNotNone(dom)
        self.assertIn("a.mp4", dom)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--headless=new", calls[1])

    def test_stderr_of_the_failed_spelling_is_logged(self):
        """The failure log has to carry what the browser complained about."""
        message = b"[ERROR] unknown flag --headless=new\r\n"

        def _fake(exe, args, profile, timeout):
            if "--headless=new" in args:
                return b"", 0, message
            return b"", 0, message

        with mock.patch.object(self.module, "_run_browser", side_effect=_fake):
            with self.assertLogs(self.module.logger, level="INFO") as logs:
                self.assertIsNone(self.module.render_html(self._URL))
        text = "\n".join(logs.output)
        self.assertIn("unknown flag --headless=new", text)

    def test_stderr_is_absent_from_the_log_when_the_browser_said_nothing(self):
        self._stub_run(b"", returncode=0)
        with self.assertLogs(self.module.logger, level="WARNING") as logs:
            self.assertIsNone(self.module.render_html(self._URL))
        self.assertNotIn(": ", logs.output[-1].split("(exit 0)")[-1])

    def test_timeout_is_a_quiet_none(self):
        """A hung page degrades to "no frames", not a stuck reply path."""
        with mock.patch.object(self.module, "_run_browser", return_value=None):
            self.assertIsNone(self.module.render_html(self._URL))

    def test_temporary_profile_is_removed(self):
        """One profile per render; leaking them would fill the temp dir."""
        calls = self._stub_run(b"<html>ok</html>")
        self.module.render_html(self._URL)
        profile = calls[0]["profile"]
        self.assertFalse(profile.exists(), f"profile left behind: {profile}")

    def test_profile_is_removed_even_when_the_run_fails(self):
        calls = self._stub_run(b"", returncode=2)
        self.module.render_html(self._URL)
        self.assertTrue(calls)
        for call in calls:
            self.assertFalse(call["profile"].exists())

    def test_no_console_window(self):
        """The packaged bot has no console; a flash per render is a bug.

        Asserted on the real ``_run_browser`` by capturing the kwargs it
        builds, rather than by launching a browser.
        """
        captured = {}

        class _Proc:
            stdout = b"<html>ok</html>"
            returncode = 0
            stderr = b""

        def _fake_run(command, **kwargs):
            captured.update(kwargs)
            captured["command"] = command
            return _Proc()

        import tempfile

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module.subprocess, "run", side_effect=_fake_run,
        ):
            self.module._run_browser(
                r"C:\edge\msedge.exe", ["--headless=new"], Path(tmp), 5.0,
            )
        self.assertIn("creationflags", captured)
        self.assertTrue(captured["creationflags"])
        self.assertEqual(captured["timeout"], 5.0)
        self.assertIn(f"--user-data-dir={Path(tmp)}", captured["command"])


class _ScriptedPageEndToEndTests(unittest.TestCase):
    """The reported case, wired through ``link_fetcher``.

    Static fetch sees an empty shell; the renderer sees the finished page
    carrying a ``<video>``; the address reaches ``Article.video_urls`` so the
    frame pipeline can use it.
    """

    SHELL = (
        "<html><head><title>某站</title></head>"
        "<body><div id='app'></div><script>hydrate()</script></body></html>"
    )
    RENDERED = (
        "<html><head><title>随手拍的一段 - 某站</title>"
        '<meta property="og:description" content="'
        '今天在海边随手拍的一段，浪很大，风也很大，站都站不稳。">'
        "</head><body><div id='app'>"
        '<video src="https://cdn.example.com/a.mp4?sign=1"></video>'
        "</div></body></html>"
    )

    def _install(self):
        from src.media import link_fetcher

        shell = self.SHELL

        def _fake_request(url, *, ua, timeout, host=""):
            return shell, len(shell), "text/html; charset=utf-8"

        originals = {
            "_request_page": link_fetcher._request_page,
            "is_public_url": link_fetcher.is_public_url,
        }
        link_fetcher._request_page = _fake_request
        link_fetcher.is_public_url = lambda url, **kw: True
        for name, original in originals.items():
            self.addCleanup(setattr, link_fetcher, name, original)
        link_fetcher.clear_cache()
        self.addCleanup(link_fetcher.clear_cache)
        return link_fetcher

    def test_rendered_page_yields_its_video_address(self):
        """The script-injected <video> is what makes frames possible."""
        link_fetcher = self._install()
        rendered = self.RENDERED

        with mock.patch.object(
            link_fetcher, "_render_page",
            return_value=link_fetcher._judge_page(
                "https://example.com/note", rendered,
                raw_len=len(rendered), content_type="text/html",
            )[0],
        ):
            article = link_fetcher.fetch_article(
                "https://example.com/note", render=True,
            )

        self.assertIsNotNone(article)
        self.assertEqual(
            article.video_urls, ("https://cdn.example.com/a.mp4?sign=1",),
        )
        self.assertEqual(article.title, "随手拍的一段 - 某站")

    def test_render_result_is_judged_not_trusted(self):
        """A rendered wall is still a wall — rendering must not launder it.

        ``_render_page`` runs the finished DOM through the same judge as the
        static path, so an anti-bot page that scripting filled in is still
        rejected rather than injected into the prompt as "content".
        """
        from src.media import link_fetcher

        wall = (
            "<html><head><title></title></head><body>"
            "<div>当前环境异常，完成验证后即可继续访问。</div>"
            "<a>去验证</a></body></html>"
        )
        article, reason = link_fetcher._judge_page(
            "https://example.com/x", wall, raw_len=len(wall),
            content_type="text/html",
        )
        self.assertIsNone(article)
        self.assertEqual(reason, "blocked")


if __name__ == "__main__":
    unittest.main()
