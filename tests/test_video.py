"""Reading a shared *video*: find its address, sample frames, describe the clip.

Reported bug: a member quotes a Xiaohongshu video note and asks "你能看到这个
视频吗".  The reply was "就看到个标题，视频本身加载不出来" — honest, because a
chat completion receives text only and nothing ever turned the clip into
something the model could look at.

Two halves are pinned down here:

* ``video_urls_from_page`` must find the media address in the *same* response
  that carried the page text.  Measured on the real note: a second request
  answers with a degraded 23 KB shell that has no ``masterUrl`` at all, so a
  re-fetch silently loses every clip.
* ``MediaPipeline.describe_video`` and ``Router._video_context`` must degrade
  to "no picture" rather than raising, and the description has to end up in
  the chat prompt with a label that stops the model from denying it saw it.

No network, no ffmpeg, no AI calls: every boundary is patched.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.media.video import frame_timestamps, video_urls_from_page
from src.media.link_fetcher import Article


#: The shape Xiaohongshu embeds: addresses escaped as ``\u002F``, in a JSON
#: state block, with a low- and a high-bitrate rendition of the same clip.
_XHS_PAGE = (
    '<html><script>window.__INITIAL_STATE__ = {'
    '"note": {"video": {"media": {"stream": {"h264": [{'
    '"masterUrl": "http:\\u002F\\u002Fsns-video-v3.xhscdn.com\\u002Fstream\\u002Fa_259.mp4?sign=1&amp;t=2",'
    '"backupUrl": "http:\\u002F\\u002Fsns-video-v3.xhscdn.com\\u002Fstream\\u002Fa_537.mp4?sign=3"'
    '}]}}}}}</script></html>'
)


class VideoUrlExtractionTests(unittest.TestCase):
    def test_escaped_addresses_are_unescaped(self):
        urls = video_urls_from_page(_XHS_PAGE)
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("http://sns-video-v3.xhscdn.com/"))
        # ``&amp;`` in markup is ``&`` in the query, or the request 404s.
        self.assertIn("sign=1&t=2", urls[0])
        self.assertNotIn("\\u002F", urls[0])

    def test_master_url_comes_first(self):
        """The rendition the site itself plays must lead the retry order."""
        urls = video_urls_from_page(_XHS_PAGE)
        self.assertIn("_259.mp4", urls[0])

    def test_duplicates_are_dropped(self):
        page = (
            '<video src="https://cdn.example.com/a.mp4"></video>'
            '<source src="https://cdn.example.com/a.mp4">'
        )
        self.assertEqual(
            video_urls_from_page(page), ["https://cdn.example.com/a.mp4"]
        )

    def test_limit_is_honoured(self):
        page = "".join(
            f'<video src="https://cdn.example.com/{i}.mp4"></video>'
            for i in range(6)
        )
        self.assertEqual(len(video_urls_from_page(page, limit=2)), 2)

    def test_non_http_entries_are_skipped(self):
        page = '<video src="blob:https://example.com/abc"></video>'
        self.assertEqual(video_urls_from_page(page), [])

    def test_empty_page_yields_nothing(self):
        self.assertEqual(video_urls_from_page(""), [])


class FrameTimestampTests(unittest.TestCase):
    """Frames are spread across the clip, never bunched at a black fade-in."""

    def test_moments_are_centred_in_equal_slices(self):
        self.assertEqual(frame_timestamps(20.0, 3), [3.33, 10.0, 16.67])

    def test_unknown_length_samples_the_opening(self):
        self.assertEqual(frame_timestamps(0.0, 2), [0.5, 1.5])

    def test_count_is_at_least_one(self):
        self.assertEqual(len(frame_timestamps(10.0, 0)), 1)


class MediaRefererTests(unittest.TestCase):
    """A CDN that pins its referer gets that root, not the page it came from.

    Bilibili is the measured case: a chat share link is ``b23.tv``, and the
    CDN answers 403 to that referer (and to none), while
    ``https://www.bilibili.com/`` answers 206.  Every Bilibili clip was
    refused before this, so the frames never reached the model.
    """

    def test_bilibili_cdn_gets_the_site_root(self):
        from src.media.video import _media_referer

        self.assertEqual(
            _media_referer(
                "https://upos-sz-mirrorcoso1.bilivideo.com/upgcxcode/a.m4s",
                "https://b23.tv/2qF8lis",
            ),
            "https://www.bilibili.com/",
        )

    def test_subdomains_of_the_pinned_host_match(self):
        from src.media.video import _media_referer

        self.assertEqual(
            _media_referer("https://x.bilivideo.cn/a.m4s", "https://b23.tv/x"),
            "https://www.bilibili.com/",
        )

    def test_other_cdns_keep_the_page_referer(self):
        from src.media.video import _media_referer

        self.assertEqual(
            _media_referer(
                "https://sns-video-v3.xhscdn.com/stream/a.mp4",
                "https://www.xiaohongshu.com/explore/x",
            ),
            "https://www.xiaohongshu.com/explore/x",
        )

    def test_a_lookalike_host_is_not_matched(self):
        from src.media.video import _media_referer

        # ``evil-bilivideo.com`` must not inherit the root referer.
        self.assertEqual(
            _media_referer("https://evil-bilivideo.com/a.m4s", "https://page/x"),
            "https://page/x",
        )

    def test_download_sends_the_corrected_referer(self):
        from src.media import video as video_mod

        captured = {}

        class _Resp:
            def __init__(self):
                self._chunks = [b"x" * 4096, b""]

            def read(self, _n=-1):
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            captured["referer"] = request.get_header("Referer")
            return _Resp()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(video_mod.urllib.request, "urlopen", fake_urlopen):
                got = video_mod.download_video(
                    "https://upos-sz-mirrorcoso1.bilivideo.com/a.m4s",
                    Path(tmp),
                    referer="https://b23.tv/2qF8lis",
                )
            self.assertIsNotNone(got)
        self.assertEqual(captured["referer"], "https://www.bilibili.com/")


class BilibiliApiTests(unittest.TestCase):
    """Bilibili's own JSON API is the path that works without a browser.

    Measured case: in the VM the headless renderer exits 0 with an empty DOM
    in about a second, so a shared Bilibili clip had no frames.  The site's
    API answered the cid and four DASH renditions to a plain desktop request,
    which is why this route exists alongside the renderer rather than instead
    of it.  No network here: both boundaries are patched.
    """

    _BVID = "BV1xx411c7mD"
    _PAGE = f"https://www.bilibili.com/video/{_BVID}?share_source=copy"

    def test_bvid_is_read_out_of_a_full_page_url(self):
        from src.media.video import _bili_bvid

        self.assertEqual(_bili_bvid(self._PAGE), self._BVID)

    def test_share_link_is_resolved_by_one_refused_redirect(self):
        """``b23.tv`` carries no id; the Location header does."""
        from src.media import video as video_mod

        asked = []
        bvid = self._BVID

        class _Opener:
            def open(self, request, timeout=0):
                asked.append(request.full_url)
                raise video_mod.urllib.error.HTTPError(
                    request.full_url, 302, "Found",
                    {"Location": f"https://www.bilibili.com/video/{bvid}"},
                    None,
                )

        with patch.object(
            video_mod.urllib.request, "build_opener",
            side_effect=lambda *handlers: _Opener(),
        ):
            self.assertEqual(
                video_mod._bili_bvid("https://b23.tv/2qF8lis"), self._BVID,
            )
        # The share link itself was asked, and the body was never fetched.
        self.assertEqual(asked, ["https://b23.tv/2qF8lis"])

    def test_a_stranger_host_never_reaches_the_api(self):
        """An ``…/BV…`` path on someone else's domain is not an invitation."""
        from src.media import video as video_mod

        started = []

        with patch.object(
            video_mod.urllib.request, "build_opener",
            side_effect=lambda *a: started.append(a),
        ):
            self.assertEqual(
                video_mod._bili_bvid(f"https://evil.example/video/{self._BVID}"),
                "",
            )
        self.assertEqual(started, [])

    def test_unresolvable_share_link_is_empty_not_an_exception(self):
        from src.media import video as video_mod

        class _Opener:
            def open(self, request, timeout=0):
                raise OSError("network down")

        with patch.object(
            video_mod.urllib.request, "build_opener",
            side_effect=lambda *a: _Opener(),
        ):
            self.assertEqual(video_mod._bili_bvid("https://b23.tv/x"), "")

    def test_api_failure_degrades_to_none(self):
        """This runs behind a reply: an API hiccup must not fail the answer."""
        from src.media import video as video_mod

        with patch.object(
            video_mod.urllib.request, "urlopen",
            side_effect=OSError("connection reset"),
        ):
            self.assertIsNone(video_mod._bili_json("/x/web-interface/view?bvid=x"))

    def test_dash_renditions_are_ordered_standard_port_first(self):
        """Odd-port addresses are refused by the SSRF fence, so they go last."""
        from src.media import video as video_mod

        view = {"code": 0, "data": {"cid": 12345}}
        play = {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {
                            "baseUrl": "https://upos.example:4483/a.m4s",
                            "backupUrl": ["https://mirror.bilivideo.com/a.m4s"],
                        },
                        {
                            "baseUrl": "https://upos.example:8082/b.m4s",
                            "backupUrl": [],
                        },
                    ],
                },
            },
        }

        def _fake_json(path):
            return view if "view" in path else play

        with patch.object(video_mod, "_bili_json", side_effect=_fake_json):
            urls = video_mod.bilibili_video_urls(self._PAGE)

        self.assertEqual(urls, [
            "https://mirror.bilivideo.com/a.m4s",
            "https://upos.example:4483/a.m4s",
            "https://upos.example:8082/b.m4s",
        ])

    def test_duplicate_addresses_appear_once(self):
        from src.media import video as video_mod

        same = "https://upos.example/a.m4s"
        view = {"code": 0, "data": {"cid": 1}}
        play = {"data": {"dash": {"video": [
            {"baseUrl": same, "backupUrl": [same]},
            {"baseUrl": same, "backupUrl": []},
        ]}}}

        with patch.object(
            video_mod, "_bili_json",
            side_effect=lambda path: view if "view" in path else play,
        ):
            self.assertEqual(video_mod.bilibili_video_urls(self._PAGE), [same])

    def test_no_cid_means_no_addresses(self):
        from src.media import video as video_mod

        with patch.object(
            video_mod, "_bili_json", return_value={"code": -404, "data": {}},
        ):
            self.assertEqual(video_mod.bilibili_video_urls(self._PAGE), [])


