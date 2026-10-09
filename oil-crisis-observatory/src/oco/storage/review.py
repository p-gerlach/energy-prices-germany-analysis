"""User review decisions and UI state (written by the dashboard; never touches the DuckDB warehouse)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .state import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS card_review (
    card_id TEXT PRIMARY KEY, decision TEXT, note TEXT, pinned INTEGER DEFAULT 0, reviewed_version INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS item_review (
    kind TEXT, item_id TEXT, decision TEXT, note TEXT, updated_at TEXT, PRIMARY KEY(kind, item_id));
CREATE TABLE IF NOT EXISTS ui_state (key TEXT PRIMARY KEY, value TEXT);
"""


class ReviewStore:
    def __init__(self, path: Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def get(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set(self, key: str, value: str):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO ui_state VALUES (?,?)", (key, value))

    def card(self, card_id: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT decision, note, pinned, reviewed_version, updated_at FROM card_review WHERE card_id=?", (card_id,)).fetchone()
        return dict(zip(["decision", "note", "pinned", "reviewed_version", "updated_at"], r)) if r else {}

    def all_cards(self) -> dict[str, dict]:
        with self.conn() as c:
            rows = c.execute("SELECT card_id, decision, note, pinned, reviewed_version FROM card_review").fetchall()
        return {r[0]: {"decision": r[1], "note": r[2], "pinned": bool(r[3]), "reviewed_version": r[4]} for r in rows}

    def set_card(self, card_id: str, version: int, decision: str | None = None, note: str | None = None, pinned: bool | None = None):
        cur = self.card(card_id)
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO card_review VALUES (?,?,?,?,?,?)",
                      (card_id, decision if decision is not None else cur.get("decision"),
                       note if note is not None else cur.get("note"),
                       int(pinned if pinned is not None else bool(cur.get("pinned"))), version, iso(utcnow())))

    def set_item(self, kind: str, item_id: str, decision: str, note: str = ""):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO item_review VALUES (?,?,?,?,?)", (kind, item_id, decision, note, iso(utcnow())))

    def items(self, kind: str) -> dict[str, dict]:
        with self.conn() as c:
            rows = c.execute("SELECT item_id, decision, note FROM item_review WHERE kind=?", (kind,)).fetchall()
        return {r[0]: {"decision": r[1], "note": r[2]} for r in rows}
