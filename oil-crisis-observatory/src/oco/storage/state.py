"""Durable lightweight state in SQLite: connector status, source health, HTTP validators,
download ledger, job queue, feed admission and connector probe results.

SQLite (not DuckDB) is used here because the dashboard, the scheduler and the CLI may all touch it;
the DuckDB warehouse has exactly one writer.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_state (
    connector TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',          -- active | paused | stopped
    reason TEXT,
    since TEXT,
    paused_until TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_open_until TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    latest_observation_at TEXT,
    source_published_at TEXT,
    next_expected_release TEXT,
    next_due_at TEXT,
    last_error TEXT,
    last_result TEXT
);
CREATE TABLE IF NOT EXISTS probe_results (
    connector TEXT NOT NULL,
    kind TEXT NOT NULL,                              -- anonymous_read | authenticated_download | credential_check
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT,
    PRIMARY KEY (connector, kind)
);
CREATE TABLE IF NOT EXISTS http_validators (
    url_key TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    sha256 TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS download_ledger (
    day TEXT NOT NULL,
    connector TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (day, connector)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',          -- queued | running | done | failed | blocked
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_try_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS feed_admission (
    feed_url TEXT PRIMARY KEY,
    admitted INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    connector TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StateStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        if self._mem is not None:
            yield self._mem
            self._mem.commit()
            return
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- connector status -------------------------------------------------------------
    def connector_status(self, connector: str) -> dict:
        with self.conn() as c:
            row = c.execute(
                "SELECT status, reason, since, paused_until, consecutive_failures, circuit_open_until "
                "FROM connector_state WHERE connector=?",
                (connector,),
            ).fetchone()
        if not row:
            return {"status": "active", "reason": None, "since": None, "paused_until": None,
                    "consecutive_failures": 0, "circuit_open_until": None}
        keys = ["status", "reason", "since", "paused_until", "consecutive_failures", "circuit_open_until"]
        return dict(zip(keys, row))

    def _upsert(self, connector: str, **fields):
        cur = self.connector_status(connector)
        cur.update(fields)
        with self.conn() as c:
            c.execute(
                "INSERT INTO connector_state(connector,status,reason,since,paused_until,consecutive_failures,circuit_open_until) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(connector) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
                "since=excluded.since, paused_until=excluded.paused_until, consecutive_failures=excluded.consecutive_failures, "
                "circuit_open_until=excluded.circuit_open_until",
                (connector, cur["status"], cur["reason"], cur["since"], cur["paused_until"],
                 cur["consecutive_failures"], cur["circuit_open_until"]),
            )

    def stop_connector(self, connector: str, reason: str):
        """Fail closed. Only a human (`oco connectors reset`) re-enables a stopped connector."""
        self._upsert(connector, status="stopped", reason=reason, since=iso(utcnow()))
        self.log(connector, "ERROR", f"connector STOPPED: {reason}")

    def pause_connector(self, connector: str, until: datetime, reason: str):
        self._upsert(connector, status="paused", reason=reason, since=iso(utcnow()), paused_until=iso(until))
        self.log(connector, "WARN", f"connector paused until {iso(until)}: {reason}")

    def reset_connector(self, connector: str):
        self._upsert(connector, status="active", reason="manually reset", since=iso(utcnow()),
                     paused_until=None, consecutive_failures=0, circuit_open_until=None)

    def record_failure(self, connector: str, threshold: int = 5, cooldown_min: int = 30) -> bool:
        st = self.connector_status(connector)
        n = int(st["consecutive_failures"] or 0) + 1
        circuit = st["circuit_open_until"]
        opened = False
        if n >= threshold:
            circuit = iso(utcnow() + timedelta(minutes=cooldown_min))
            opened = True
        self._upsert(connector, consecutive_failures=n, circuit_open_until=circuit)
        return opened

    def record_success(self, connector: str):
        st = self.connector_status(connector)
        status = "active" if st["status"] == "paused" else st["status"]
        self._upsert(connector, status=status, consecutive_failures=0, circuit_open_until=None,
                     paused_until=None if status == "active" else st["paused_until"])

    # ---- health ------------------------------------------------------------------------
    def update_health(self, source: str, **fields):
        allowed = {"last_attempt_at", "last_success_at", "latest_observation_at", "source_published_at",
                   "next_expected_release", "next_due_at", "last_error", "last_result"}
        bad = set(fields) - allowed
        if bad:
            raise KeyError(bad)
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO source_health(source) VALUES(?)", (source,))
            for k, v in fields.items():
                if isinstance(v, datetime):
                    v = iso(v)
                elif isinstance(v, (dict, list)):
                    v = json.dumps(v, default=str)
                c.execute(f"UPDATE source_health SET {k}=? WHERE source=?", (v, source))

    def health(self) -> list[dict]:
        with self.conn() as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute("SELECT * FROM source_health ORDER BY source")]
            c.row_factory = None
        return rows

    def health_for(self, source: str) -> dict:
        for r in self.health():
            if r["source"] == source:
                return r
        return {"source": source}

    # ---- probes ------------------------------------------------------------------------
    def record_probe(self, connector: str, kind: str, ok: bool, detail: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO probe_results(connector,kind,checked_at,ok,detail) VALUES(?,?,?,?,?) "
                "ON CONFLICT(connector,kind) DO UPDATE SET checked_at=excluded.checked_at, ok=excluded.ok, detail=excluded.detail",
                (connector, kind, iso(utcnow()), int(ok), detail[:2000]),
            )

    def probes(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT connector, kind, checked_at, ok, detail FROM probe_results").fetchall()
        return [dict(zip(["connector", "kind", "checked_at", "ok", "detail"], r)) for r in rows]

    # ---- validators --------------------------------------------------------------------
    def get_validators(self, url_key: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT etag, last_modified, sha256 FROM http_validators WHERE url_key=?", (url_key,)).fetchone()
        return dict(zip(["etag", "last_modified", "sha256"], row)) if row else {}

    def set_validators(self, url_key: str, etag: str | None, last_modified: str | None, sha256: str | None):
        with self.conn() as c:
            c.execute(
                "INSERT INTO http_validators(url_key,etag,last_modified,sha256,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(url_key) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
                "sha256=excluded.sha256, updated_at=excluded.updated_at",
                (url_key, etag, last_modified, sha256, iso(utcnow())),
            )

    # ---- download budget ---------------------------------------------------------------
    def add_download_bytes(self, connector: str, n: int):
        day = utcnow().date().isoformat()
        with self.conn() as c:
            c.execute(
                "INSERT INTO download_ledger(day,connector,bytes) VALUES(?,?,?) "
                "ON CONFLICT(day,connector) DO UPDATE SET bytes=bytes+excluded.bytes",
                (day, connector, n),
            )

    def downloaded_bytes(self, connector: str, days: int = 30) -> int:
        since = (utcnow() - timedelta(days=days)).date().isoformat()
        with self.conn() as c:
            row = c.execute("SELECT COALESCE(SUM(bytes),0) FROM download_ledger WHERE connector=? AND day>=?",
                            (connector, since)).fetchone()
        return int(row[0])

    # ---- jobs --------------------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict, dedupe_key: str | None = None, max_attempts: int = 5) -> int | None:
        now = iso(utcnow())
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs(kind,dedupe_key,payload,status,attempts,max_attempts,next_try_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, dedupe_key, json.dumps(payload), "queued", 0, max_attempts, now, now, now),
            )
            return cur.lastrowid if cur.rowcount else None

    def due_jobs(self, kind: str, limit: int = 5) -> list[dict]:
        now = iso(utcnow())
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, payload, attempts, max_attempts FROM jobs WHERE kind=? AND status='queued' AND next_try_at<=? "
                "ORDER BY id LIMIT ?",
                (kind, now, limit),
            ).fetchall()
        return [{"id": r[0], "payload": json.loads(r[1]), "attempts": r[2], "max_attempts": r[3]} for r in rows]

    def finish_job(self, job_id: int, ok: bool, error: str | None = None, retry_in_min: int = 30, blocked: bool = False):
        with self.conn() as c:
            row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempts = row[0] + 1
            if ok:
                status = "done"
            elif blocked:
                status = "blocked"
            elif attempts >= row[1]:
                status = "failed"
            else:
                status = "queued"
            c.execute(
                "UPDATE jobs SET status=?, attempts=?, next_try_at=?, updated_at=?, last_error=? WHERE id=?",
                (status, attempts, iso(utcnow() + timedelta(minutes=retry_in_min * attempts)), iso(utcnow()), error, job_id),
            )

    def jobs(self, kind: str | None = None) -> list[dict]:
        q = "SELECT id, kind, dedupe_key, payload, status, attempts, last_error, updated_at FROM jobs"
        args: tuple = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        keys = ["id", "kind", "dedupe_key", "payload", "status", "attempts", "last_error", "updated_at"]
        return [dict(zip(keys, r)) for r in rows]

    # ---- feeds -------------------------------------------------------------------------
    def set_feed_admission(self, url: str, admitted: bool, detail: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO feed_admission(feed_url,admitted,checked_at,detail) VALUES(?,?,?,?) "
                "ON CONFLICT(feed_url) DO UPDATE SET admitted=excluded.admitted, checked_at=excluded.checked_at, detail=excluded.detail",
                (url, int(admitted), iso(utcnow()), detail[:1000]),
            )

    def admitted_feeds(self) -> dict[str, dict]:
        with self.conn() as c:
            rows = c.execute("SELECT feed_url, admitted, checked_at, detail FROM feed_admission").fetchall()
        return {r[0]: {"admitted": bool(r[1]), "checked_at": r[2], "detail": r[3]} for r in rows}

    # ---- log ---------------------------------------------------------------------------
    def log(self, connector: str | None, level: str, message: str):
        with self.conn() as c:
            c.execute("INSERT INTO events_log(at,connector,level,message) VALUES(?,?,?,?)",
                      (iso(utcnow()), connector, level, message[:2000]))

    def recent_log(self, limit: int = 200) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT at, connector, level, message FROM events_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(["at", "connector", "level", "message"], r)) for r in rows]
