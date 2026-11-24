"""Rolling digest of the conversation that fell out of the live window.

The chat context handed to the model is bounded — a few dozen turns
(:data:`src.summarize.base.MAX_CHAT_CONTEXT_TURNS`).  Anything older would
be invisible even though the group keeps referring to it ("那事儿最后咋样
了").  This module keeps a compact summary of exactly that already-scrolled
region and lets the prompt say "更早的对话概要".

Cost discipline is the whole point: compressing on every reply would add a
second AI call per message.  A digest is rebuilt only when at least
:data:`DIGEST_MIN_NEW_MSGS` un-digested messages exist outside the window,
so with a 40-turn window that is at most one extra call per ~30 messages.

Failure never propagates — a stale digest is fine, a broken reply is not.
"""

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..db.store import MessageStore

logger = logging.getLogger(__name__)

#: Un-digested messages outside the window needed to justify a rebuild.
#: Below this the cached digest is used as-is.
DIGEST_MIN_NEW_MSGS = 30

#: Messages consumed per rebuild, oldest first.  Bounded so one busy
#: afternoon cannot produce a gigantic summarization call.
DIGEST_BATCH_SIZE = 120

#: How much already-digested detail to carry forward when merging, so the
#: digest stays a summary instead of growing without bound.
DIGEST_MAX_CHARS = 1200

DIGEST_PROMPT = """\
你是微信群里的 AI 聊天助手。下面有一段群聊，请把它压缩成一份简短纪要。

## 已有的纪要（更早的内容）：
{existing}

## 需要并入的新对话：
{messages}

要求：
- 用第一人称、自然段落，中文。
- 只留会被人再提起的内容：谁在做什么、结论是什么、定下的约定、群里的梗、人物关系。
- 寒暄、表情、重复刷屏直接丢掉。
- 不要罗列每条消息，不要编造没出现的信息。
- {max_chars} 字以内。

直接输出新的完整纪要。"""


class ChatDigest:
    """Keeps :mod:`chat_digest` in step with what the window forgets.

    Usage (once per @mention, before building the prompt)::

        digest = ChatDigest(store, summarize_fn).refresh(chat_id, cutoff_ts)
    """

    def __init__(self, store: "MessageStore",
                 summarize_fn: Callable[[str, str], str]):
        """
        Args:
            store: MessageStore for reads and digest persistence.
            summarize_fn: ``(system_prompt, user_prompt) -> str``; a cheap
                model call.  Failures may raise — they are caught here.
        """
        self._store = store
        self._summarize = summarize_fn

    def refresh(self, chat_id: str, window_start_ts: int) -> str:
        """Return the digest text to inject, rebuilding it if warranted.

        Args:
            chat_id: The group.
            window_start_ts: Timestamp where the live context window
                begins.  Everything before it is fair game for the digest.

        Returns:
            Digest text (possibly empty).  Never raises.
        """
        try:
            return self._refresh_impl(chat_id, window_start_ts)
        except Exception:
            logger.exception(
                "Chat digest refresh failed for %s (continuing)", chat_id[:24],
            )
            return self._cached(chat_id)

    def _cached(self, chat_id: str) -> str:
        try:
            row = self._store.get_chat_digest(chat_id)
            return (row or {}).get("digest_text", "") or ""
        except Exception:
            return ""

    def _refresh_impl(self, chat_id: str, window_start_ts: int) -> str:
        row = self._store.get_chat_digest(chat_id) or {}
        existing = row.get("digest_text", "") or ""
        last_id = row.get("last_message_id")

        # Everything strictly older than the live window.
        older = self._store.get_messages_before(
            chat_id, window_start_ts, limit=DIGEST_BATCH_SIZE + 1,
        )
        pending = self._after_cursor(chat_id, older, last_id)
        if len(pending) < DIGEST_MIN_NEW_MSGS:
            return existing

        batch = pending[:DIGEST_BATCH_SIZE]
        messages_text = "\n".join(
            f"{m.get('sender_name', '?')}: {m.get('content', '')}"
            for m in batch if m.get("content")
        )
        if not messages_text:
            return existing

        system_prompt = DIGEST_PROMPT.format(
            existing=(existing or "（暂无）").replace("{", "{{").replace("}", "}}"),
            messages=messages_text.replace("{", "{{").replace("}", "}}"),
            max_chars=DIGEST_MAX_CHARS,
        )
        updated = (self._summarize(
            system_prompt, "请输出更新后的完整纪要。",
        ) or "").strip()
        if not updated:
            return existing
        if len(updated) > DIGEST_MAX_CHARS:
            updated = updated[:DIGEST_MAX_CHARS]

        self._store.upsert_chat_digest(
            chat_id, updated, batch[-1].get("message_id"),
        )
        logger.info(
            "Chat digest rebuilt for %s: %d msgs → %d chars",
            chat_id[:24], len(batch), len(updated),
        )
        return updated

    def _after_cursor(self, chat_id: str, messages: list[dict],
                      last_id: str | None) -> list[dict]:
        """Drop the messages already folded into the digest."""
        if not last_id:
            return messages
        seen = False
        rest: list[dict] = []
        for m in messages:
            if seen:
                rest.append(m)
            elif m.get("message_id") == last_id:
                seen = True
        # An unknown cursor (old message pruned) means start over rather
        # than silently digesting the same rows twice.
        return rest if seen else messages
