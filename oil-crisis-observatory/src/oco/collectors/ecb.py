"""ECB SDMX API: USD-per-EUR reference rates (public, no key)."""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

BASE = "https://data-api.ecb.europa.eu/service/data/EXR/"


def parse_ecb_csv(content: bytes) -> pd.DataFrame:
    text = content.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise SchemaChanged("ECB returned markup instead of CSV")
    if not text.strip():
        return pd.DataFrame(columns=["TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"])
    df = pd.read_csv(io.StringIO(text), dtype=str)
    for col in ("TIME_PERIOD", "OBS_VALUE"):
        if col not in df.columns:
            raise SchemaChanged(f"ECB CSV lacks column {col}; got {list(df.columns)[:12]}")
    if "UNIT" in df.columns and not df["UNIT"].dropna().isin(["USD"]).all():
        raise SchemaChanged(f"unexpected ECB UNIT values {df['UNIT'].unique()[:5]}")
    return df


class ECBCollector(Collector):
    key = "ecb_fx"
    connector = "ecb"

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        with self.ctx.client(self.connector) as client:
            for s in self.cfg["series"]:
                wh.upsert_series(s["id"], source="ECB", source_key=s["key"], name=s["name"], geography="Euro area",
                                 product="fx", unit=s["unit"], frequency="daily",
                                 description="ECB euro foreign exchange reference rate (published ~16:00 CET on TARGET business days)",
                                 metadata={"attribution": self.ctx.policy.connector("ecb").meta["attribution"]})
                if mode == "backfill":
                    start = self.cfg.get("backfill_start", "2015-01-01")
                else:
                    start = (date.today() - timedelta(days=int(self.cfg.get("revision_overlap_days", 30)))).isoformat()
                r = client.get(BASE + s["key"], params={"format": "csvdata", "startPeriod": start})
                result.n_requests += 1
                df = parse_ecb_csv(r.content)
                sha = wh.store_raw("ecb", r.content, url=r.url, final_url=r.final_url, retrieved_at=r.retrieved_at,
                                   http_status=r.status, content_type=r.content_type, ext="csv",
                                   etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"))
                result.raw.append(sha)
                rows = []
                for _, row in df.iterrows():
                    v = pd.to_numeric(row.get("OBS_VALUE"), errors="coerce")
                    d = date.fromisoformat(str(row["TIME_PERIOD"])[:10])
                    rows.append({"obs_start": d, "obs_end": d, "value": None if pd.isna(v) else float(v),
                                 "status": "ok" if not pd.isna(v) else "missing",
                                 "attrs": {"OBS_STATUS": row.get("OBS_STATUS")}})
                    result.saw(d)
                result.add(s["id"], wh.ingest_observations(s["id"], rows, raw_sha256=sha, retrieved_at=r.retrieved_at))

    def probe(self):
        s = self.cfg["series"][0]
        with self.ctx.client(self.connector) as client:
            r = client.get(BASE + s["key"], params={"format": "csvdata", "lastNObservations": 1})
        df = parse_ecb_csv(r.content)
        if df.empty:
            return False, "ECB returned no rows"
        return True, f"latest {df['TIME_PERIOD'].iloc[-1]} = {df['OBS_VALUE'].iloc[-1]} USD/EUR"
