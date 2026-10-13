"""SQLite storage: conversations, leads and the questions the site could not answer."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, page_url TEXT DEFAULT '',
    lead_prompted INTEGER DEFAULT 0, lead_reason TEXT DEFAULT '', lead_id INTEGER);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL, day TEXT NOT NULL, meta TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day, role);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, name TEXT DEFAULT '', email TEXT NOT NULL,
    phone TEXT DEFAULT '', company TEXT DEFAULT '', message TEXT DEFAULT '', reason TEXT DEFAULT '',
    interest TEXT DEFAULT '', source TEXT DEFAULT 'form', consent INTEGER DEFAULT 0, page_url TEXT DEFAULT '',
    transcript TEXT DEFAULT '[]', created_at TEXT NOT NULL,
    webhook_status TEXT DEFAULT 'not configured', webhook_attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS unanswered (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, question TEXT NOT NULL, best_score REAL,
    created_at TEXT NOT NULL);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """One connection guarded by a lock: chat traffic of one website is far below what SQLite handles."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")   # readers do not block the writer
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or 0

    def _read(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # -- conversations --
    def touch_session(self, session_id: str, page_url: str = "") -> dict:
        self._write("INSERT OR IGNORE INTO sessions(session_id, created_at, page_url) VALUES (?,?,?)",
                    (session_id, _now(), page_url[:500]))
        return self._read("SELECT * FROM sessions WHERE session_id=?", (session_id,))[0]

    def session(self, session_id: str) -> dict | None:
        rows = self._read("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return rows[0] if rows else None

    def mark_lead_prompted(self, session_id: str, reason: str) -> None:
        self._write("UPDATE sessions SET lead_prompted=1, lead_reason=? WHERE session_id=?", (reason, session_id))

    def session_topics(self, session_id: str, limit: int = 5) -> str:
        """The website pages the conversation touched, e.g. "Standing desks, Delivery".

        This is what tells a salesperson what the visitor cares about before reading the transcript.
        """
        rows = self._read("SELECT meta FROM messages WHERE session_id=? AND role='assistant' ORDER BY id", (session_id,))
        titles: list[str] = []
        for row in rows:
            for source in json.loads(row["meta"]).get("sources", []):
                title = source["label"].split(" \u203a ")[0]
                if title not in titles:
                    titles.append(title)
        return ", ".join(titles[:limit])

    def add_message(self, session_id: str, role: str, content: str, meta: dict | None = None) -> None:
        now = _now()
        self._write("INSERT INTO messages(session_id, role, content, created_at, day, meta) VALUES (?,?,?,?,?,?)",
                    (session_id, role, content, now, now[:10], json.dumps(meta or {})))

    def history(self, session_id: str, limit: int = 12) -> list[dict]:
        """The last `limit` messages, oldest first."""
        rows = self._read("SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                          (session_id, limit))
        return rows[::-1]

    def user_messages_today(self) -> int:
        return self._read("SELECT COUNT(*) AS n FROM messages WHERE day=? AND role='user'", (_now()[:10],))[0]["n"]

    # -- leads --
    def add_lead(self, session_id: str, email: str, **fields: object) -> int:
        transcript = json.dumps(self.history(session_id, 40), ensure_ascii=False)
        lead_id = self._write(
            "INSERT INTO leads(session_id, email, name, phone, company, message, reason, interest, source, consent,"
            " page_url, transcript, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, email, fields.get("name", ""), fields.get("phone", ""), fields.get("company", ""),
             fields.get("message", ""), fields.get("reason", ""), fields.get("interest", ""),
             fields.get("source", "form"), int(bool(fields.get("consent"))), fields.get("page_url", ""),
             transcript, _now()))
        self._write("UPDATE sessions SET lead_id=? WHERE session_id=?", (lead_id, session_id))
        return lead_id

    def lead(self, lead_id: int) -> dict | None:
        rows = self._read("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not rows:
            return None
        rows[0]["transcript"] = json.loads(rows[0]["transcript"])
        return rows[0]

    def leads(self) -> list[dict]:
        rows = self._read("SELECT * FROM leads ORDER BY id DESC")
        for r in rows:
            r["transcript"] = json.loads(r["transcript"])
        return rows

    def set_webhook_status(self, lead_id: int, status: str, attempts: int) -> None:
        self._write("UPDATE leads SET webhook_status=?, webhook_attempts=? WHERE id=?", (status, attempts, lead_id))

    # -- content gaps --
    def add_unanswered(self, session_id: str, question: str, best_score: float | None) -> None:
        self._write("INSERT INTO unanswered(session_id, question, best_score, created_at) VALUES (?,?,?,?)",
                    (session_id, question, best_score, _now()))

    def unanswered(self, limit: int = 200) -> list[dict]:
        return self._read("SELECT * FROM unanswered ORDER BY id DESC LIMIT ?", (limit,))

    def stats(self) -> dict:
        one = lambda sql: self._read(sql)[0]["n"]  # noqa: E731
        return {"conversations": one("SELECT COUNT(DISTINCT session_id) AS n FROM messages"),
                "visitor_messages": one("SELECT COUNT(*) AS n FROM messages WHERE role='user'"),
                "leads": one("SELECT COUNT(*) AS n FROM leads"),
                "unanswered": one("SELECT COUNT(*) AS n FROM unanswered")}
