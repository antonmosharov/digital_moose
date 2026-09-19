import sqlite3

from app.store import Store


def test_activity_migration_and_encrypted_reply_details(tmp_path):
    db = sqlite3.connect(tmp_path / "moose.db")
    db.execute("""CREATE TABLE activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, chat TEXT NOT NULL,
        status TEXT NOT NULL, detail TEXT NOT NULL, duration REAL NOT NULL DEFAULT 0)""")
    db.execute(
        "INSERT INTO activity(time, chat, status, detail) VALUES ('old', 'Test', 'success', 'Replied')"
    )
    db.commit()
    db.close()
    store = Store(str(tmp_path))
    assert store.activity()[0]["reply_messages"] == []
    replies = ["Private reply " + "я" * 1500, "Second message\n<script>untrusted</script>"]
    tools = ["read_news", "read_news", "write_consciousness"]
    store.log("Test", "success", "Replied", reply_messages=replies, tools_used=tools)
    raw = store.db.execute("SELECT payload FROM activity ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert b"Private reply" not in raw and b"read_news" not in raw
    store.db.close()
    reopened = Store(str(tmp_path))
    event = reopened.activity()[0]
    assert event["reply_messages"] == replies
    assert event["tools_used"] == tools
    assert "payload" not in event
    reopened.db.close()
