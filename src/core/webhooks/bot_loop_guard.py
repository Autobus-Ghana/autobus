"""Keep Autobus inboxes from auto-replying to each other forever.

Two merchants who both connect an Instagram account see each other's bot
replies as fresh customer messages, so every greeting triggers another one.
A person typing from another connected account must still get answered, so
only messages that repeat a reply Autobus itself just sent are dropped.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _reply_key(text: str) -> str:
    normalized = " ".join(str(text or "").split()).lower()
    if not normalized:
        return ""
    return "bot_reply:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def remember_bot_reply(text: str) -> None:
    from utilities.rate_limit import _redis

    key = _reply_key(text)
    client = _redis()
    if not key or client is None:
        return
    try:
        client.set(key, "1", ex=_env_int("BOT_REPLY_MEMORY_SECONDS", 600))
    except Exception as exc:
        logger.warning("[bot-loop] could not remember reply: %s", exc)


def is_recent_bot_reply(text: str) -> bool:
    """True when the inbound text is a reply some Autobus inbox sent moments ago."""
    from utilities.rate_limit import _redis

    key = _reply_key(text)
    client = _redis()
    if not key or client is None:
        return False
    try:
        return bool(client.exists(key))
    except Exception as exc:
        logger.warning("[bot-loop] could not check reply memory: %s", exc)
        return False


def reply_budget_ok(conversation_key: str) -> bool:
    """False once a conversation gets more automatic replies than a person could prompt."""
    from utilities.rate_limit import hit

    limit = _env_int("AUTO_REPLY_BURST_LIMIT", 15)
    window = _env_int("AUTO_REPLY_BURST_WINDOW_SECONDS", 300)
    allowed, _, retry_after = hit(
        f"auto_reply:{conversation_key}", limit=limit, window_seconds=window
    )
    if not allowed:
        logger.warning(
            "[bot-loop] pausing auto replies for %s for %ss (over %s in %ss)",
            conversation_key,
            retry_after,
            limit,
            window,
        )
    return allowed
