import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings


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
        for key in ("bot_token", "api_key"):
            settings[f"has_{key}"] = bool(settings.pop(key))
        return settings

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
