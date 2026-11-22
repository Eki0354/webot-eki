"""Tests for read-path visibility in the heartbeat and status file.

The bot could stop receiving messages while every existing signal stayed
green: ``messages_processed`` only grows when something arrives, so a frozen
read path looked exactly like a quiet group.  The heartbeat, the JSON status
file and the dashboard now carry the last message actually read, the lag
behind WeChat's own session table, and which chats are stalled.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import HealthMonitor


class _Config:
    wechat_backend = "wcdb"


class _Conn:
    def execute(self, sql):
        return None


class _Router:
    messages_processed = 7


class _Summarizer:
    last_api_call_time = 0.0


def _monitor(backend, on_tick=None):
    return HealthMonitor(
        summarizer=_Summarizer(),
        router=_Router(),
        conn=_Conn(),
        backend=backend,
        config=_Config(),
        on_tick=on_tick,
    )


class _Backend:
    """Backend exposing a controllable read_progress()."""

    def __init__(self, progress=None, error=None):
        self._progress = progress
        self._error = error
        self.health_status = lambda: "HWND_1"

    def read_progress(self):
        if self._error:
            raise self._error
        return dict(self._progress)


class ReadProgressTests(unittest.TestCase):
    def test_backend_without_read_progress_reports_unknown(self):
        """Non-WCDB backends must not fake a healthy read path."""
        monitor = _monitor(object())

        progress = monitor._read_progress()

        self.assertEqual(progress["newest_seen_ts"], 0)
        self.assertEqual(progress["stalled_chats"], [])
        self.assertEqual(progress["lag_sec"], 0)

    def test_backend_error_is_contained(self):
        monitor = _monitor(_Backend(error=RuntimeError("db gone")))

        progress = monitor._read_progress()   # must not raise

        self.assertEqual(progress["newest_seen_ts"], 0)

    def test_partial_progress_is_filled_in(self):
        monitor = _monitor(_Backend(progress={"lag_sec": 42}))

        progress = monitor._read_progress()

        self.assertEqual(progress["lag_sec"], 42)
        self.assertEqual(progress["stalled_chats"], [])
        self.assertIn("newest_seen_ts", progress)

    def test_tick_pushes_read_fields_to_dashboard(self):
        now = int(time.time())
        backend = _Backend(progress={
            "newest_seen_ts": now - 30,
            "newest_session_ts": now,
            "lag_sec": 30,
            "stalled_chats": ["123@chatroom"],
            "tracked_chats": 2,
        })
        ticks = []
        monitor = _monitor(backend, on_tick=lambda **kw: ticks.append(kw))

        monitor._tick()

        self.assertTrue(ticks)
        payload = ticks[-1]
        self.assertEqual(payload["stalled_chats"], ["123@chatroom"])
        self.assertEqual(payload["read_lag_sec"], 30)
        self.assertEqual(payload["last_message_sec_ago"], 30)

    def test_never_read_reports_minus_one(self):
        backend = _Backend(progress={
            "newest_seen_ts": 0,
            "lag_sec": 0,
            "stalled_chats": [],
            "tracked_chats": 1,
        })
        ticks = []
        monitor = _monitor(backend, on_tick=lambda **kw: ticks.append(kw))

        monitor._tick()

        self.assertEqual(ticks[-1]["last_message_sec_ago"], -1)


class StatusJsonTests(unittest.TestCase):
    def test_status_file_carries_read_progress(self):
        now = int(time.time())
        backend = _Backend(progress={
            "newest_seen_ts": now - 120,
            "newest_session_ts": now,
            "lag_sec": 120,
            "stalled_chats": ["123@chatroom"],
            "tracked_chats": 3,
        })
        monitor = _monitor(backend)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.bot.PROJECT_ROOT", Path(tmp)):
                monitor._tick()
            written = json.loads(
                (Path(tmp) / "data" / "bot_status.json").read_text("utf-8"),
            )

        self.assertEqual(written["read_lag_sec"], 120)
        self.assertEqual(written["stalled_chats"], ["123@chatroom"])
        self.assertEqual(written["tracked_chats"], 3)
        self.assertEqual(written["last_message_sec_ago"], 120)
        # Existing fields must survive the addition.
        self.assertEqual(written["messages_processed"], 7)
        self.assertEqual(written["wechat_backend"], "wcdb")
        self.assertIn("timestamp", written)


if __name__ == "__main__":
    unittest.main()
