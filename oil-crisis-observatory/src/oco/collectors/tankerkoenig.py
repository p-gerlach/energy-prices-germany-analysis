"""Tankerkönig: official German pump prices (Markttransparenzstelle für Kraftstoffe), CC BY 4.0, free key.

* `oco tankerkoenig build-panel` (once): list.php around each configured city centre -> a FIXED panel of stations
  (saved to data/state/tk_panel.json). A fixed panel keeps averages comparable over time.
* every run: prices.php for the panel in batches of 10 (the API limit) -> raw station prices in `pump_prices`.
* completed Berlin calendar days are aggregated into observation series (mean / median of station-time prices):
    tk.panel.{diesel,e5,e10}.mean, tk.panel.{fuel}.median, tk.{city}.{fuel}.mean
  Today is never written as an observation (it is still changing); the dashboard shows it from the raw table.
* This is a SAMPLE PANEL of ~100 urban stations, not the Bundeskartellamt's national average.
* The public demo key returns fake prices; data fetched with it are refused.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..storage.state import utcnow
from ..storage.warehouse import Warehouse
from .base import Collector, NotConfigured, RunResult, SchemaChanged

API = "https://creativecommons.tankerkoenig.de/json/"
DEMO_KEY = "00000000-0000-0000-0000-000000000002"
FUELS = ("diesel", "e5", "e10")
BERLIN = ZoneInfo("Europe/Berlin")

PUMP_SCHEMA = """CREATE TABLE IF NOT EXISTS pump_prices (
    station_id VARCHAR, city VARCHAR, brand VARCHAR, fetched_at TIMESTAMPTZ, status VARCHAR,
    diesel DOUBLE, e5 DOUBLE, e10 DOUBLE, raw_sha256 VARCHAR, PRIMARY KEY(station_id, fetched_at))"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")).strip("_")


