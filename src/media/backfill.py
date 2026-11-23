"""Rewrite bare media placeholders in stored messages once the media is readable.

Why this exists: media is described *once*, at the moment the message is
standardized, and the result is what gets persisted.  When that description
fails — no image keys yet, WeChat had not written the file to disk, the
vision call came back empty — the row keeps a bare ``[图片]`` / ``[表情]``
forever.  Every later prompt then reads that placeholder and answers "我看不到
图", even though the picture is perfectly readable a minute later.

The fix is deliberately pull-based, not a startup sweep: only the messages
about to enter a prompt are revisited, and only a couple per prompt.  A full
sweep would hammer the vision API over weeks of history to answer questions
nobody asked.

Two properties matter:

* **The prompt that triggered the backfill sees the result.**  The context
  dicts are updated in place, so the AI call that follows reads the
  description rather than the placeholder.
* **A picture that stays unreadable is not retried on every message.**  The
  first failure for a given message is remembered, and later prompts skip
  it.  Without that, one broken sticker costs a vision request on every
  single @mention in the group.
"""

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: A message whose whole content is one bare placeholder: never described.
#: A described one carries text after the bracket and must be left alone.
_BARE_PLACEHOLDER_RE = re.compile(r"^\[(图片|表情)\]$")

#: Only these can be described after the fact.  Video and files have no
#: vision path, so revisiting them would only burn time.
_BACKFILLABLE_TYPES = {3, 47}

#: Bound on vision requests one prompt may cause.  Each one costs a couple
#: of seconds and real money, and the answer only needs a few of them.
MAX_PER_CALL = 3

#: How many recent raw messages to fetch when looking for the originals.
_RAW_PAGE_LIMIT = 200

#: Cap on remembered failures, so a long-running bot cannot grow this set
#: without bound.
_MAX_REMEMBERED_FAILURES = 500


class MediaBackfill:
    """Re-describe old media messages and write the result back to the DB.

    Usage::

        backfill = MediaBackfill(store, pipeline_provider, raw_message_fetcher)
        backfill.refresh(chat_id, context_messages)
        # → context_messages[...]["content"] now holds descriptions, and the
        #   same content is persisted for later prompts.
    """

    def __init__(self, store, pipeline_provider: Callable[[], object],
                 raw_message_fetcher: Callable[[str, int], list]) -> None:
        """
        Args:
            store: MessageStore, for rewriting a message's content.
            pipeline_provider: Returns the MediaPipeline, or None/False when
                media handling is disabled.  Called lazily so an unused
                pipeline is never built.
            raw_message_fetcher: ``(chat_id, limit) -> list[raw dict]``.
                The raw WCDB message still carries the ``<img>`` / emoji XML
                the pipeline needs; the stored row does not.
        """
        self._store = store
        self._pipeline_provider = pipeline_provider
        self._fetch_raw = raw_message_fetcher
        self._failed: set[str] = set()
        self._done = 0

    # -- Public ------------------------------------------------------------

    def refresh(self, chat_id: str, messages: list[dict]) -> int:
        """Describe bare placeholders in *messages*, in place.

        Returns the number of messages that gained a description.  Never
        raises: a backfill is an improvement, and failing it must not break
        the reply the user is waiting for.
        """
        try:
            pending = [
                m for m in messages
                if self._needs_backfill(m)
            ][:MAX_PER_CALL]
            if not pending:
                return 0

            pipeline = self._pipeline_provider()
            if not pipeline:
                return 0

            raw_by_id = self._raw_index(chat_id)
            if not raw_by_id:
                return 0

            recovered = 0
            for msg in pending:
                msg_id = str(msg.get("message_id", ""))
                raw = raw_by_id.get(msg_id)
                if raw is None:
                    # Not in the page we can see (older than the fetch
                    # window, or a chat we lost the handle for).  Do *not*
                    # mark it as failed: it may well be reachable next time,
                    # and a wrong permanent "give up" costs the user the
                    # picture for the rest of the run.
                    continue
                try:
                    text = pipeline.process(raw, chat_id)
                except Exception:
                    logger.debug(
                        "Media backfill failed for %s", msg_id, exc_info=True,
                    )
                    self._remember_failure(msg_id)
                    continue

                if not text or _BARE_PLACEHOLDER_RE.match(text.strip()):
                    self._remember_failure(msg_id)
                    continue

                msg["content"] = text
                if self._store.update_message_content(msg_id, text):
                    recovered += 1
                    self._done += 1
                    logger.info(
                        "Media backfill: described %s in %s",
                        msg_id[:12], chat_id[:24],
                    )
                else:
                    logger.debug(
                        "Media backfill: row not updated for %s", msg_id,
                    )
            return recovered
        except Exception:
            logger.exception("Media backfill crashed (continuing)")
            return 0

    @property
    def stats(self) -> dict:
        """Counters for diagnostics."""
        return {"recovered": self._done, "given_up": len(self._failed)}

    # -- Internals ---------------------------------------------------------

    def _needs_backfill(self, msg: dict) -> bool:
        """True when this stored message is a bare placeholder worth retrying."""
        try:
            if int(msg.get("msg_type", 0)) not in _BACKFILLABLE_TYPES:
                return False
        except (TypeError, ValueError):
            return False
        content = str(msg.get("content", "") or "").strip()
        if not _BARE_PLACEHOLDER_RE.match(content):
            return False
        return str(msg.get("message_id", "")) not in self._failed

    def _raw_index(self, chat_id: str) -> dict[str, dict]:
        """Raw messages of this chat, keyed by the ID the store uses.

        The stored ``message_id`` *is* :func:`raw_message_id` for this raw
        message, so hashing the freshly read page reproduces the key without
        guessing from timestamps.
        """
        from src.wechat.wcdb_backend import raw_message_id

        try:
            raw_messages = self._fetch_raw(chat_id, _RAW_PAGE_LIMIT)
        except Exception:
            logger.debug("Media backfill: cannot read raw messages", exc_info=True)
            return {}
        index = {}
        for raw in raw_messages or []:
            try:
                index[raw_message_id(raw)] = raw
            except Exception:
                continue
        return index

    def _remember_failure(self, msg_id: str) -> None:
        if len(self._failed) >= _MAX_REMEMBERED_FAILURES:
            # Cheap bound: the set is only a cost guard, not a record.
            self._failed.clear()
        self._failed.add(msg_id)


__all__ = ["MediaBackfill", "MAX_PER_CALL"]