def _pipeline(vision=True):
    from src.media.pipeline import MediaPipeline

    config = MagicMock()
    config.media_vision_enabled = vision
    config.media_vision_model = ""
    config.wechat_data_dir = ""
    config.ai_backend = "deepseek"
    config.deepseek_api_key = "sk-test"
    config.deepseek_model = "deepseek-v4-flash"
    config.deepseek_base_url = "https://api.deepseek.com"
    config.image_xor_key = ""
    config.image_aes_key = ""
    config.image_key_provider = "env"
    return MediaPipeline(config)


_VIDEO_URLS = ("https://cdn.example.com/a.mp4",)


class DescribeVideoTests(unittest.TestCase):
    """Every failure must return None — this runs inside the reply path."""

    def test_without_vision_there_is_nothing_to_ask(self):
        self.assertIsNone(
            _pipeline(vision=False).describe_video(_VIDEO_URLS, "https://x.com/p")
        )

    def test_missing_decoder_is_not_an_error(self):
        pipeline = _pipeline()
        with patch("src.media.video.available", return_value=False):
            self.assertIsNone(
                pipeline.describe_video(_VIDEO_URLS, "https://x.com/p")
            )
        self.assertEqual(pipeline.stats()["video"], 0)

    def test_no_addresses_skips_the_download(self):
        pipeline = _pipeline()
        with patch("src.media.video.available", return_value=True):
            self.assertIsNone(pipeline.describe_video((), "https://x.com/p"))

    def test_undownloadable_clip_is_counted_and_reported(self):
        pipeline = _pipeline()
        with patch("src.media.video.available", return_value=True), \
             patch("src.media.video.frames_from_urls", return_value=[]), \
             patch("src.media.pipeline.logger"):
            self.assertIsNone(
                pipeline.describe_video(_VIDEO_URLS, "https://x.com/p")
            )
        stats = pipeline.stats()
        self.assertEqual(stats["video"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_frames_are_described_and_the_temp_dir_removed(self):
        pipeline = _pipeline()
        workdir = Path(tempfile.mkdtemp(prefix="webot_video_test_"))
        frame = workdir / "frame_0.jpg"
        frame.write_bytes(b"jpg")

        with patch("src.media.video.available", return_value=True), \
             patch("src.media.video.frames_from_urls", return_value=[frame]), \
             patch.object(
                 pipeline._vision, "describe_frames",
                 return_value="一个女生在客厅里跳舞，然后镜头转向一屋子盒子。",
             ):
            text = pipeline.describe_video(_VIDEO_URLS, "https://x.com/p")

        self.assertIn("跳舞", text)
        self.assertEqual(pipeline.stats()["described"], 1)
        # ``frames_from_urls`` hands the directory over; retaining it would
        # leak a clip-sized temp dir per shared video.
        self.assertFalse(workdir.exists())

    def test_empty_model_answer_is_a_failure_not_a_crash(self):
        pipeline = _pipeline()
        workdir = Path(tempfile.mkdtemp(prefix="webot_video_test_"))
        frame = workdir / "frame_0.jpg"
        frame.write_bytes(b"jpg")

        with patch("src.media.video.available", return_value=True), \
             patch("src.media.video.frames_from_urls", return_value=[frame]), \
             patch.object(pipeline._vision, "describe_frames",
                          return_value=None), \
             patch("src.media.pipeline.logger"):
            self.assertIsNone(
                pipeline.describe_video(_VIDEO_URLS, "https://x.com/p")
            )
        self.assertEqual(pipeline.stats()["failed"], 1)
        self.assertFalse(workdir.exists())


class RouterVideoContextTests(unittest.TestCase):
    """The description has to reach the prompt labelled as already seen."""

    def _router(self, pipeline=None, provider=None):
        from src.router import MessageRouter

        class _Cfg:
            link_fetch_enabled = True

        router = MessageRouter.__new__(MessageRouter)
        router._config = _Cfg()
        router._media_pipeline_provider = (
            provider if provider is not None else (lambda: pipeline)
        )
        return router

    def test_no_pipeline_attached_is_quiet(self):
        from src.router import MessageRouter

        router = MessageRouter.__new__(MessageRouter)
        router._config = type("C", (), {"link_fetch_enabled": True})()
        self.assertEqual(router._video_context([Article(
            url="https://x.com/p", title="t", text="x",
            video_urls=_VIDEO_URLS,
        )]), "")

    def test_text_only_article_never_calls_the_pipeline(self):
        pipeline = MagicMock()
        pipeline.describe_video.side_effect = AssertionError("must not sample")
        router = self._router(pipeline)
        self.assertEqual(
            router._video_context([Article(url="https://x.com/p", title="t", text="x")]),
            "",
        )

    def test_description_is_labelled_as_already_watched(self):
        pipeline = MagicMock()
        pipeline.describe_video.return_value = "一段 20 秒的搞笑短片。"
        router = self._router(pipeline)

        block = router._video_context([Article(
            url="https://x.com/p", title="视频标题", text="正文",
            video_urls=_VIDEO_URLS,
        )])

        self.assertIn("[视频画面]", block)
        self.assertIn("视频标题", block)
        self.assertIn("一段 20 秒的搞笑短片。", block)
        # Without this instruction the model denies seeing it — the bug.
        self.assertIn("不要说你打不开视频", block)

    def test_failure_degrades_to_empty(self):
        pipeline = MagicMock()
        pipeline.describe_video.side_effect = RuntimeError("ffmpeg died")
        router = self._router(pipeline)
        with patch("src.router.logger"):
            self.assertEqual(
                router._video_context([Article(
                    url="https://x.com/p", title="t", text="x",
                    video_urls=_VIDEO_URLS,
                )]),
                "",
            )

    def test_link_context_carries_text_and_frames_together(self):
        """The copy answers "讲了啥", the frames answer "能看到视频吗"."""
        pipeline = MagicMock()
        pipeline.describe_video.return_value = "画面里在展示一屋子游戏机。"
        router = self._router(pipeline)

        from src.media import link_fetcher

        article = Article(
            url="https://x.com/p", title="视频笔记", text="正文内容。" * 20,
            video_urls=_VIDEO_URLS,
        )
        original = link_fetcher.fetch_for_question
        link_fetcher.fetch_for_question = lambda text, **kw: [article]
        try:
            block = router._link_context("https://x.com/p 这是啥")
        finally:
            link_fetcher.fetch_for_question = original

        self.assertIn("[链接正文]", block)
        self.assertIn("[视频画面]", block)
        self.assertIn("画面里在展示一屋子游戏机。", block)


if __name__ == "__main__":
    unittest.main()