class TankerkoenigCollector(Collector):
    key = "tankerkoenig"
    connector = "tankerkoenig"
    credential_envs = ("TANKERKOENIG_API_KEY",)

    @property
    def panel_path(self):
        return self.ctx.paths.state / "tk_panel.json"

    def require_credentials(self):
        super().require_credentials()
        if self.secrets()[0] == DEMO_KEY:
            raise NotConfigured("the public Tankerkönig DEMO key returns fake prices; request your own free key at https://creativecommons.tankerkoenig.de/")

    def _check(self, payload: dict):
        if not payload.get("ok"):
            msg = str(payload.get("message", ""))[:160]
            if "apikey" in msg.lower() or "api-key" in msg.lower():
                raise NotConfigured(f"Tankerkönig rejected the API key: {msg}")
            raise SchemaChanged(f"Tankerkönig error: {msg}")
        lic = str(payload.get("license", ""))
        if lic and "CC BY" not in lic.upper():
            raise SchemaChanged(f"Tankerkönig licence changed to {lic!r}; review before storing data")

    def build_panel(self) -> dict:
        self.require_credentials()
        panel = {"built_at": str(utcnow()), "stations": []}
        seen = set()
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            for c in self.cfg["cities"]:
                r = client.get(API + "list.php", params={"lat": c["lat"], "lng": c["lng"], "rad": self.cfg.get("radius_km", 5),
                                                         "sort": "dist", "type": "all", "apikey": self.secrets()[0]})
                payload = r.json()
                self._check(payload)
                n = 0
                for s in payload.get("stations", []):
                    if s["id"] in seen:
                        continue
                    seen.add(s["id"])
                    panel["stations"].append({"id": s["id"], "city": c["name"], "brand": s.get("brand"), "name": s.get("name"),
                                              "lat": s.get("lat"), "lng": s.get("lng"), "postCode": s.get("postCode")})
                    n += 1
                    if n >= int(self.cfg.get("per_city", 10)):
                        break
                self.ctx.sleep(1.0)
        self.panel_path.write_text(json.dumps(panel, indent=1, ensure_ascii=False))
        return panel

    def load_panel(self) -> list[dict]:
        if not self.panel_path.exists():
            raise NotConfigured("no Tankerkönig station panel yet — run `oco tankerkoenig build-panel` once")
        return json.loads(self.panel_path.read_text())["stations"]

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        wh.con.execute(PUMP_SCHEMA)
        stations = self.load_panel()
        meta = {s["id"]: s for s in stations}
        now = utcnow().replace(microsecond=0)
        rows = []
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            for i in range(0, len(stations), 10):
                ids = ",".join(s["id"] for s in stations[i:i + 10])
                r = client.get(API + "prices.php", params={"ids": ids, "apikey": self.secrets()[0]})
                result.n_requests += 1
                payload = r.json()
                self._check(payload)
                body = r.content.replace(self.secrets()[0].encode(), b"<redacted>")
                sha = wh.store_raw("tankerkoenig", body, url=r.url, retrieved_at=r.retrieved_at, content_type=r.content_type, ext="json")
                result.raw.append(sha)
                for sid, p in (payload.get("prices") or {}).items():
                    m = meta.get(sid, {})
                    vals = {f: (float(p[f]) if isinstance(p.get(f), (int, float)) and p.get(f) else None) for f in FUELS}
                    rows.append([sid, m.get("city"), m.get("brand"), now, p.get("status"), vals["diesel"], vals["e5"], vals["e10"], sha])
        if rows:
            wh.con.executemany("INSERT OR IGNORE INTO pump_prices VALUES (?,?,?,?,?,?,?,?,?)", rows)
        open_n = sum(1 for r in rows if r[4] == "open")
        result.message = f"{len(rows)} station prices stored ({open_n} open) at {now:%Y-%m-%d %H:%M} UTC"
        result.saw(now.astimezone(BERLIN).date())
        self.aggregate_days(wh, result)

    def aggregate_days(self, wh: Warehouse, result: RunResult):
        """Write COMPLETED Berlin days only (today is still changing)."""
        df = wh.con.execute("SELECT * FROM pump_prices WHERE status='open'").df()
        if df.empty:
            return
        df["day"] = pd.to_datetime(df["fetched_at"], utc=True).dt.tz_convert(BERLIN).dt.date
        today = utcnow().astimezone(BERLIN).date()
        df = df[df["day"] < today]
        attribution = self.ctx.policy.connector("tankerkoenig").meta["attribution"]
        groups = [("panel", df)] + [(slug(c), g) for c, g in df.groupby("city")]
        for name, g in groups:
            for fuel in FUELS:
                for stat in (("mean", "median") if name == "panel" else ("mean",)):
                    sid = f"tk.{name}.{fuel}.{stat}"
                    agg = g.groupby("day")[fuel].agg(stat)
                    cnt = g.groupby("day")[fuel].count()
                    if agg.dropna().empty:
                        continue
                    wh.upsert_series(sid, source="Tankerkönig", source_key=sid,
                                     name=f"{'Panel' if name == 'panel' else name.title()} {fuel.upper() if fuel != 'diesel' else 'diesel'} pump price, daily {stat} (sample panel)",
                                     geography="DE" if name == "panel" else name, product=fuel, unit="EUR per litre", frequency="daily",
                                     description="Daily statistic over all 10-minute station price readings of open stations in the fixed panel",
                                     metadata={"attribution": attribution, "panel": "fixed sample of ~10 stations per city in 10 cities",
                                               "limitations": ["sample panel, not the national average", "urban stations only"]})
                    obs = [{"obs_start": d, "obs_end": d, "value": None if pd.isna(v) else round(float(v), 4),
                            "attrs": {"n_readings": int(cnt.get(d, 0))}} for d, v in agg.items()]
                    result.add(sid, wh.ingest_observations(sid, obs, raw_sha256=None))

    def probe(self):
        self.require_credentials()
        c = self.cfg["cities"][0]
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            r = client.get(API + "list.php", params={"lat": c["lat"], "lng": c["lng"], "rad": 2, "sort": "dist", "type": "all",
                                                     "apikey": self.secrets()[0]})
        payload = r.json()
        self._check(payload)
        st = payload.get("stations", [])
        return True, f"{len(st)} stations within 2 km of {c['name']}; licence {payload.get('license')}"


def latest_snapshot(con) -> dict | None:
    """Most recent panel reading (today, intraday) straight from the raw table."""
    try:
        t = con.execute("SELECT MAX(fetched_at) FROM pump_prices").fetchone()[0]
    except Exception:  # noqa: BLE001 — table absent before first run
        return None
    if t is None:
        return None
    df = con.execute("SELECT * FROM pump_prices WHERE fetched_at=? AND status='open'", [t]).df()
    return {"fetched_at": str(t), "n_open": int(len(df)),
            **{f: (float(df[f].mean()) if df[f].notna().any() else None) for f in FUELS}}
