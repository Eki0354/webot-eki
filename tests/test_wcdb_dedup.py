"""Tests for dedup ordering in the WCDB poll loop.

The poll loop used to standardize every message of the last page before
checking the dedup set.  Standardization runs the media pipeline — vision
requests for images — so every restart re-described old pictures before any
new message (and its @-reply) was looked at.  The fix computes the message
ID from the raw row first and skips known IDs outright.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.wechat.wcdb_backend import (
    HWND_REINIT_SEC,
    STALL_REINIT_SEC,
    WcdbBackend,
    raw_message_id,
)


def _raw(server_id, local_id, content, local_type=1, ts=1000):
    return {
        "server_id": server_id,
        "local_id": local_id,
        "localType": local_type,
        "create_time": ts,
        "sender_username": "wxid_sender",
        "message_content": content,
    }


class _FakeClient:
    def __init__(self, messages, sessions=None):
        self.messages = messages
        self.sessions = sessions or []
        self.calls = 0
        self.session_calls = 0

    def get_messages(self, talker=None, limit=50):
        self.calls += 1
        return self.messages

    def get_sessions(self, limit=500):
        self.session_calls += 1
        return self.sessions

    def resolve_nickname(self, username):
        return username


def _make_backend(messages, store=None, sessions=None):
    """A backend wired to a fake client, with no real DB/window/DLL."""
    backend = WcdbBackend(groups=["g"], store=store)
    backend._client = _FakeClient(messages, sessions)
    backend._talker_ids = {"g": "123@chatroom"}
    backend._running = True
    backend._standardized = []

    def fake_standardize(msg, group_name, talker):
        backend._standardized.append(msg.get("server_id"))
        return {
            "message_id": raw_message_id(msg),
            "chat_id": talker,
            "group_name": group_name,
            "sender_id": msg.get("sender_username", ""),
            "sender_name": "sender",
            "content": msg.get("message_content", ""),
            "msg_type": 1,
            "timestamp": msg.get("create_time", 0),
            "is_at_mentioned": False,
            "is_group": True,
            "is_system_join": False,
            "new_member_id": "",
        }

    backend._standardize = fake_standardize
    backend._handled = []
    backend._handle_message = (
        lambda group_name, talker, standardized, callback:
        backend._handled.append(standardized["content"])
    )
    return backend


class RawMessageIdTests(unittest.TestCase):
    """The pre-check ID must match what _standardize produces."""

    def test_server_id_wins_and_is_stable(self):
        msg = _raw(1234, 7, "hello")
        self.assertEqual(raw_message_id(msg), raw_message_id(dict(msg)))

    def test_falls_back_to_local_id_without_server_id(self):
        with_server = _raw(1234, 7, "hello")
        without_server = _raw("", 7, "hello")
        # Different key source => different ID; but the same row twice must
        # always hash the same, otherwise dedup would never hit.
        self.assertNotEqual(raw_message_id(with_server),
                            raw_message_id(without_server))
        self.assertEqual(raw_message_id(without_server),
                         raw_message_id(_raw("", 7, "hello")))


class PollDedupTests(unittest.TestCase):
    def test_second_cycle_does_not_standardize_again(self):
        backend = _make_backend([_raw(1, 10, "a"), _raw(2, 11, "b")])

        backend._poll_group("g", "123@chatroom", lambda m: None)
        self.assertEqual(len(backend._standardized), 2)
        # WCDB returns newest-first, so the loop replays them oldest-first.
        self.assertEqual(backend._handled, ["b", "a"])

        backend._standardized.clear()
        backend._poll_group("g", "123@chatroom", lambda m: None)
        self.assertEqual(backend._standardized, [],
                         "known messages must be skipped before standardizing")
        self.assertEqual(backend._handled, ["b", "a"])

    def test_filtered_message_is_not_standardized_twice(self):
        backend = _make_backend([_raw(1, 10, "x")])
        backend._standardize = lambda msg, g, t: None

        backend._poll_group("g", "123@chatroom", lambda m: None)
        calls = backend._client.calls
        backend._poll_group("g", "123@chatroom", lambda m: None)
        # Nothing was dispatched either cycle, and the row is remembered.
        self.assertEqual(backend._handled, [])
        self.assertGreater(backend._client.calls, calls)


class DedupWarmupTests(unittest.TestCase):
    def test_warm_dedup_loads_stored_ids(self):
        class _Store:
            def __init__(self):
                self.asked = []

            def get_recent_message_ids(self, chat_id, limit=200):
                self.asked.append(chat_id)
                return ["id-a", "id-b"]

        store = _Store()
        backend = _make_backend([], store=store)

        backend._warm_dedup()

        self.assertEqual(store.asked, ["123@chatroom"])
        self.assertIn("id-a", backend._known_ids)
        self.assertIn("id-b", backend._known_ids)

    def test_warm_dedup_without_store_is_a_noop(self):
        backend = _make_backend([], store=None)
        backend._warm_dedup()   # must not raise
        self.assertEqual(len(backend._known_ids), 0)

    def test_warm_dedup_survives_store_errors(self):
        class _BrokenStore:
            def get_recent_message_ids(self, chat_id, limit=200):
                raise RuntimeError("db gone")

        backend = _make_backend([], store=_BrokenStore())
        backend._warm_dedup()   # must not raise — polling still has to work


class ReadLagTests(unittest.TestCase):
    """Reading nothing and reading nothing *new* must be distinguishable."""

    TALKER = "123@chatroom"
    NOW = 1_800_000_000

    def _backend(self, session_ts, seen_ts=None, seconds_uptime=600):
        sessions = [{"username": self.TALKER, "last_timestamp": session_ts}]
        backend = _make_backend([], sessions=sessions)
        if seen_ts is not None:
            backend._last_seen_ts[self.TALKER] = seen_ts
        backend._read_watch_since = self.NOW - seconds_uptime
        return backend

    def test_matching_times_are_not_flagged(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=self.NOW)

        with self.assertNoLogs("src.wechat.wcdb_backend", level="WARNING"):
            backend._check_read_lag(self.NOW)

        self.assertEqual(backend.read_progress()["stalled_chats"], [])
        self.assertEqual(backend.read_progress()["lag_sec"], 0)

    def test_large_lag_marks_chat_stalled(self):
        session_ts = self.NOW
        # We can only read messages ~1 hour older than what WeChat records.
        backend = self._backend(session_ts=session_ts,
                                seen_ts=session_ts - 3600)

        with self.assertLogs("src.wechat.wcdb_backend", level="WARNING") as logs:
            backend._check_read_lag(self.NOW)

        self.assertTrue(
            any("not coming through" in line for line in logs.output),
            logs.output,
        )
        progress = backend.read_progress()
        self.assertEqual(progress["stalled_chats"], [self.TALKER])
        self.assertEqual(progress["lag_sec"], 3600)

    def test_never_read_anything_is_flagged_after_grace(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=None)

        with self.assertLogs("src.wechat.wcdb_backend", level="WARNING") as logs:
            backend._check_read_lag(self.NOW)

        self.assertTrue(
            any("none could be read" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(backend.read_progress()["stalled_chats"], [self.TALKER])

    def test_never_read_anything_is_quiet_during_grace(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=None,
                                seconds_uptime=10)

        with self.assertNoLogs("src.wechat.wcdb_backend", level="WARNING"):
            backend._check_read_lag(self.NOW)

        self.assertEqual(backend._stalled, set())

    def test_empty_session_row_is_not_a_stall(self):
        """A group nobody has written in yet has last_timestamp 0."""
        backend = self._backend(session_ts=0, seen_ts=None)

        with self.assertNoLogs("src.wechat.wcdb_backend", level="WARNING"):
            backend._check_read_lag(self.NOW)

        self.assertEqual(backend._stalled, set())

    def test_chat_missing_from_session_table_is_flagged(self):
        # Everything resolved to a different account: nothing to compare.
        backend = _make_backend(
            [], sessions=[{"username": "other@chatroom", "last_timestamp": 1}],
        )
        backend._read_watch_since = self.NOW - 600

        with self.assertLogs("src.wechat.wcdb_backend", level="WARNING") as logs:
            backend._check_read_lag(self.NOW)

        self.assertTrue(
            any("missing from the session table" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(backend._stalled, {self.TALKER})

    def test_stall_clears_when_reads_catch_up(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=self.NOW - 3600)
        backend._check_read_lag(self.NOW)
        self.assertEqual(backend._stalled, {self.TALKER})

        backend._last_seen_ts[self.TALKER] = self.NOW
        backend._check_read_lag(self.NOW + 1)

        self.assertEqual(backend._stalled, set())
        self.assertEqual(backend.read_progress()["stalled_chats"], [])

    def test_warning_is_rate_limited(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=self.NOW - 3600)

        with self.assertLogs("src.wechat.wcdb_backend", level="WARNING") as logs:
            backend._check_read_lag(self.NOW)
            first = len(logs.output)
            # Same stall a few seconds later: must not log again.
            backend._check_read_lag(self.NOW + 5)
            second = len([l for l in logs.output if "not coming through" in l])

        self.assertEqual(
            len([l for l in logs.output[:first] if "not coming through" in l]), 1,
        )
        self.assertEqual(second, 1)

    def test_rate_limited_check_skips_work(self):
        backend = self._backend(session_ts=self.NOW, seen_ts=self.NOW)
        backend._last_lag_check = 0.0
        backend._maybe_check_read_lag()
        first = backend._client.session_calls
        backend._maybe_check_read_lag()
        # Second call inside the interval must not hit the DLL again.
        self.assertEqual(backend._client.session_calls, first)


class HealthStatusTests(unittest.TestCase):
    """The heartbeat line must not report healthy while reads are dead."""

    NOW = 1_800_000_000

    class _Window:
        def __init__(self, hwnd=4242, valid=True):
            self._cached_hwnd = hwnd
            self._valid = valid
            self.scans = 0

        def _validate_hwnd(self, hwnd):
            return self._valid

        def find_hwnd(self, force=False):
            self.scans += 1
            return self._cached_hwnd

    def _backend(self, hwnd=4242, valid=True):
        backend = _make_backend([])
        backend._window = self._Window(hwnd, valid)
        return backend

    def test_healthy_handle_reports_hwnd(self):
        self.assertEqual(self._backend().health_status(), "HWND_4242")

    def test_missing_window_reports_no_hwnd(self):
        self.assertEqual(self._backend(hwnd=None).health_status(), "no_hwnd")
        self.assertEqual(
            self._backend(valid=False).health_status(), "no_hwnd",
        )

    def test_stalled_read_path_outranks_hwnd(self):
        """A live window with a dead read path is still unhealthy."""
        backend = self._backend()
        backend._stalled = {"123@chatroom", "456@chatroom"}

        status = backend.health_status()

        self.assertEqual(status, "read_stalled(2)")
        self.assertNotIn("HWND", status)


class StallRecoveryTests(unittest.TestCase):
    """A long stall must rebuild the handle — reads never raise on their own."""

    TALKER = "123@chatroom"
    NOW = 1_800_000_000

    def _stalled_backend(self):
        sessions = [{"username": self.TALKER, "last_timestamp": self.NOW}]
        backend = _make_backend([], sessions=sessions)
        backend._last_seen_ts[self.TALKER] = self.NOW - 3600
        backend._read_watch_since = self.NOW - 600
        backend._rebuilt = []
        backend._reinitialize = lambda: backend._rebuilt.append(True)
        return backend

    def test_first_stall_only_starts_the_clock(self):
        backend = self._stalled_backend()

        backend._check_read_lag(self.NOW)

        self.assertEqual(backend._rebuilt, [])
        self.assertEqual(backend._stalled_since, self.NOW)
        self.assertEqual(backend._stalled, {self.TALKER})

    def test_short_stall_does_not_rebuild(self):
        backend = self._stalled_backend()

        backend._check_read_lag(self.NOW)
        backend._check_read_lag(self.NOW + 30)

        self.assertEqual(backend._rebuilt, [])

    def test_persistent_stall_rebuilds_handle(self):
        backend = self._stalled_backend()

        with self.assertLogs("src.wechat.wcdb_backend", level="ERROR") as logs:
            backend._check_read_lag(self.NOW)
            backend._check_read_lag(self.NOW + STALL_REINIT_SEC + 1)

        self.assertEqual(len(backend._rebuilt), 1)
        self.assertTrue(
            any("rebuilding the WCDB handle" in line for line in logs.output),
            logs.output,
        )

    def test_rebuild_is_throttled(self):
        backend = self._stalled_backend()

        backend._check_read_lag(self.NOW)
        backend._check_read_lag(self.NOW + STALL_REINIT_SEC + 1)
        # Stalled again immediately: the throttle must hold it back.
        backend._stalled_since = self.NOW
        backend._check_read_lag(self.NOW + STALL_REINIT_SEC + 2)

        self.assertEqual(len(backend._rebuilt), 1)

    def test_rebuild_failure_does_not_break_polling(self):
        backend = self._stalled_backend()

        def boom():
            raise RuntimeError("WeChat is gone")

        backend._reinitialize = boom

        with self.assertLogs("src.wechat.wcdb_backend", level="ERROR") as logs:
            backend._check_read_lag(self.NOW)
            backend._check_read_lag(self.NOW + STALL_REINIT_SEC + 1)

        self.assertTrue(
            any("Handle rebuild failed" in line for line in logs.output),
            logs.output,
        )

    def test_recovery_clears_the_stall_clock(self):
        backend = self._stalled_backend()

        backend._check_read_lag(self.NOW)
        self.assertIsNotNone(backend._stalled_since)

        backend._last_seen_ts[self.TALKER] = self.NOW
        backend._check_read_lag(self.NOW + 1)

        self.assertIsNone(backend._stalled_since)
        self.assertEqual(backend._rebuilt, [])


class WindowRecoveryTests(unittest.TestCase):
    """A vanished WeChat window must be acted on, not just reported."""

    NOW = 1_800_000_000

    def _backend(self, hwnd=None):
        backend = _make_backend([])
        backend._window = HealthStatusTests._Window(hwnd)
        backend._rebuilt = []
        backend._reinitialize = lambda: backend._rebuilt.append(True)
        backend._now = self.NOW
        return backend

    def _check_at(self, backend, ts):
        # _maybe_check_window reads the wall clock; pin it via the throttle
        # fields instead of monkeypatching time.
        backend._last_window_check = ts - 3600
        import src.wechat.wcdb_backend as mod
        original = mod.time.time
        mod.time.time = lambda: ts
        try:
            backend._maybe_check_window()
        finally:
            mod.time.time = original

    def test_window_coming_back_clears_the_alarm(self):
        backend = self._backend(hwnd=None)
        self._check_at(backend, self.NOW)
        self.assertIsNotNone(backend._no_hwnd_since)

        backend._window._cached_hwnd = 4242
        with self.assertLogs("src.wechat.wcdb_backend", level="INFO") as logs:
            self._check_at(backend, self.NOW + 60)

        self.assertIsNone(backend._no_hwnd_since)
        self.assertTrue(any("window is back" in l for l in logs.output),
                        logs.output)

    def test_short_absence_does_not_rebuild(self):
        backend = self._backend(hwnd=None)

        self._check_at(backend, self.NOW)
        self._check_at(backend, self.NOW + 60)

        self.assertEqual(backend._rebuilt, [])

    def test_persistent_absence_rebuilds_handle(self):
        backend = self._backend(hwnd=None)

        with self.assertLogs("src.wechat.wcdb_backend", level="ERROR") as logs:
            self._check_at(backend, self.NOW)
            self._check_at(backend, self.NOW + HWND_REINIT_SEC + 1)

        self.assertEqual(len(backend._rebuilt), 1)
        self.assertTrue(
            any("window has been missing" in l for l in logs.output),
            logs.output,
        )

    def test_window_check_is_rate_limited(self):
        backend = self._backend(hwnd=4242)

        self._check_at(backend, self.NOW)
        scans = backend._window.scans
        # Second check inside the interval must not re-scan.
        backend._maybe_check_window()

        self.assertEqual(backend._window.scans, scans)

    def test_rebuild_failure_keeps_polling_alive(self):
        backend = self._backend(hwnd=None)

        def boom():
            raise RuntimeError("WeChat is gone")

        backend._reinitialize = boom

        with self.assertLogs("src.wechat.wcdb_backend", level="ERROR") as logs:
            self._check_at(backend, self.NOW)
            self._check_at(backend, self.NOW + HWND_REINIT_SEC + 1)

        self.assertTrue(
            any("Handle rebuild failed" in l for l in logs.output),
            logs.output,
        )


class CallbackPoolShutdownTests(unittest.TestCase):
    """stop() must not leave the poll loop submitting into a closed pool.

    The old stop() shut the executor down but kept `_pool` pointing at it, so
    a poll cycle already in flight hit
    ``RuntimeError: cannot schedule new futures after shutdown`` — visible in
    the field as "Poll error #1 (RuntimeError)" right after every restart.
    """

    def _backend_with_pool(self):
        import concurrent.futures

        backend = _make_backend([_raw(1, 10, "hello")])
        backend._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        return backend

    def test_stop_detaches_the_pool(self):
        backend = self._backend_with_pool()

        backend.stop()

        self.assertIsNone(backend._pool,
                          "a closed pool must not stay reachable")
        self.assertEqual(len(backend._retired_pools), 1)

    def test_poll_after_stop_does_not_raise(self):
        backend = self._backend_with_pool()
        backend.stop()

        # Must not raise "cannot schedule new futures after shutdown".
        backend._poll_group("g", "123@chatroom", lambda m: None)

        # Nothing is dispatched: _poll_group stops on `_running` first, so a
        # stopping bot neither submits nor handles.
        self.assertEqual(backend._handled, [])

    def test_closed_pool_falls_back_to_inline(self):
        """A pool closed by stop() while polling continues must not drop work.

        `_running` is still True here — this is the window where stop() has
        detached the pool but the current poll cycle has not noticed yet.
        """
        import concurrent.futures

        backend = _make_backend([_raw(1, 10, "hello")])
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        pool.shutdown(wait=False)
        backend._pool = pool   # closed, but still referenced (the old bug)

        backend._poll_group("g", "123@chatroom", lambda m: None)

        self.assertEqual(backend._handled, ["hello"])

    def test_stop_is_idempotent(self):
        backend = self._backend_with_pool()

        backend.stop()
        backend.stop()   # second call must not raise or double-retire

        self.assertIsNone(backend._pool)
        self.assertEqual(len(backend._retired_pools), 1)

    def test_stop_before_start_is_a_noop(self):
        backend = _make_backend([])
        backend._pool = None

        backend.stop()   # must not raise

        self.assertEqual(backend._retired_pools, [])


if __name__ == "__main__":
    unittest.main()
