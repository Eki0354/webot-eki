"""A described image must never reach the model looking like a placeholder.

Measured on a live run: the image *was* described and the description *was*
in the chat context, yet the model answered "我这边就是个占位符".  A bare
``[图片]`` carries a strong prior of "unavailable", so the fix labels the
content instead of relying on the prompt to out-vote that prior.
"""

import unittest


class LabelDescribedMediaTests(unittest.TestCase):
    """``label_described_media`` rewrites only what really has content."""

    def test_described_media_becomes_explicit_content_label(self):
        from src.summarize.base import label_described_media

        self.assertEqual(
            label_described_media("[图片] 一只猫在键盘上"),
            "[图片内容：一只猫在键盘上]",
        )
        self.assertEqual(
            label_described_media("[表情] 一只猫在摇头"),
            "[表情内容：一只猫在摇头]",
        )
        # The pipeline also labels links, so a title follows the same rule.
        self.assertEqual(
            label_described_media("[链接] 某新闻标题"),
            "[链接内容：某新闻标题]",
        )

    def test_bare_placeholder_is_left_alone(self):
        """An undescribed placeholder is genuinely unreadable — keep it bare.

        Rewriting it would tell the model there is content when there is
        none, which is exactly the fabrication the prompt forbids.
        """
        from src.summarize.base import label_described_media

        for content in ("[图片]", "[视频]", "[文件]", "[语音]", "[表情]"):
            self.assertEqual(label_described_media(content), content)

    def test_marker_in_the_middle_is_untouched(self):
        from src.summarize.base import label_described_media

        content = "他说 [图片] 之后就没下文了"
        self.assertEqual(label_described_media(content), content)


class ChatContextRenderingTests(unittest.TestCase):
    """The rewrite has to reach the prompt, not just the helper."""

    def test_chat_prompt_labels_described_media(self):
        from src.summarize.base import AbstractSummarizer

        captured = {}

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
                captured["messages"] = messages
                return "ok"

        summarizer = _Capture()
        summarizer.chat(
            message="描述上一张图片的内容",
            context_messages=[{
                "sender_name": "Dobby",
                "content": "[图片] 一只猫在键盘上",
                "msg_type": 3,
            }],
            requester_name="Dobby",
            bot_name="Eki",
            group_name="Dobby测试",
        )

        # The surrounding chat now travels as real conversation turns, so
        # that is where the label has to land.
        transcript = "\n".join(m["content"] for m in captured["messages"])
        self.assertIn("[图片内容：一只猫在键盘上]", transcript)
        # The ambiguous form must be gone, otherwise the prior can still win.
        self.assertNotIn("Dobby: [图片] 一只猫在键盘上", transcript)

    def test_proactive_prompt_labels_described_media(self):
        from src.summarize.base import AbstractSummarizer

        captured = {}

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
                return ""

        class _Mode:
            label = "活跃"
            description = "群里聊得热闹"
            instruction = "可以接话"
            max_chars = 60
            context_count = 20

        summarizer = _Capture()
        summarizer.proactive_chat(
            mode=_Mode(),
            context_messages=[{
                "sender_name": "Dobby",
                "content": "[表情] 一只猫在摇头",
                "msg_type": 47,
            }],
            bot_name="Eki",
            group_name="Dobby测试",
        )

        self.assertIn("[表情内容：一只猫在摇头]", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
