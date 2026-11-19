"""A quoted image must be described from the payload inside ``<refermsg>``.

WeChat 4.x sends a reply as an appmsg whose ``<refermsg>`` block holds the
original message.  The reply's own body carries only the sender's wording, so
reading ``<refermsg>`` is the difference between answering the question and
telling the model "[直播]" — the label ``type=57`` would otherwise produce.
"""

import unittest


_QUOTED_IMG = (
    '<msg><appmsg>'
    '<title>@Eki 这张图片里是谁</title>'
    '<action>view</action>'
    '<type>57</type>'
    '<refermsg>'
    '<type>3</type>'
    '<svrid>8297273199210014969</svrid>'
    '<displayname>Dobby</displayname>'
    '<content>&lt;msg&gt;&lt;img md5="abc" hevc_mid_size="123"/&gt;&lt;/msg&gt;</content>'
    '</refermsg>'
    '</appmsg></msg>'
)


class RefermsgParsingTests(unittest.TestCase):
    def test_extract_refermsg_unescapes_payload(self):
        from src.media.appmsg import extract_refermsg

        refer = extract_refermsg(_QUOTED_IMG)
        self.assertEqual(refer["type"], 3)
        self.assertEqual(refer["displayname"], "Dobby")
        # The quoted payload is stored escaped; leaving it escaped would make
        # the <img> tag unreadable to the attribute readers downstream.
        self.assertIn('<img md5="abc"', refer["content"])

    def test_reply_is_labelled_as_quote_not_live_share(self):
        """``type=57`` is the reply envelope, not a live share.

        Measured on real data: a reply to an image reported ``[直播]`` and the
        model had nothing to answer with.
        """
        from src.media.appmsg import reduce_appmsg

        self.assertEqual(reduce_appmsg(_QUOTED_IMG), "[引用] @Eki 这张图片里是谁")

    def test_appmsg_without_refermsg_yields_none(self):
        from src.media.appmsg import extract_refermsg

        self.assertIsNone(
            extract_refermsg('<msg><appmsg><type>5</type></appmsg></msg>')
        )


class _FakeImage:
    """Records the message the pipeline asked it to decrypt."""

    def __init__(self):
        self.seen = []

    def fetch(self, msg):
        self.seen.append(msg)
        from pathlib import Path
        return Path("fake.jpg")


class _FakeVision:
    available = True

    def describe(self, path):
        return "一个穿白裙的女孩站在海边"


class QuotedImageTests(unittest.TestCase):
    """``MediaPipeline.process`` on a reply that quotes an image."""

    def _pipeline(self):
        from src.media.pipeline import MediaPipeline

        pipeline = object.__new__(MediaPipeline)
        pipeline._stats = {"emoji": 0, "image": 0, "described": 0, "failed": 0}
        pipeline._image = _FakeImage()
        pipeline._vision = _FakeVision()
        pipeline._emoji = None
        return pipeline

    def _reply(self, xml: str) -> dict:
        # ``content_kind='text'`` keeps the test free of base64+zstd encoding;
        # the composite localType still routes into the appmsg branch.
        return {
            "localType": 244813135921,
            "content_kind": "text",
            "message_content": xml,
            "message_id": "m1",
        }

    def test_quoted_image_is_described(self):
        pipeline = self._pipeline()
        text = pipeline.process(self._reply(_QUOTED_IMG), "55952837114@chatroom")

        self.assertEqual(
            text,
            "@Eki 这张图片里是谁（引用的图片：一个穿白裙的女孩站在海边）",
        )
        # The store must receive the quoted <img> payload, not the reply body.
        self.assertIn('<img md5="abc"', pipeline._image.seen[0]["message_content"])
        self.assertEqual(pipeline._stats["described"], 1)

    def test_quoted_non_image_falls_back_to_appmsg_label(self):
        pipeline = self._pipeline()
        xml = (
            '<msg><appmsg><title>看这个链接</title><type>57</type>'
            '<refermsg><type>5</type><content>x</content></refermsg>'
            '</appmsg></msg>'
        )
        text = pipeline.process(self._reply(xml), "55952837114@chatroom")

        # No image to describe — the generic reduction still runs.
        self.assertEqual(text, "[引用] 看这个链接")
        self.assertEqual(pipeline._image.seen, [])

    def test_undecryptable_quote_still_returns_the_question(self):
        pipeline = self._pipeline()
        pipeline._image = type("_Broken", (), {"fetch": lambda self, msg: None})()

        text = pipeline.process(self._reply(_QUOTED_IMG), "55952837114@chatroom")

        # Never drop the user's wording: an unanswered question is better
        # than a silently missing message.
        self.assertIn("@Eki 这张图片里是谁", text)
        self.assertIn("没能解析", text)
        self.assertEqual(pipeline._stats["failed"], 1)


if __name__ == "__main__":
    unittest.main()
