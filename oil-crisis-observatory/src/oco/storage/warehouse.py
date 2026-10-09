"""DuckDB warehouse: immutable raw evidence manifest, versioned observations, derived findings.

Exactly one writer at a time (fcntl file lock). The dashboard opens a published read-only
snapshot (`snapshot.duckdb`) that is replaced atomically after each ingestion run.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from ..settings import Paths

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS raw_files (
        sha256 VARCHAR PRIMARY KEY, source VARCHAR, url VARCHAR, final_url VARCHAR,
        retrieved_at TIMESTAMPTZ, http_status INTEGER, content_type VARCHAR, size BIGINT,
        rel_path VARCHAR, etag VARCHAR, last_modified VARCHAR, source_published_at TIMESTAMPTZ, note VARCHAR,
        demo BOOLEAN DEFAULT FALSE)""",
    """CREATE TABLE IF NOT EXISTS source_runs (
        run_id VARCHAR PRIMARY KEY, source VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
        status VARCHAR, n_requests INTEGER, raw_sha256s VARCHAR, n_new INTEGER, n_revised INTEGER,
        n_unchanged INTEGER, latest_observation DATE, error VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS series (
        series_id VARCHAR PRIMARY KEY, source VARCHAR, source_key VARCHAR, name VARCHAR, geography VARCHAR,
        product VARCHAR, unit VARCHAR, frequency VARCHAR, description VARCHAR, metadata VARCHAR,
        created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS observation_versions (
        version_id VARCHAR PRIMARY KEY, series_id VARCHAR, obs_start DATE, obs_end DATE, value DOUBLE,
        status VARCHAR, source_published_at TIMESTAMPTZ, first_seen_at TIMESTAMPTZ, retrieved_at TIMESTAMPTZ,
        raw_sha256 VARCHAR, revision_no INTEGER, is_current BOOLEAN, superseded_at TIMESTAMPTZ, attrs VARCHAR)""",
    "CREATE INDEX IF NOT EXISTS idx_obs_series ON observation_versions(series_id, obs_start)",
    """CREATE TABLE IF NOT EXISTS headlines (
        headline_id VARCHAR PRIMARY KEY, canonical_url VARCHAR, url VARCHAR, title VARCHAR, publisher VARCHAR,
        language VARCHAR, published_at TIMESTAMPTZ, published_at_source VARCHAR, discovered_at TIMESTAMPTZ,
        discovery_source VARCHAR, excerpt VARCHAR, content_hash VARCHAR, cluster_id VARCHAR, raw_sha256 VARCHAR,
        origin VARCHAR, updated_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS headline_clusters (
        cluster_id VARCHAR PRIMARY KEY, representative_id VARCHAR, title VARCHAR, first_seen_at TIMESTAMPTZ,
        n_members INTEGER)""",
    """CREATE TABLE IF NOT EXISTS anomalies (
        anomaly_id VARCHAR PRIMARY KEY, rule_id VARCHAR, series_id VARCHAR, obs_start DATE, obs_end DATE,
        value DOUBLE, fired BOOLEAN, status VARCHAR, severity_rank DOUBLE, formula_version VARCHAR,
        baseline VARCHAR, stats VARCHAR, thresholds VARCHAR, input_version_ids VARCHAR, explanation VARCHAR,
        created_at TIMESTAMPTZ, superseded_at TIMESTAMPTZ, superseded_by VARCHAR, as_of TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS evidence_cards (
        card_id VARCHAR, version INTEGER, kind VARCHAR, subject_id VARCHAR, created_at TIMESTAMPTZ,
        status VARCHAR, relationship VARCHAR, question VARCHAR, payload VARCHAR, narrative VARCHAR,
        narrative_source VARCHAR, review_flag VARCHAR, input_version_ids VARCHAR, topics VARCHAR,
        geography VARCHAR, signal_strength VARCHAR, content_hash VARCHAR, PRIMARY KEY(card_id, version))""",
    """CREATE TABLE IF NOT EXISTS satellite_scenes (
        product_id VARCHAR PRIMARY KEY, product_name VARCHAR, mission VARCHAR, product_type VARCHAR, collection VARCHAR,
        acquisition_start TIMESTAMPTZ, acquisition_end TIMESTAMPTZ, published_at TIMESTAMPTZ, modified_at TIMESTAMPTZ,
        first_seen_at TIMESTAMPTZ, footprint_wkt VARCHAR, orbit_direction VARCHAR, relative_orbit INTEGER,
        polarisation VARCHAR, instrument_mode VARCHAR, cloud_cover DOUBLE, tile_id VARCHAR, platform VARCHAR,
        online BOOLEAN, size_bytes BIGINT, attrs VARCHAR, download_status VARCHAR, local_path VARCHAR, local_sha256 VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS scene_aoi (
        product_id VARCHAR, aoi_id VARCHAR, intersection_fraction DOUBLE, PRIMARY KEY(product_id, aoi_id))""",
    """CREATE TABLE IF NOT EXISTS candidate_detections (
        detection_id VARCHAR PRIMARY KEY, product_id VARCHAR, aoi_id VARCHAR, lon DOUBLE, lat DOUBLE,
        area_px INTEGER, peak_db DOUBLE, contrast_db DOUBLE, quality VARCHAR, review_status VARCHAR,
        method_version VARCHAR, created_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS snapshot_counts (
        product_id VARCHAR, aoi_id VARCHAR, acquisition_start TIMESTAMPTZ, candidate_count INTEGER,
        valid_water_fraction DOUBLE, comparable_group VARCHAR, method_version VARCHAR, unit VARCHAR,
        quality VARCHAR, created_at TIMESTAMPTZ, PRIMARY KEY(product_id, aoi_id, method_version))""",
    """CREATE TABLE IF NOT EXISTS thermal_detections (
        det_id VARCHAR PRIMARY KEY, source VARCHAR, satellite VARCHAR, instrument VARCHAR, acq_datetime TIMESTAMPTZ,
        lat DOUBLE, lon DOUBLE, confidence VARCHAR, daynight VARCHAR, frp DOUBLE, scan DOUBLE, track DOUBLE,
        bright_ti4 DOUBLE, bright_ti5 DOUBLE, version VARCHAR, raw_sha256 VARCHAR, first_seen_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS thermal_events (
        event_id VARCHAR PRIMARY KEY, facility_id VARCHAR, start_at TIMESTAMPTZ, end_at TIMESTAMPTZ,
        n_detections INTEGER, n_overpasses INTEGER, sensors VARCHAR, daynight VARCHAR, max_frp DOUBLE,
        routine_flare_share DOUBLE, classification VARCHAR, baseline VARCHAR, detection_ids VARCHAR,
        review_status VARCHAR, created_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS assessment_products (
        product_id VARCHAR PRIMARY KEY, source VARCHAR, activation VARCHAR, title VARCHAR, product_date DATE,
        licence VARCHAR, source_url VARCHAR, local_path VARCHAR, sha256 VARCHAR, imported_at TIMESTAMPTZ, notes VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS comparisons (
        comparison_id VARCHAR PRIMARY KEY, kind VARCHAR, facility_or_aoi VARCHAR, event_date DATE,
        before_ids VARCHAR, after_ids VARCHAR, valid BOOLEAN, reasons VARCHAR, outputs VARCHAR, created_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR)""",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha1(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:20]


