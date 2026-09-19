import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings
from app.prompts import attachments


class Store:
    def __init__(self, directory: str):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = path / "secret.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.db = sqlite3.connect(path / "moose.db", check_same_thread=False)
        os.chmod(path / "moose.db", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                allowed INTEGER NOT NULL DEFAULT 0, seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                chat TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                duration REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, sent_at REAL NOT NULL,
                is_bot INTEGER NOT NULL, sender_id INTEGER NOT NULL, value BLOB NOT NULL,
                PRIMARY KEY (chat_id, thread_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS messages_time ON messages(sent_at);
            CREATE TABLE IF NOT EXISTS conversation_state (
                chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, sent_at REAL NOT NULL,
                is_bot INTEGER NOT NULL, human_id INTEGER,
                PRIMARY KEY (chat_id, thread_id)
            );
            CREATE TABLE IF NOT EXISTS opportunities (
                chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                kind TEXT NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_id, kind)
            );
            CREATE TABLE IF NOT EXISTS proactive_attempts (
                chat_id INTEGER NOT NULL, attempted_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_chat ON proactive_attempts(chat_id, attempted_at);
        """)
        self.db.commit()

    def settings(self) -> Settings:
        row = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return Settings.model_validate_json(self.cipher.decrypt(row[0])) if row else Settings()

    def save_settings(self, settings: Settings):
        value = self.cipher.encrypt(settings.model_dump_json().encode())
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (value,))
        self.db.commit()

    def public_settings(self):
        settings = self.settings().model_dump()
        for key in ("bot_token", "api_key", "news_api_key"):
            settings[f"has_{key}"] = bool(settings.pop(key))
        return settings

    def read_consciousness(self) -> str:
        return self.settings().consciousness

    def write_consciousness(self, content: str, previous: str) -> bool:
        # Synchronous read/check/write: no request can interleave on the event loop.
        settings = self.settings()
        if settings.consciousness != previous:
            return False
        updated = Settings.model_validate({**settings.model_dump(), "consciousness": content})
        self.save_settings(updated)
        return True

    def observe_chat(self, chat: dict):
        self.db.execute(
            """INSERT INTO chats VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, kind=excluded.kind,
            seen=excluded.seen""",
            (
                chat["id"],
                chat.get("title") or chat.get("first_name") or str(chat["id"]),
                chat.get("type", "supergroup"),
                self.now(),
            ),
        )
        self.db.commit()

    def permit_chat(self, chat_id: int, title: str, allowed: bool):
        self.db.execute(
            """INSERT INTO chats VALUES (?, ?, 'supergroup', ?, ?)
            ON CONFLICT(id) DO UPDATE SET allowed=excluded.allowed, title=excluded.title""",
            (chat_id, title or str(chat_id), int(allowed), self.now()),
        )
        self.db.commit()
        if not allowed:
            self.forget_chat(chat_id)

    def forget_chat(self, chat_id: int):
        for table in ("messages", "conversation_state", "opportunities", "proactive_attempts"):
            self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def remember(self, message: dict, *, is_bot: bool = False):
        """Encrypt text and Telegram file references; never store media bytes."""
        if not isinstance(message.get("message_id"), int):
            return
        chat_id = message["chat"]["id"]
        thread_id = message.get("message_thread_id", 0)
        sender = message.get("sender_chat") or message.get("from", {})
        name = (
            sender.get("title")
            or " ".join(sender[k] for k in ("first_name", "last_name") if sender.get(k))
            or sender.get("username")
            or str(sender.get("id", "Unknown"))
        )
        text = message.get("text") or message.get("caption") or ""
        media = [{"name": a["file_name"], "type": a["mime_type"]} for a in attachments(message)]
        for kind in ("poll", "contact", "location", "venue", "dice"):
            if message.get(kind):
                media.append({"type": kind})
        if not text and not media:
            return  # Membership/service events aren't conversational turns.
        value = {
            "who": name,
            "text": text,
            "media": media,
            "reply_to": message.get("reply_to_message", {}).get("message_id"),
            "attachments": attachments(message),
        }
        self.db.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                thread_id,
                message["message_id"],
                message.get("date", time.time()),
                int(is_bot or sender.get("is_bot", False)),
                sender.get("id", 0),
                self.cipher.encrypt(json.dumps(value).encode()),
            ),
        )
        bot = bool(is_bot or sender.get("is_bot", False))
        self.db.execute(
            """INSERT INTO conversation_state VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                 message_id=MAX(conversation_state.message_id, excluded.message_id),
                 sent_at=CASE WHEN excluded.message_id > conversation_state.message_id
                   THEN excluded.sent_at ELSE conversation_state.sent_at END,
                 is_bot=CASE WHEN excluded.message_id > conversation_state.message_id
                   THEN excluded.is_bot ELSE conversation_state.is_bot END,
                 human_id=MAX(COALESCE(conversation_state.human_id, 0), COALESCE(excluded.human_id, 0))""",
            (
                chat_id,
                thread_id,
                message["message_id"],
                message.get("date", time.time()),
                int(bot),
                None if bot else message["message_id"],
            ),
        )
        self.db.commit()

    def prune_history(self, settings: Settings, now: float):
        self.db.execute(
            "DELETE FROM messages WHERE sent_at < ?",
            (now - settings.history_retention_days * 86400,),
        )
        self.db.execute("DELETE FROM proactive_attempts WHERE attempted_at < ?", (now - 8 * 86400,))
        self.db.commit()

    def media_attachment(self, chat_id: int, thread_id: int, message_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT value FROM messages WHERE chat_id=? AND thread_id=? AND message_id=?",
            (chat_id, thread_id, message_id),
        ).fetchone()
        if not row:
            return None
        items = json.loads(self.cipher.decrypt(row[0])).get("attachments", [])
        return items[0] if items else None

    def participation_context(self, chat_id: int, thread_id: int, now: float) -> list[dict]:
        rows = self.db.execute(
            """SELECT value, is_bot FROM messages
               WHERE chat_id=? AND thread_id=? AND sent_at BETWEEN ? AND ?
               ORDER BY message_id DESC LIMIT 10""",
            (chat_id, thread_id, now - 3600, now),
        ).fetchall()
        return [
            {
                "text": json.loads(self.cipher.decrypt(row["value"]))["text"],
                "is_bot": bool(row["is_bot"]),
            }
            for row in reversed(rows)
        ]

    def history(
        self,
        chat_id: int,
        thread_id: int,
        before: int,
        limit: int = 10,
        since: float = 0,
        until: float | None = None,
    ) -> dict:
        limit = max(1, min(limit, 50))
        rows = self.db.execute(
            """SELECT * FROM messages WHERE chat_id=? AND thread_id=? AND message_id < ?
               AND sent_at >= ? AND sent_at <= ? ORDER BY message_id DESC LIMIT ?""",
            (
                chat_id,
                thread_id,
                before,
                since,
                until if until is not None else time.time(),
                limit + 1,
            ),
        ).fetchall()
        page = []
        for row in reversed(rows[:limit]):
            value = json.loads(self.cipher.decrypt(row["value"]))
            text = value["text"]
            page.append(
                {
                    "message_id": row["message_id"],
                    "reference": f"telegram:{chat_id}:{thread_id}:{row['message_id']}",
                    "time": datetime.fromtimestamp(row["sent_at"], UTC).isoformat(),
                    "who": value["who"],
                    "is_bot": bool(row["is_bot"]),
                    "text": text[:4000] + (" [truncated]" if len(text) > 4000 else ""),
                    "media": value["media"],
                    "reply_to": value["reply_to"],
                }
            )
        return {
            "messages": page,
            "next_before_message_id": page[0]["message_id"] if page else None,
            "has_more": len(rows) > limit,
        }

    def conversations(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT m.*, c.kind, c.title FROM conversation_state m JOIN chats c ON c.id=m.chat_id"
            )
        ]

    def claim_opportunity(self, chat_id: int, thread_id: int, kind: str, message_id: int) -> bool:
        cursor = self.db.execute(
            """INSERT INTO opportunities VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id, thread_id, kind) DO UPDATE SET message_id=excluded.message_id
               WHERE opportunities.message_id < excluded.message_id""",
            (chat_id, thread_id, kind, message_id),
        )
        self.db.commit()
        return bool(cursor.rowcount)

    def proactive_available(
        self, chat_id: int, now: float, day_start: float, settings: Settings
    ) -> bool:
        row = self.db.execute(
            """SELECT MAX(attempted_at), SUM(attempted_at >= ?) FROM proactive_attempts
               WHERE chat_id=?""",
            (day_start, chat_id),
        ).fetchone()
        return (row[0] is None or now - row[0] >= settings.proactive_cooldown_hours * 3600) and (
            row[1] or 0
        ) < settings.proactive_daily_limit

    def record_proactive_attempt(self, chat_id: int, now: float):
        self.db.execute("INSERT INTO proactive_attempts VALUES (?, ?)", (chat_id, now))
        self.db.commit()

    def allowed(self, chat: dict, settings: Settings) -> bool:
        if chat.get("type") == "private":
            return settings.allow_private
        row = self.db.execute("SELECT allowed FROM chats WHERE id=?", (chat["id"],)).fetchone()
        return bool(row and row[0])

    def chats(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM chats ORDER BY seen DESC")]

    def log(self, chat: str, status: str, detail: str, duration: float = 0):
        self.db.execute(
            "INSERT INTO activity(time, chat, status, detail, duration) VALUES (?, ?, ?, ?, ?)",
            (self.now(), chat, status, detail[:500], duration),
        )
        self.db.execute("DELETE FROM activity WHERE id <= (SELECT MAX(id)-1000 FROM activity)")
        self.db.commit()

    def activity(self):
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 100")
        ]

    def state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
        self.db.commit()

    @staticmethod
    def now():
        return datetime.now(UTC).isoformat()
