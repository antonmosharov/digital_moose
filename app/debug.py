"""Read-only, bounded diagnostic snapshots with configured secrets removed."""

import json
import os
import re
from urllib.parse import quote

from app.news import usage
from app.store import Store


def telemetry(
    store: Store, bot, *, limit: int, before: int | None, chat_id: int | None, token: str
) -> dict:
    settings = store.settings()
    rows = store.db.execute(
        """SELECT rowid AS cursor, * FROM messages
           WHERE rowid < ? AND (? IS NULL OR chat_id=?) ORDER BY rowid DESC LIMIT ?""",
        (before if before is not None else 9223372036854775807, chat_id, chat_id, limit + 1),
    ).fetchall()
    history = []
    for row in rows[:limit]:
        content = json.loads(store.cipher.decrypt(row["value"]))
        history.append(
            {
                "cursor": row["cursor"],
                "chat_id": row["chat_id"],
                "thread_id": row["thread_id"],
                "message_id": row["message_id"],
                "sent_at": row["sent_at"],
                "sender_id": row["sender_id"],
                "is_bot": bool(row["is_bot"]),
                "who": content["who"],
                "text": content["text"][:20000],
                "reply_to": content["reply_to"],
                "media": content["media"],
            }
        )
    result = {
        "generated_at": store.now(),
        "settings": store.public_settings(),
        "bot": {"status": bot.status, "error": bot.error},
        "activity": store.activity(),
        "chats": store.chats(),
        "conversations": store.conversations(),
        "news_usage": usage(store),
        "proactive_attempts": [
            dict(r)
            for r in store.db.execute(
                "SELECT * FROM proactive_attempts ORDER BY attempted_at DESC LIMIT 100"
            )
        ],
        "history": history,
        "has_more": len(rows) > limit,
        "next_before": history[-1]["cursor"] if history else None,
    }
    secrets = {
        settings.bot_token,
        settings.api_key,
        settings.news_api_key,
        os.getenv("ADMIN_PASSWORD", ""),
        token,
    }
    secrets |= {quote(value, safe="") for value in secrets if value}

    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(filter(None, secrets), key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(api_token=)[^&\s]+", r"\1[REDACTED]", value)
            value = re.sub(r"moose_debug_[A-Za-z0-9_-]+", "[REDACTED]", value)
        return value

    return redact(result)