def _same(a, b) -> bool:
    a_nan = a is None or (isinstance(a, float) and math.isnan(a))
    b_nan = b is None or (isinstance(b, float) and math.isnan(b))
    if a_nan or b_nan:
        return a_nan and b_nan
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


class WriterBusy(Exception):
    pass


class Warehouse:
    """Writer handle. Use `with Warehouse.writer(paths) as wh:`."""

    def __init__(self, con: duckdb.DuckDBPyConnection, paths: Paths | None):
        self.con = con
        self.paths = paths

    # ---- lifecycle ------------------------------------------------------------------------
    @classmethod
    @contextmanager
    def writer(cls, paths: Paths, publish: bool = True, wait: bool = True):
        paths.state.mkdir(parents=True, exist_ok=True)
        lock_fh = open(paths.writer_lock, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            lock_fh.close()
            raise WriterBusy("another ingestion process holds the warehouse writer lock")
        con = duckdb.connect(str(paths.warehouse))
        wh = cls(con, paths)
        wh.init_schema()
        if paths.demo:
            wh.set_meta("mode", "DEMO_SYNTHETIC")
        try:
            yield wh
        finally:
            con.execute("CHECKPOINT")
            con.close()
            if publish:
                publish_snapshot(paths)
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

    @classmethod
    def memory(cls) -> "Warehouse":
        con = duckdb.connect(":memory:")
        wh = cls(con, None)
        wh.init_schema()
        return wh

    def init_schema(self):
        for stmt in SCHEMA:
            self.con.execute(stmt)

    def set_meta(self, key: str, value: str):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    def get_meta(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", [key]).fetchone()
        return r[0] if r else None

    # ---- raw evidence ---------------------------------------------------------------------
    def store_raw(self, source: str, content: bytes | None, *, url: str, final_url: str | None = None,
                  retrieved_at: datetime | None = None, http_status: int = 200, content_type: str = "",
                  ext: str = "bin", etag: str | None = None, last_modified: str | None = None,
                  source_published_at: datetime | None = None, note: str | None = None,
                  existing_path: Path | None = None, sha256: str | None = None) -> str:
        """Write bytes once, content-addressed, read-only. Returns sha256. URLs must already be redacted."""
        retrieved_at = retrieved_at or now_utc()
        if existing_path is not None:
            digest = sha256 or _file_sha256(existing_path)
            size = existing_path.stat().st_size
        else:
            digest = hashlib.sha256(content or b"").hexdigest()
            size = len(content or b"")
        rel = Path(source) / f"{retrieved_at:%Y}" / f"{retrieved_at:%m}" / f"{digest}.{ext}"
        if self.paths is not None:
            dest = self.paths.raw / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                existing = self.con.execute("SELECT rel_path FROM raw_files WHERE sha256=?", [digest]).fetchone()
                if existing and (self.paths.raw / existing[0]).exists():
                    rel = Path(existing[0])
                elif existing_path is not None:
                    shutil.move(str(existing_path), dest)
                    os.chmod(dest, 0o444)
                else:
                    tmp = dest.with_suffix(dest.suffix + ".tmp")
                    tmp.write_bytes(content or b"")
                    os.replace(tmp, dest)
                    os.chmod(dest, 0o444)
        self.con.execute(
            "INSERT INTO raw_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [digest, source, url, final_url or url, retrieved_at, http_status, content_type, size, str(rel),
             etag, last_modified, source_published_at, note, bool(self.paths and self.paths.demo)],
        )
        return digest

    # ---- series & observations ------------------------------------------------------------
    def upsert_series(self, series_id: str, *, source: str, source_key: str, name: str, geography: str,
                      product: str, unit: str, frequency: str, description: str = "", metadata: dict | None = None):
        ts = now_utc()
        self.con.execute(
            """INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (series_id) DO UPDATE SET name=excluded.name, unit=excluded.unit,
               description=excluded.description, metadata=excluded.metadata, updated_at=excluded.updated_at""",
            [series_id, source, source_key, name, geography, product, unit, frequency, description,
             json.dumps(metadata or {}, default=str), ts, ts],
        )

    def ingest_observations(self, series_id: str, rows: list[dict], *, raw_sha256: str | None,
                            retrieved_at: datetime | None = None) -> dict:
        """Versioned upsert.

        rows: dicts with obs_start (date), obs_end (date), value (float|None), status (str),
              optional source_published_at (datetime|None) and attrs (dict).
        A None value means MISSING (never zero). Identical re-ingestion is a no-op.
        A changed value/status creates a new revision and marks the previous one superseded.
        """
        retrieved_at = retrieved_at or now_utc()
        if not rows:
            return {"new": 0, "revised": 0, "unchanged": 0, "revised_keys": []}
        cur = self.con.execute(
            "SELECT obs_start, value, status, revision_no, version_id FROM observation_versions "
            "WHERE series_id=? AND is_current", [series_id]
        ).fetchall()
        current = {r[0]: r for r in cur}
        new = revised = unchanged = 0
        revised_keys = []
        inserts = []
        seen = set()
        for row in rows:
            start = _as_date(row["obs_start"])
            if start in seen:
                raise ValueError(f"duplicate observation {series_id} {start} in one batch")
            seen.add(start)
            end = _as_date(row.get("obs_end") or start)
            val = row.get("value")
            if val is not None and (isinstance(val, float) and math.isnan(val)):
                val = None
            status = row.get("status") or ("ok" if val is not None else "missing")
            if val is None and status == "ok":
                status = "missing"
            prev = current.get(start)
            if prev is not None:
                if _same(prev[1], val) and prev[2] == status:
                    unchanged += 1
                    continue
                rev = prev[3] + 1
                self.con.execute(
                    "UPDATE observation_versions SET is_current=FALSE, superseded_at=? WHERE version_id=?",
                    [retrieved_at, prev[4]],
                )
                revised += 1
                revised_keys.append(start)
            else:
                rev = 1
                new += 1
            vid = sha1(series_id, start, rev, val, status)
            inserts.append([vid, series_id, start, end, val, status, row.get("source_published_at"), retrieved_at,
                            retrieved_at, raw_sha256, rev, True, None, json.dumps(row.get("attrs") or {}, default=str)])
        if inserts:
            self.con.executemany("INSERT INTO observation_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
        return {"new": new, "revised": revised, "unchanged": unchanged, "revised_keys": revised_keys}

    def record_run(self, run_id: str, source: str, started_at: datetime, status: str, n_requests: int,
                   raw: list[str], counts: dict, latest: date | None, error: str | None = None):
        self.con.execute(
            "INSERT OR REPLACE INTO source_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, source, started_at, now_utc(), status, n_requests, json.dumps(raw), counts.get("new", 0),
             counts.get("revised", 0), counts.get("unchanged", 0), latest, error],
        )


# ---- read helpers (usable on writer or read-only snapshot connections) -------------------
def observations(con, series_id: str, as_of: datetime | None = None, current_only: bool = True) -> pd.DataFrame:
    """Observation table for one series. With as_of: the vintage that was known at that instant."""
    if as_of is not None:
        q = ("SELECT * FROM observation_versions WHERE series_id=? AND first_seen_at<=? "
             "AND (superseded_at IS NULL OR superseded_at>?) ORDER BY obs_start")
        df = con.execute(q, [series_id, as_of, as_of]).df()
    elif current_only:
        df = con.execute("SELECT * FROM observation_versions WHERE series_id=? AND is_current ORDER BY obs_start",
                         [series_id]).df()
    else:
        df = con.execute("SELECT * FROM observation_versions WHERE series_id=? ORDER BY obs_start, revision_no",
                         [series_id]).df()
    if not df.empty:
        df["obs_start"] = pd.to_datetime(df["obs_start"])
        df["obs_end"] = pd.to_datetime(df["obs_end"])
    return df


def series_info(con, series_id: str) -> dict:
    r = con.execute("SELECT * FROM series WHERE series_id=?", [series_id]).df()
    return r.iloc[0].to_dict() if not r.empty else {}


def publish_snapshot(paths: Paths):
    """Atomically replace the dashboard's read-only snapshot with the current warehouse."""
    if not paths.warehouse.exists():
        return
    tmp = paths.snapshot.with_suffix(".tmp")
    shutil.copy2(paths.warehouse, tmp)
    os.replace(tmp, paths.snapshot)


def open_snapshot(paths: Paths) -> duckdb.DuckDBPyConnection | None:
    if not paths.snapshot.exists():
        return None
    return duckdb.connect(str(paths.snapshot), read_only=True)


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date()
    return date.fromisoformat(str(v)[:10])


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
