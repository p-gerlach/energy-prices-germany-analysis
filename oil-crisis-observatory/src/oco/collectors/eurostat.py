"""Eurostat dissemination API (JSON-stat 2.0), no key. One series per category of a chosen dimension.

Configured in sources.yaml -> eurostat.datasets. Codes and dimensions were checked against the live API;
an unknown code or a changed dimension raises SchemaChanged instead of silently storing the wrong series.
"""
from __future__ import annotations

from datetime import date
from itertools import product

import pandas as pd

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"


def decode_jsonstat(d: dict) -> pd.DataFrame:
    """Flatten a JSON-stat 2.0 dataset into rows: one column per dimension + value (+ status)."""
    if "error" in d:
        raise SchemaChanged(f"Eurostat error: {str(d['error'])[:200]}")
    ids, sizes = d["id"], d["size"]
    cats = []
    for k in ids:
        idx = d["dimension"][k]["category"]["index"]
        order = sorted(idx, key=idx.get) if isinstance(idx, dict) else list(idx)
        cats.append(order)
    values, status = d.get("value", {}), d.get("status", {})
    rows = []
    for flat, combo in enumerate(product(*cats)):
        v = values.get(str(flat)) if isinstance(values, dict) else (values[flat] if flat < len(values) else None)
        rows.append(list(combo) + [v, (status.get(str(flat)) if isinstance(status, dict) else None)])
    df = pd.DataFrame(rows, columns=ids + ["value", "status"])
    return df


class EurostatCollector(Collector):
    key = "eurostat"
    connector = "eurostat"

    def fetch(self, client, ds: dict, result: RunResult):
        params = []
        for k, v in ds["params"].items():
            for x in (v if isinstance(v, list) else [v]):
                params.append((k, x))
        params += [("sinceTimePeriod", self.cfg.get("since", "2018-01")), ("lang", "EN")]
        r = client.get(API + ds["id"], params=params)
        result.n_requests += 1
        return r

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        attribution = self.ctx.policy.connector("eurostat").meta["attribution"]
        with self.ctx.client(self.connector) as client:
            for ds in self.cfg["datasets"]:
                r = self.fetch(client, ds, result)
                d = r.json()
                df = decode_jsonstat(d)
                by = ds["by"]
                if by not in df.columns or "time" not in df.columns:
                    raise SchemaChanged(f"Eurostat {ds['id']}: dimensions {list(df.columns)} lack {by!r}/time")
                wanted = ds["params"].get(by)
                if wanted:
                    have = set(df[by])
                    missing = [w for w in (wanted if isinstance(wanted, list) else [wanted]) if w not in have]
                    if missing:
                        self.ctx.state.log(self.key, "WARN", f"{ds['id']}: codes not in response {missing}")
                sha = wh.store_raw("eurostat", r.content, url=r.url, retrieved_at=r.retrieved_at, content_type=r.content_type, ext="json",
                                   note=f"{ds['id']} updated {d.get('updated')}")
                result.raw.append(sha)
                labels = d["dimension"][by]["category"].get("label", {})
                published = pd.Timestamp(d["updated"]).tz_convert("UTC").to_pydatetime() if d.get("updated") else None
                for code, g in df.groupby(by):
                    if g["value"].dropna().abs().sum() == 0 and not wanted:
                        continue  # partner never supplied anything in the window: skip, but it is not stored as zero
                    sid = f"eurostat.{ds['id']}.{ds['params'].get('geo', 'x')}.{code}".lower()
                    nm = ds["name"].format(**{by: code, "partner": labels.get(code, code), "coicop18": code, "label": labels.get(code, code)})
                    wh.upsert_series(sid, source="Eurostat", source_key=f"{ds['id']}:{code}", name=nm, geography=ds["params"].get("geo", ""),
                                     product=ds["id"], unit=ds["unit"], frequency="monthly",
                                     description=f"Eurostat {d.get('label')} ({ds['id']})", metadata={"attribution": attribution, "label": labels.get(code, code)})
                    rows = []
                    for _, rr in g.sort_values("time").iterrows():
                        y, m = map(int, str(rr["time"])[:7].split("-"))
                        end = (pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)).date()
                        v = rr["value"]
                        rows.append({"obs_start": date(y, m, 1), "obs_end": end, "value": None if v is None else float(v),
                                     "status": "ok" if v is not None else "missing", "source_published_at": published,
                                     "attrs": {"flag": rr["status"]}})
                        if v is not None:
                            result.saw(end)
                    result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=sha))

    def probe(self):
        res = RunResult(self.key)
        ds = self.cfg["datasets"][0]
        with self.ctx.client(self.connector) as client:
            r = self.fetch(client, ds, res)
        df = decode_jsonstat(r.json()).dropna(subset=["value"])
        return True, f"{ds['id']}: {len(df)} values, latest month {df['time'].max()}"
