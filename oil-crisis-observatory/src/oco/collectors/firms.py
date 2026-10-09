"""NASA FIRMS Area CSV API (VIIRS NRT) with a free user-supplied MAP_KEY.

* only for manually VERIFIED facilities (config/facilities.geojson); never for invented coordinates
* day_range 1..5 per request (documented API limit); dated windows for short backfills
* NRT endpoint is NOT the full archive — longer history needs the separately verified archive route
* keeps sensor, acquisition time, confidence, day/night, FRP, scan/track footprint
"""
from __future__ import annotations

import hashlib
import io
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from .. import geo
from ..http import ContentValidationError, RateLimited
from ..storage.state import utcnow
from ..storage.warehouse import Warehouse
from .base import Collector, NotConfigured, RunResult, SchemaChanged

BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
REQUIRED = {"latitude", "longitude", "acq_date", "acq_time", "confidence", "frp", "daynight", "scan", "track"}


def parse_firms_csv(text: str) -> pd.DataFrame:
    t = text.strip()
    low = t[:300].lower()
    if "invalid map_key" in low or "invalid api" in low:
        raise NotConfigured("FIRMS rejected the MAP_KEY (invalid). Check FIRMS_MAP_KEY in .env")
    if "exceed" in low and "transaction" in low:
        raise RateLimited("FIRMS transaction limit reached — waiting (no upgrade exists or is used)")
    if not t:
        return pd.DataFrame(columns=sorted(REQUIRED))
    if t.startswith("<"):
        raise ContentValidationError("FIRMS returned HTML instead of CSV")
    df = pd.read_csv(io.StringIO(t))
    if df.empty:
        return df
    missing = REQUIRED - set(df.columns)
    if missing:
        raise SchemaChanged(f"FIRMS CSV lacks columns {missing}")
    return df


def detection_id(source: str, r) -> str:
    return hashlib.sha1(f"{source}|{r['latitude']:.5f}|{r['longitude']:.5f}|{r['acq_date']}|{int(r['acq_time']):04d}|{r.get('satellite','')}".encode()).hexdigest()[:20]


def acq_dt(r) -> datetime:
    t = int(r["acq_time"])
    d = date.fromisoformat(str(r["acq_date"]))
    return datetime(d.year, d.month, d.day, t // 100, t % 100, tzinfo=timezone.utc)


class FIRMSCollector(Collector):
    key = "firms"
    connector = "firms"
    credential_envs = ("FIRMS_MAP_KEY",)

    def areas(self) -> list[tuple[str, tuple]]:
        fac = geo.facilities(verified_only=True)
        if not fac:
            raise NotConfigured("no VERIFIED facility with a boundary in config/facilities.geojson — add one (with public source links) before thermal monitoring")
        km = float(self.cfg.get("facility_buffer_km", 5))
        return [(f["properties"]["id"], geo.buffer_bbox(geo.bbox(f["geometry"]), km)) for f in fac]

    def url(self, product: str, bb, days: int, day: date | None = None) -> str:
        area = ",".join(f"{v:.4f}" for v in bb)
        u = f"{BASE}/{self.secrets()[0]}/{product}/{area}/{days}"
        return u + (f"/{day.isoformat()}" if day else "")

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        areas = self.areas()
        days = int(self.cfg.get("day_range", 2))
        windows: list[date | None] = [None]
        if mode == "backfill":
            n = int(kw.get("days") or 30)
            end = utcnow().date()
            windows = [end - timedelta(days=i) for i in range(5, n + 5, 5)]
            days = 5
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            for fid, bb in areas:
                for product in self.cfg["products"]:
                    for w in windows:
                        r = client.get(self.url(product, bb, days, w))
                        result.n_requests += 1
                        try:
                            df = parse_firms_csv(r.text())
                        except RateLimited:
                            self.ctx.state.pause_connector(self.connector, utcnow() + timedelta(minutes=15),
                                                           "FIRMS transaction limit reached; waiting")
                            raise
                        sha = wh.store_raw("firms", r.content, url=r.url, retrieved_at=r.retrieved_at,
                                           content_type=r.content_type, ext="csv", note=f"{fid} {product}")
                        result.raw.append(sha)
                        for _, row in df.iterrows():
                            did = detection_id(product, row)
                            exists = wh.con.execute("SELECT 1 FROM thermal_detections WHERE det_id=?", [did]).fetchone()
                            if exists:
                                result.counts["unchanged"] += 1
                                continue
                            wh.con.execute(
                                "INSERT INTO thermal_detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                [did, product, row.get("satellite"), row.get("instrument", "VIIRS"), acq_dt(row),
                                 float(row["latitude"]), float(row["longitude"]), str(row["confidence"]),
                                 row["daynight"], float(row["frp"]), float(row["scan"]), float(row["track"]),
                                 _f(row.get("bright_ti4")), _f(row.get("bright_ti5")), str(row.get("version", "")),
                                 sha, utcnow()])
                            result.counts["new"] += 1
                            result.saw(acq_dt(row).date())

    def probe(self):
        self.require_credentials()
        # Data-availability endpoint proves the key works without needing a facility.
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            r = client.get(f"https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{self.secrets()[0]}/VIIRS_SNPP_NRT")
        text = r.text().strip()
        if "invalid" in text[:200].lower():
            return False, "MAP_KEY rejected"
        return True, "data availability: " + text.replace("\n", " | ")[:200]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
