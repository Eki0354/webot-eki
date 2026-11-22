"""
WCDB Native Backend — zero external dependencies.

Reads WeChat messages directly from the encrypted WCDB database via
patched wcdb_api.dll (ctypes).  Uses WeChatWindowController for sending.

WCDB native access — reads encrypted database in-process, no external dependencies.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .base import AbstractWeChatBackend, MessageCallback
from .wcdb_client import WcdbNativeClient
from .window_controller import WeChatWindowController
from .helpers import DedupSet, format_nontext_content
from src.media.appmsg import looks_like_xml, strip_sender_prefix
from src.media.blob import decode_blob, is_binary

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 1.0
MAX_DEDUP_SIZE = 5000
MAX_CONSECUTIVE_ERRORS = 5   # trigger reinit after this many consecutive failures

# How many recently persisted message IDs to preload into the dedup set on
# startup, per group.  Without this the set is empty after every restart, and
# the last page of history (including its images) is processed again — the
# media pipeline then re-describes old pictures before any new message is
# looked at.
DEDUP_WARMUP_LIMIT = 200

# ── "Read empty" detection ────────────────────────────────────────
# Reading nothing and reading nothing *new* look identical from outside: the
# DLL returns an empty list for both, so a frozen handle used to keep the
# bot reporting db_ok while @-mentions never arrived.  SessionTable records
# the last message time per chat, so the two cases can be told apart.
# Measured on a live account: session.last_timestamp equals the newest
# get_messages() create_time exactly for every chat that has messages.
LAG_CHECK_INTERVAL_SEC = 60      # how often to read the session table
STALL_WARN_SEC = 300             # session newer than what we read by this much → stalled
STALL_WARN_REPEAT_SEC = 600      # per-group warning rate limit
READ_GRACE_SEC = 120             # startup grace before "never read anything" counts

# A read path that stays stalled for this long is treated as a dead handle,
# not as "the group is quiet": the WCDB connection is closed and re-opened.
# WeChat restarts, logouts and VM suspends all leave a stale handle behind,
# and nothing used to recover from that because reads never raised.
STALL_REINIT_SEC = 180
STALL_REINIT_MIN_INTERVAL = 300  # never thrash the native handle

# The window is a separate failure from the read path: sending needs it, but
# reading the database does not.  It is re-scanned first (cheap), and only a
# window that stays gone for this long escalates to a full rebuild.
HWND_REINIT_SEC = 180

# Message types whose body is media rather than text.  Used to pick a
# placeholder when MediaPipeline cannot describe them.
_MEDIA_LOCAL_TYPES = frozenset({
    3,      # image
    43,     # video
    47,     # emoji / sticker
    48,     # location
    49,     # appmsg (link / file / mini-program)
})


def raw_message_id(msg: dict) -> str:
    """Compute the stable message ID for a *raw* WCDB message.

    This must stay in sync with the ID generated in ``_standardize``: the
    poll loop calls it before standardization so that already-seen messages
    are skipped without running the media pipeline (vision requests are the
    expensive part, and they would otherwise re-describe old pictures on
    every restart).
    """
    raw_id = (
        str(msg.get("server_id", ""))
        or str(msg.get("local_id", ""))
        or f"{msg.get('sender_username', '')}|{msg.get('message_content', '')}"
           f"|{msg.get('create_time', 0)}"
    )
    return hashlib.md5(str(raw_id).encode()).hexdigest()


def _fmt_ts(ts: int) -> str:
    """Format a Unix timestamp for human-readable log lines."""
    if not ts:
        return "never"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
    except (OSError, ValueError, OverflowError):
        return str(ts)



class WcdbBackend(AbstractWeChatBackend):
    """Native WCDB backend — database read + window send.

    Reads messages directly from WeChat's session.db via wcdb_api.dll
    with one-byte DRM patch. Sends via WeChatWindowController.

    Usage:
        backend = WcdbBackend(
            bot_display_name="机器人",
            groups=["摸鱼群"],
            poll_sec=1.0,
        )
        backend.start(my_callback)
    """

    def __init__(self,
                 bot_display_name: str = "",
                 groups: list[str] | None = None,
                 poll_sec: float = DEFAULT_POLL_SEC,
                 store=None,
                 config=None):
        self._bot_name = bot_display_name
        self._groups = groups or []
        self._poll_sec = poll_sec
        self._store = store  # MessageStore fallback for name resolution
        self._running = False
        self._client: Optional[WcdbNativeClient] = None
        self._window = WeChatWindowController()
        self._talker_ids: dict[str, str] = {}
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Thread safety: WCDB DLL (ctypes) may not be thread-safe internally.
        # All _client calls are serialized through this lock.
        self._client_lock = threading.Lock()
        # Callback thread pool — fire-and-forget AI calls so the poll loop
        # never blocks on a slow summarization.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # Guards `_pool` against the poll thread submitting while stop() is
        # closing it.  Without this the two race and the submit raises
        # RuntimeError("cannot schedule new futures after shutdown").
        self._pool_lock = threading.Lock()
        # Pools detached from `_pool` but not yet drained: stop() closes the
        # pool to new work, and the shutdown path still has to wait for the
        # callbacks it already queued.
        self._retired_pools: list[concurrent.futures.ThreadPoolExecutor] = []
        # Voice recognition pipeline (lazy-init when voice_asr_enabled)
        self._voice: Optional[object] = None
        # Media (emoji/appmsg/vision) pipeline (lazy-init when media_vision_enabled)
        self._media: Optional[object] = None
        self._voice_config = config
        # ── "Read empty" detection state ──────────────────────────
        # Window over which "we read nothing at all" is tolerated: it starts
        # at launch and restarts whenever the handle is rebuilt, since a
        # fresh handle legitimately knows nothing yet.
        self._read_watch_since = time.time()
        self._last_seen_ts: dict[str, int] = {}    # talker -> newest message we read
        self._session_ts: dict[str, int] = {}      # talker -> SessionTable last_timestamp
        self._stalled: set[str] = set()            # talkers we cannot read new messages from
        self._last_lag_check = 0.0
        self._lag_warned_at: dict[str, float] = {}
        self._stalled_since: Optional[float] = None   # when the stall started
        self._last_reinit_at = 0.0                    # throttle handle rebuilds
        self._no_hwnd_since: Optional[float] = None   # WeChat window missing since
        self._last_window_check = 0.0                 # throttle window re-scans

    # ── Public API ─────────────────────────────────────────────────

    def start(self, callback: MessageCallback) -> None:
        if not self._groups:
            logger.error("No groups configured. Set WECHAT_GROUPS in .env")
            return

        logger.info(
            "WcdbBackend starting (groups=%s, poll=%ss, bot=%r)",
            self._groups, self._poll_sec, self._bot_name,
        )

        # Init and open database
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB database opened successfully")
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to initialize WCDB: %s", e)
            # If init() allocated DLL/WCDB engine but open() failed,
            # clean up native resources so repeated retries don't leak.
            if self._client is not None:
                try:
                    self._client.close()
                    self._client = None
                except Exception:
                    pass
            try:
                from src.web.server import update_status
                update_status(running=False, error=str(e))
            except Exception:
                pass
            return

        # Resolve group talker IDs
        self._resolve_groups()

        if not self._talker_ids:
            logger.error("No groups resolved. Check WECHAT_GROUPS.")
            return

        # Don't replay the last page of history as if it were new.
        self._warm_dedup()

        # Pre-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window pre-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found — will retry on first send")

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="bot-cb-",
        )
        self._running = True
        consecutive_errors = 0

        # Import once to avoid per-iteration overhead
        from src.web.server import is_shutting_down as _is_shutting_down

        try:
            while self._running and not _is_shutting_down():
                try:
                    self._poll_cycle(callback)
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    consecutive_errors += 1

                    # After MAX_CONSECUTIVE_ERRORS consecutive failures,
                    # attempt full reinitialization (WeChat may have restarted).
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Hit %d consecutive errors — attempting "
                            "reinitialization...", consecutive_errors,
                        )
                        try:
                            self._reinitialize()
                            consecutive_errors = 0
                            continue
                        except Exception as reinit_err:
                            logger.error(
                                "Reinitialization failed: %s", reinit_err,
                            )
                            # Fall through to backoff; will retry next cycle.
                            push_error = str(reinit_err)
                            try:
                                from src.web.server import update_status
                                update_status(error=push_error)
                            except Exception:
                                pass

                    wait = min(2 ** min(consecutive_errors % MAX_CONSECUTIVE_ERRORS, 5), 30)
                    logger.warning(
                        "Poll error #%d (%s): %s. Retry in %ss...",
                        consecutive_errors, type(e).__name__, e, wait,
                    )
                    time.sleep(wait)
        finally:
            # Drain in-flight callbacks gracefully.  stop() may already have
            # detached the pool (it closes it without waiting so the UI stays
            # responsive), so the shutdown path has to finish draining what
            # was handed over rather than assume `_pool` is still there.
            with self._pool_lock:
                pools = self._retired_pools[:]
                self._retired_pools.clear()
                if self._pool is not None:
                    pools.append(self._pool)
                    self._pool = None
            for pool in pools:
                pool.shutdown(wait=True, cancel_futures=True)
            if self._client:
                self._client.close()
        logger.info("WcdbBackend stopped.")

    def send_text(self, chat_id: str, content: str) -> bool:
        if not content:
            return False

        group_name = self._talker_to_name(chat_id)
        if not group_name:
            logger.error("Cannot resolve chat_id=%s to group name", chat_id)
            return False

        return self._send_and_confirm(group_name, chat_id, content)

    def stop(self) -> None:
        """Signal shutdown and stop accepting callbacks.

        The pool is detached and closed here (without waiting) so a stopping
        bot cannot take new work, while ``_run``'s ``finally`` still drains
        whatever was already queued.  Leaving ``_pool`` pointing at a closed
        executor made the next submit raise
        ``RuntimeError: cannot schedule new futures after shutdown``.
        """
        self._running = False
        with self._pool_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                self._retired_pools.append(pool)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # ── Recovery ─────────────────────────────────────────────────────

    def _reinitialize(self) -> None:
        """Close and re-open the WCDB client after persistent errors.

        Called when the poll loop hits MAX_CONSECUTIVE_ERRORS consecutive
        failures — typically because WeChat was restarted and the DB handle
        or HWND became stale.
        """
        logger.warning("Reinitializing WCDB backend after consecutive errors...")
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB reinitialized successfully")
        except Exception as e:
            logger.error("WCDB reinitialization failed: %s", e)
            raise
        # Clear dedup set — WCDB may return messages with new IDs
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Forget what the old handle could see: the new one must be judged on
        # its own reads, otherwise it would stay "stalled" from the start.
        self._last_seen_ts.clear()
        self._session_ts.clear()
        self._stalled.clear()
        self._stalled_since = None
        self._read_watch_since = time.time()
        # Re-resolve groups (talker IDs may have changed)
        self._resolve_groups()
        # Re-seed with what is already stored, so the re-opened DB does not
        # replay its whole last page (and its images) as if it were new.
        self._warm_dedup()
        # Re-find WeChat window
        hwnd = self._window.find_hwnd(force=True)
        if hwnd:
            logger.info("WeChat window re-detected: HWND=%s", hwnd)
            self._no_hwnd_since = None
        else:
            logger.warning("WeChat window not found after reinit")
            # Restart the "window is gone" clock from the rebuild, so a
            # second rebuild cannot fire immediately after a failed one.
            self._no_hwnd_since = time.time()

    # ── Group resolution ────────────────────────────────────────────

    def _warm_dedup(self) -> None:
        """Seed the dedup set with message IDs already persisted.

        The set lives only in memory, so without this every restart treats
        the last polled page as brand new and runs the media pipeline over
        its historical images — the vision requests then delay the first
        real message (and its @-reply) by minutes.
        """
        if self._store is None or not self._talker_ids:
            return
        try:
            warm = 0
            for talker in self._talker_ids.values():
                for msg_id in self._store.get_recent_message_ids(
                    talker, limit=DEDUP_WARMUP_LIMIT,
                ):
                    if msg_id not in self._known_ids:
                        self._known_ids.add(msg_id)
                        warm += 1
            if warm:
                logger.info("Dedup warmed with %d stored message IDs", warm)
        except Exception as e:
            logger.warning("Dedup warmup failed (continuing): %s", e)

    def _resolve_groups(self) -> None:
        """Map configured group names to talker IDs from WCDB sessions.

        WCDB session records only contain usernames (e.g. 20968749111@chatroom).
        Display names must be resolved via the DLL's get_display_names() or
        the local nickname cache (WeChat contacts / manual overrides).
        """
        sessions = self._client.get_sessions()

        # Build a map of all @chatroom entries: username -> {name, member_count}
        all_chatrooms: dict[str, dict] = {}
        for s in sessions:
            username = str(s.get("username", "") or "")
            if not username.endswith("@chatroom"):
                continue

            # Try session-level display name fields (rarely populated)
            display = str(
                s.get("displayName") or s.get("displayname")
                or s.get("nickname") or s.get("display_name")
                or ""
            ).strip()
            if not display:
                # Fall back to DLL lookup (resolves via contacts DB + nicknames)
                display = self._client.resolve_nickname(username)
            if not display or display == username:
                # Last resort: try last_sender_display_name from session,
                # or use the numeric prefix of username as label
                display = str(s.get("last_sender_display_name", "") or "").strip()
                if not display or display == username:
                    display = username  # fallback

            # Get real member count from group member list
            member_count = 0
            try:
                members = self._client.get_group_members(username)
                if members:
                    member_count = len(members)
            except Exception:
                pass

            all_chatrooms[username] = {
                "name": display,
                "member_count": member_count,
            }

        if not all_chatrooms:
            logger.error(
                "No @chatroom sessions found in WCDB (total sessions: %d). "
                "Make sure WeChat is logged in and session.db is accessible.",
                len(sessions),
            )
            return

        auto_discover = (
            not self._groups
            or (len(self._groups) == 1 and self._groups[0].strip() in ("*", "all", ""))
        )

        if auto_discover:
            for username, info in all_chatrooms.items():
                self._talker_ids[info["name"]] = username
            logger.info(
                "Auto-discovered %d group chats: %s",
                len(self._talker_ids), list(self._talker_ids.keys()),
            )
            self._groups = list(self._talker_ids.keys())

        else:
            # Manual mode: match configured names against resolved display names
            for group_name in self._groups:
                found = None
                for username, info in all_chatrooms.items():
                    display = info["name"]
                    if group_name.lower() in display.lower() or display.lower() in group_name.lower():
                        found = username
                        break
                if found:
                    self._talker_ids[group_name] = found
                    logger.info("Resolved '%s' -> %s (display='%s')", group_name, found, all_chatrooms[found]["name"])
                else:
                    # Direct lookup: maybe group_name IS a username like 20968749111@chatroom
                    if group_name in all_chatrooms:
                        self._talker_ids[group_name] = group_name
                        logger.info("Resolved '%s' as direct username", group_name)
                    else:
                        logger.warning(
                            "Could not resolve group '%s'. Available: %s",
                            group_name, list(all_chatrooms.keys()),
                        )

        # Persist chat_id -> display_name so the web UI can show
        # human-readable group names in the nickname dropdown.
        if all_chatrooms:
            self._save_group_names(all_chatrooms)

        # ── Resolve and persist group members ──────────────────────────
        group_members: dict[str, dict[str, str]] = {}
        for username, info in all_chatrooms.items():
            try:
                members = self._client.get_group_members(username)
                if not members:
                    continue
                wxids = [m.get("username", "") for m in members if m.get("username")]
                if not wxids:
                    continue
                # Resolve display names in batches of 200
                names = {}
                for i in range(0, len(wxids), 200):
                    batch = wxids[i:i + 200]
                    names.update(self._client.get_display_names(batch))
                # Filter out unresolved (where name == wxid) and save
                group_members[username] = {
                    wxid: names.get(wxid, wxid)
                    for wxid in wxids
                }
                logger.info(
                    "Resolved %d/%d member names for %s",
                    len(group_members[username]), len(wxids), info["name"],
                )
            except Exception as e:
                logger.warning("Failed to resolve members for %s: %s", username, e)
        if group_members:
            self._save_group_members(group_members)

    @staticmethod
    def _save_group_members(chat_members: dict[str, dict[str, str]]) -> None:
        """Persist chat_id -> {wxid: display_name} to data/group_members.json."""
        import os as _os
        path = Path("data/group_members.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chat_members, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            total = sum(len(m) for m in chat_members.values())
            logger.info("Saved %d member names across %d groups to %s", total, len(chat_members), path)
        except Exception as e:
            logger.warning("Failed to persist group_members.json: %s", e)

    @staticmethod
    def _save_group_names(chatrooms: dict[str, dict]) -> None:
        """Persist chat_id -> {name, member_count} to data/group_names.json atomically."""
        import os as _os
        path = Path("data/group_names.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chatrooms, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            logger.info(
                "Saved %d group-name mappings to %s",
                len(chatrooms), path,
            )
        except Exception as e:
            logger.warning("Failed to persist group_names.json: %s", e)

    def _talker_to_name(self, talker_id: str) -> str:
        for name, tid in self._talker_ids.items():
            if tid == talker_id:
                return name
        return ""

    # ── Message polling ──────────────────────────────────────────────

    def _poll_cycle(self, callback: MessageCallback) -> None:
        for group_name in list(self._groups):
            if not self._running:
                break
            talker = self._talker_ids.get(group_name)
            if not talker:
                continue
            self._poll_group(group_name, talker, callback)
        # Make "cannot read new messages" visible instead of silent.
        self._maybe_check_read_lag()
        # A vanished window breaks replies even when reading still works.
        self._maybe_check_window()
        # Check shutdown signal before sleeping so stop() is responsive
        if not self._running:
            return
        time.sleep(self._poll_sec)

    def _poll_group(self, group_name: str, talker: str,
                    callback: MessageCallback) -> None:
        """Fetch messages for one group and dispatch new ones.

        AI-triggering callbacks are submitted to the thread pool so slow
        summarization in one group never blocks polling of other groups.
        """
        with self._client_lock:
            messages = self._client.get_messages(talker=talker, limit=50)
        if not messages:
            return

        # Remember how far we can actually see into this chat — compared
        # against SessionTable later to detect a frozen read path.
        newest = max(
            (int(m.get("create_time") or 0) for m in messages), default=0,
        )
        if newest > self._last_seen_ts.get(talker, 0):
            self._last_seen_ts[talker] = newest

        for msg in reversed(messages):
            if not self._running:
                break

            # Dedup BEFORE standardization: standardizing runs the media
            # pipeline (vision requests for images), which would otherwise
            # re-describe the same old pictures on every restart.
            pre_id = raw_message_id(msg)
            if pre_id in self._known_ids:
                continue

            standardized = self._standardize(msg, group_name, talker)
            if standardized is None:
                # Filtered (system notice, empty body...).  Remember it so
                # the next cycle does not standardize it again.
                self._known_ids.add(pre_id)
                continue

            msg_id = standardized["message_id"]
            self._known_ids.add(pre_id)
            if msg_id != pre_id and msg_id in self._known_ids:
                continue
            self._known_ids.add(msg_id)

            if self._bot_name and self._bot_name in standardized["sender_name"]:
                continue

            self._trim_dedup()

            # Fire-and-forget: callback (potentially AI call) + send run in
            # a thread pool worker so the poll loop continues immediately.
            # The submit happens under `_pool_lock`: stop() detaches and
            # closes the pool concurrently, and submitting into a closed
            # executor raises "cannot schedule new futures after shutdown".
            submitted = False
            with self._pool_lock:
                if self._pool is not None:
                    try:
                        self._pool.submit(
                            self._handle_message,
                            group_name, talker, standardized, callback,
                        )
                        submitted = True
                    except RuntimeError as e:
                        # Pool closed between the check and the submit.
                        logger.debug("Callback pool is closed: %s", e)
            if not submitted:
                # Fallback (shutting down): run inline so the message is not
                # silently dropped while the process is still alive.
                self._handle_message(
                    group_name, talker, standardized, callback,
                )

    def _maybe_check_read_lag(self) -> None:
        """Rate-limited wrapper around :meth:`_check_read_lag`."""
        now = time.time()
        if now - self._last_lag_check < LAG_CHECK_INTERVAL_SEC:
            return
        self._last_lag_check = now
        try:
            self._check_read_lag(now)
        except Exception as e:
            # Diagnosis must never break polling.
            logger.debug("Read-lag check failed: %s", e)

    def _check_read_lag(self, now: float) -> None:
        """Compare what we can read with what the session table says exists.

        ``get_messages()`` returns an empty list both for "no new messages"
        and for "this handle cannot read the message DB", and the non-DLL
        path silently degrades to the first case.  SessionTable records the
        last message time per chat, so a gap between the two means the read
        path is broken — which is exactly the state that used to look
        healthy while @-mentions were never delivered.
        """
        with self._client_lock:
            sessions = self._client.get_sessions()
        if not sessions:
            logger.warning(
                "Session table came back empty — cannot verify the read path"
            )
            return

        by_username = {
            str(s.get("username", "") or ""): s for s in sessions
        }

        stalled_now: set[str] = set()
        for group_name, talker in self._talker_ids.items():
            session = by_username.get(talker)
            if session is None:
                # The chat is not in the session list at all: either the
                # group was left, or the handle reads a different account.
                self._stalled.add(talker)
                stalled_now.add(talker)
                self._warn_lag(
                    talker,
                    f"group '{group_name}' ({talker}) is missing from the "
                    f"session table — the WCDB handle may belong to another "
                    f"WeChat account",
                    now,
                )
                continue

            try:
                session_ts = int(session.get("last_timestamp") or 0)
            except (TypeError, ValueError):
                session_ts = 0
            if session_ts <= 0:
                # Nothing has ever been said here; nothing to compare.
                continue
            self._session_ts[talker] = session_ts

            seen_ts = self._last_seen_ts.get(talker, 0)
            if seen_ts <= 0:
                # Never read a single message.  Give the (possibly rebuilt)
                # handle a grace period: its first poll may not have run yet.
                if now - self._read_watch_since > READ_GRACE_SEC:
                    self._stalled.add(talker)
                    stalled_now.add(talker)
                    self._warn_lag(
                        talker,
                        f"group '{group_name}' ({talker}) has messages up to "
                        f"{_fmt_ts(session_ts)} but none could be read — "
                        f"the message database is not being read",
                        now,
                    )
                continue

            lag = session_ts - seen_ts
            if lag > STALL_WARN_SEC:
                self._stalled.add(talker)
                stalled_now.add(talker)
                self._warn_lag(
                    talker,
                    f"group '{group_name}' ({talker}) is {lag // 60}m behind: "
                    f"session has messages up to {_fmt_ts(session_ts)}, "
                    f"last read was {_fmt_ts(seen_ts)} — new messages are "
                    f"not coming through",
                    now,
                )
            else:
                self._stalled.discard(talker)

        if stalled_now:
            logger.warning(
                "Read path stalled for %d of %d group(s)",
                len(stalled_now), len(self._talker_ids),
            )
            if self._stalled_since is None:
                self._stalled_since = now
            elif (now - self._stalled_since >= STALL_REINIT_SEC
                    and now - self._last_reinit_at >= STALL_REINIT_MIN_INTERVAL):
                # Stalled long enough to be a dead handle rather than a quiet
                # group.  Reads never raise in this state, so nothing else
                # would ever notice — rebuild the WCDB connection.
                logger.error(
                    "Read path stalled for %ds — rebuilding the WCDB handle",
                    int(now - self._stalled_since),
                )
                self._last_reinit_at = now
                self._stalled_since = None
                try:
                    self._reinitialize()
                except Exception as e:
                    # Diagnosis must not take polling down with it; the next
                    # cycle retries after STALL_REINIT_MIN_INTERVAL.
                    logger.error("Handle rebuild failed: %s", e)
        else:
            self._stalled_since = None

    def _maybe_check_window(self) -> None:
        """Re-scan for WeChat's window and recover when it stays gone.

        ``no_hwnd`` used to be a heartbeat string and nothing more: replies
        silently failed while the bot kept reporting itself as running.
        The window is re-scanned cheaply first (WeChat may have been
        minimised to tray and come back), and only a window that stays
        missing escalates to a full handle rebuild.
        """
        now = time.time()
        if now - self._last_window_check < LAG_CHECK_INTERVAL_SEC:
            return
        self._last_window_check = now

        try:
            hwnd = self._window.find_hwnd(force=True)
        except Exception as e:
            logger.debug("Window re-scan failed: %s", e)
            return

        if hwnd:
            if self._no_hwnd_since is not None:
                logger.info("WeChat window is back: HWND=%s", hwnd)
            self._no_hwnd_since = None
            return

        if self._no_hwnd_since is None:
            self._no_hwnd_since = now
            logger.warning(
                "WeChat window disappeared — replies cannot be sent until it "
                "comes back"
            )
            return

        gone_for = now - self._no_hwnd_since
        if gone_for < HWND_REINIT_SEC:
            return
        if now - self._last_reinit_at < STALL_REINIT_MIN_INTERVAL:
            return

        logger.error(
            "WeChat window has been missing for %ds — rebuilding the WCDB "
            "handle (WeChat was probably restarted or logged out)",
            int(gone_for),
        )
        self._last_reinit_at = now
        self._no_hwnd_since = None
        try:
            self._reinitialize()
        except Exception as e:
            logger.error("Handle rebuild failed: %s", e)

    def health_status(self) -> str:
        """One-line health for the heartbeat (``wechat=...`` in the log).

        ``no_hwnd`` means WeChat's window is gone; ``read_stalled`` means the
        message database is not delivering.  Both used to look healthy:
        reads returned empty instead of failing, so the only visible signal
        was that @-mentions silently stopped arriving.
        """
        try:
            hwnd = self._window._cached_hwnd
            has_hwnd = bool(hwnd) and self._window._validate_hwnd(hwnd)
        except Exception:
            has_hwnd = False

        if self._stalled:
            return f"read_stalled({len(self._stalled)})"
        if not has_hwnd:
            return "no_hwnd"
        return f"HWND_{hwnd}" if hwnd else "ok"

    def _warn_lag(self, talker: str, message: str, now: float) -> None:
        """Log a read-lag warning at most once per group per interval."""
        last = self._lag_warned_at.get(talker, 0.0)
        if now - last < STALL_WARN_REPEAT_SEC:
            return
        self._lag_warned_at[talker] = now
        logger.warning("%s", message)

    def read_progress(self) -> dict:
        """Snapshot of what polling can actually see (for heartbeats/UI).

        ``newest_seen`` is the newest message timestamp read from WCDB,
        ``newest_session`` what WeChat itself records for those chats, and
        ``lag_sec`` the gap between them.  A non-empty ``stalled_chats``
        means the read path is not delivering, even though polling appears
        healthy.
        """
        seen = {t: ts for t, ts in self._last_seen_ts.items() if ts > 0}
        newest_seen = max(seen.values(), default=0)
        newest_session = max(self._session_ts.values(), default=0)
        return {
            "newest_seen_ts": newest_seen,
            "newest_session_ts": newest_session,
            "lag_sec": (
                max(0, newest_session - newest_seen)
                if newest_seen and newest_session else 0
            ),
            "stalled_chats": sorted(self._stalled),
            "tracked_chats": len(self._talker_ids),
        }

    def _handle_message(self, group_name: str, talker: str,
                        standardized: dict, callback: MessageCallback) -> None:
        """Execute callback and send reply (runs in thread pool worker)."""
        if not self._running:
            return

        try:
            cb_start = time.monotonic()
            reply = callback(standardized)
            cb_elapsed = time.monotonic() - cb_start
            if cb_elapsed > 0.5:
                logger.debug(
                    "Callback took %.2fs (msg_id=%s, group='%s')",
                    cb_elapsed, standardized["message_id"], group_name,
                )

            if reply:
                logger.info(
                    "Reply ready: group='%s' sender='%s' len=%d",
                    group_name, standardized["sender_name"], len(reply),
                )
                # _send_and_confirm uses window_controller (keyboard), not
                # _client (WCDB).  Don't hold _client_lock during send —
                # it blocks the poll loop from reading new messages.
                success = self._send_and_confirm(group_name, talker, reply)
                if success:
                    logger.info(
                        "Reply sent: group='%s' (%d chars)",
                        group_name, len(reply),
                    )
                else:
                    logger.error(
                        "Reply FAILED: group='%s' — check WeChat window",
                        group_name,
                    )
        except Exception:
            logger.exception(
                "Unhandled error in callback worker (group='%s', sender='%s')",
                group_name, standardized.get("sender_name", "?"),
            )

    # ── Voice recognition helpers ────────────────────────────────────

    def _get_voice(self):
        """Lazy-init the VoicePipeline (avoids import unless enabled)."""
        if self._voice is not None:
            return self._voice
        if self._voice_config is None:
            self._voice = False  # Sentinel: no config → disabled
            return False
        try:
            from src.voice import VoicePipeline
            self._voice = VoicePipeline(self._voice_config)
        except Exception:
            logger.exception("VoicePipeline init failed — voice disabled")
            self._voice = False
        return self._voice

    def _try_voice(self, msg: dict) -> Optional[str]:
        """Attempt voice recognition; return text or None on failure."""
        voice = self._get_voice()
        if not voice:  # False or None → disabled
            return None
        try:
            return voice.process(msg)
        except Exception:
            logger.exception("VoicePipeline.process failed")
            return None

    # ── Media helpers ────────────────────────────────────────────────

    def _get_media(self):
        """Lazy-init the MediaPipeline (avoids import unless enabled)."""
        if self._media is not None:
            return self._media
        if self._voice_config is None:
            self._media = False  # Sentinel: no config → disabled
            return False
        try:
            from src.media import MediaPipeline
            self._media = MediaPipeline(self._voice_config)
        except Exception:
            logger.exception("MediaPipeline init failed — media disabled")
            self._media = False
        return self._media

    def _try_media(self, msg: dict, chat_id: str = "") -> Optional[str]:
        """Describe a media message; return text or None on failure."""
        media = self._get_media()
        if not media:  # False or None → disabled
            return None
        try:
            return media.process(msg, chat_id)
        except Exception:
            logger.exception("MediaPipeline.process failed")
            return None

    def _blob_text(self, msg: dict) -> str:
        """Decode a non-media BLOB body to plain chat text.

        WeChat compresses some ordinary text messages the same way it stores
        media.  A decoded payload that is markup is *not* chat text — it is a
        system notice or an appmsg body, which the media pipeline and the join
        filter deal with.  Returning markup here would put kilobytes of XML
        into the AI prompt, so those yield "" and the message is dropped,
        matching the behaviour before BLOB columns were emitted at all.

        The stored body is often prefixed with the sender id
        (``wxid_xxx:\\n<msg>...``), so the prefix is stripped before deciding
        whether the payload is markup.
        """
        raw = msg.get("message_content") or msg.get("content") or ""
        if not raw:
            return ""
        text = decode_blob(str(raw))
        if not text:
            return ""
        body = strip_sender_prefix(text).strip()
        if not body or looks_like_xml(body):
            return ""
        return body

    # ── Message standardization ──────────────────────────────────────

    def _standardize(self, msg: dict, group_name: str,
                     talker: str) -> Optional[dict]:
        """Convert WCDB raw message to standard format."""
        # WCDB message fields: sender_username, message_content, local_type, create_time
        sender = str(msg.get("sender_username", msg.get("senderUsername", msg.get("sender", ""))))
        local_type = int(msg.get("localType", msg.get("msg_type", 1)))

        # ── Binary bodies ─────────────────────────────────────────
        # The native layer emits BLOB columns base64-encoded.  That string
        # is a compressed XML payload, never message text — using it as
        # content would push kilobytes of base64 into the AI prompt.
        if is_binary(msg):
            content = ""
        else:
            content = str(msg.get("message_content", msg.get("content", ""))).strip()

        # ── Voice recognition ──────────────────────────────────────
        # Voice messages (localType=34) have empty message_content;
        # we must recognise them BEFORE the empty-content check below.
        if local_type == 34:
            voice_text = self._try_voice(msg)
            if voice_text:
                content = f"[语音] {voice_text}"
            else:
                content = "[语音]"
        elif local_type in _MEDIA_LOCAL_TYPES:
            # Media is described by MediaPipeline when enabled; otherwise
            # it degrades to a plain placeholder.
            content = self._try_media(msg, talker) or format_nontext_content(local_type)
        elif is_binary(msg):
            # A BLOB that is not media: WeChat compresses ordinary text and
            # system notices the same way.  Try the media pipeline first (it
            # reduces appmsg XML to a title), then fall back to plain text.
            # The raw base64 must never become content.
            content = self._try_media(msg, talker) or self._blob_text(msg)
            if not content:
                return None
        elif not content:
            return None

        # ── System message handling ───────────────────────────────
        # Extract "xxx joined the group" events → welcome feature.
        # Only active when WELCOME_ENABLED=true; otherwise join messages
        # are silently filtered out like other system messages.
        _JOIN_PATTERN = re.compile(r'"([^"]+)"(?:通过[^"]*)?加入了群聊')
        join_match = _JOIN_PATTERN.search(content)
        new_member_id: str = ""
        is_system_join: bool = False
        welcome_on = (
            self._voice_config is not None
            and getattr(self._voice_config, "welcome_enabled", False)
        )
        if join_match and welcome_on:
            new_member_id = join_match.group(1)
            is_system_join = True
            logger.info(
                "Join event detected: new_member=%s group=%s",
                new_member_id, group_name[:20],
            )
        elif join_match:
            # Welcome disabled — silently drop join messages
            return None

        # Filter other system messages (but NOT join events)
        if not is_system_join:
            _FILTER_KEYWORDS = (
                "修改群名", "退出了群聊",
                "撤回了一条消息", "被移除", "开启了朋友验证",
                "移出了群聊",
            )
            # NOTE: "邀请" intentionally NOT in this list — it would
            # false-positive filter normal chat like "我邀请你参加活动".
            # System invite messages ("xxx邀请yyy加入了群聊") are already
            # caught by _JOIN_PATTERN above.
            if any(kw in content for kw in _FILTER_KEYWORDS):
                return None

        # Parse timestamp
        ts = msg.get("create_time", msg.get("createTime", msg.get("timestamp", 0)))
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = int(time.time())

        # Resolve sender display name
        sender_name = self._client.resolve_nickname(sender)

        # Fallback: if WCDB DLL can't resolve (user not in contacts),
        # try the messages table for a previously seen display name
        if sender_name == sender and self._store is not None:
            prev = self._store.get_sender_display_name(sender)
            if prev:
                sender_name = prev

        # Resolve @mentions in content
        resolved_content = content
        if "@" in content:
            def _replace_at(match):
                at_wxid = match.group(0)[1:]
                name = self._client.resolve_nickname(at_wxid)
                return f"@{name}" if name != at_wxid else match.group(0)
            resolved_content = re.sub(r'@wxid_[a-zA-Z0-9]+', _replace_at, content)

        # Detect @mention of bot
        is_at = self._bot_name and (
            f"@{self._bot_name}" in resolved_content
            or f"@{self._bot_name}" in content
        )

        # Generate stable message ID.
        # For system join messages, use a content-based ID to avoid
        # volatile WCDB server_id/local_id causing dedup misses.
        # Otherwise reuse `raw_message_id` so the poll loop can compute the
        # same ID *before* standardization and skip seen messages.
        if is_system_join:
            raw_id = f"join|{talker}|{new_member_id}|{content}|{ts}"
            msg_id = hashlib.md5(str(raw_id).encode()).hexdigest()
        else:
            msg_id = raw_message_id(msg)

        return {
            "message_id": msg_id,
            "chat_id": talker,
            "group_name": group_name,
            "sender_id": str(sender),
            "sender_name": str(sender_name),
            "content": resolved_content,
            "msg_type": int(msg.get("localType", msg.get("msg_type", 1))),
            "timestamp": ts,
            "is_at_mentioned": is_at,
            "is_group": True,
            "is_system_join": is_system_join,
            "new_member_id": new_member_id,
        }

    def _trim_dedup(self) -> None:
        """DedupSet handles this internally."""
        pass

    # ── Message sending ──────────────────────────────────────────────

    def _send_and_confirm(self, group_name: str, talker: str,
                          content: str) -> bool:
        """Send via WeChatWindowController (fire-and-forget).

        Returns True if the keyboard send action completed successfully.
        No confirmation polling — the window controller already retries
        on failure, and polling WCDB adds 3s of latency for marginal gain.
        """
        return self._window.send_to_chat(group_name, content)
