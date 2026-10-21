import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'umum',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_profile (
    user_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    value REAL,
    unit TEXT,
    note TEXT,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_logs_user ON health_logs(user_id, logged_at);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'sekali' | 'harian'
    run_at TEXT,                 -- ISO datetime, untuk 'sekali'
    time_of_day TEXT,            -- HH:MM, untuk 'harian'
    days TEXT,                   -- JSON list 1-7 (Senin=1 ... Minggu=7), untuk 'harian'
    use_ai INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(config.TIMEZONE)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with self.lock:
                self.conn.backup(target)
        finally:
            target.close()

    # ---------- riwayat obrolan ----------
    def add_message(self, user_id: int, role: str, content: str) -> None:
        self._run(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now_iso()),
        )

    def get_history(self, user_id: int, limit: int) -> list[dict]:
        rows = self._query(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return list(reversed(rows))

    def clear_history(self, user_id: int) -> None:
        self._run("DELETE FROM messages WHERE user_id = ?", (user_id,))

    # ---------- memori ----------
    def add_memory(self, user_id: int, content: str, category: str) -> int:
        ts = now_iso()
        cur = self._run(
            "INSERT INTO memories (user_id, content, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, content, category, ts, ts),
        )
        return cur.lastrowid

    def update_memory(self, user_id: int, memory_id: int, content: str) -> bool:
        cur = self._run(
            "UPDATE memories SET content = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (content, now_iso(), memory_id, user_id),
        )
        return cur.rowcount > 0

    def delete_memory(self, user_id: int, memory_id: int) -> bool:
        cur = self._run("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id))
        return cur.rowcount > 0

    def list_memories(self, user_id: int, limit: int = 200) -> list[dict]:
        return self._query(
            "SELECT id, category, content, updated_at FROM memories WHERE user_id = ? "
            "ORDER BY category, id LIMIT ?",
            (user_id, limit),
        )

    def search_memories(self, user_id: int, query: str, limit: int = 20) -> list[dict]:
        words = [w for w in query.lower().split() if len(w) > 1]
        if not words:
            return []
        score = " + ".join(["(LOWER(content) LIKE ?)"] * len(words))
        params = tuple(f"%{w}%" for w in words)
        return self._query(
            f"SELECT id, category, content, updated_at, ({score}) AS score FROM memories "
            f"WHERE user_id = ? AND ({score}) > 0 ORDER BY score DESC, id DESC LIMIT ?",
            params + (user_id,) + params + (limit,),
        )

    # ---------- kesehatan ----------
    def get_health_profile(self, user_id: int) -> dict:
        rows = self._query("SELECT data FROM health_profile WHERE user_id = ?", (user_id,))
        return json.loads(rows[0]["data"]) if rows else {}

    def update_health_profile(self, user_id: int, fields: dict) -> dict:
        profile = self.get_health_profile(user_id)
        profile.update({k: v for k, v in fields.items() if v is not None})
        self._run(
            "INSERT INTO health_profile (user_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (user_id, json.dumps(profile, ensure_ascii=False), now_iso()),
        )
        return profile

    def add_health_log(self, user_id: int, type_: str, value, unit, note, logged_at: str) -> int:
        cur = self._run(
            "INSERT INTO health_logs (user_id, type, value, unit, note, logged_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, type_, value, unit, note, logged_at),
        )
        return cur.lastrowid

    def delete_health_log(self, user_id: int, log_id: int) -> bool:
        cur = self._run("DELETE FROM health_logs WHERE id = ? AND user_id = ?", (log_id, user_id))
        return cur.rowcount > 0

    def get_health_logs(self, user_id: int, since_iso: str, type_: str | None = None) -> list[dict]:
        sql = "SELECT id, type, value, unit, note, logged_at FROM health_logs WHERE user_id = ? AND logged_at >= ?"
        params: tuple = (user_id, since_iso)
        if type_:
            sql += " AND type = ?"
            params += (type_,)
        return self._query(sql + " ORDER BY logged_at", params)

    # ---------- pengingat ----------
    def add_reminder(self, user_id, chat_id, message, kind, run_at, time_of_day, days, use_ai) -> int:
        cur = self._run(
            "INSERT INTO reminders (user_id, chat_id, message, kind, run_at, time_of_day, days, use_ai, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, chat_id, message, kind, run_at, time_of_day,
             json.dumps(days) if days else None, int(use_ai), now_iso()),
        )
        return cur.lastrowid

    def list_reminders(self, user_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM reminders WHERE user_id = ? AND active = 1 ORDER BY id", (user_id,)
        )

    def all_active_reminders(self) -> list[dict]:
        return self._query("SELECT * FROM reminders WHERE active = 1")

    def deactivate_reminder(self, user_id: int, reminder_id: int) -> bool:
        cur = self._run(
            "UPDATE reminders SET active = 0 WHERE id = ? AND user_id = ? AND active = 1",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0
