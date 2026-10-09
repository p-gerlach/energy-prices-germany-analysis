"""Data for the world shipping map: daily chokepoint transits by vessel type, Gulf port exports, and the
static reference shapes (land, IMF shipping lanes, locations). Everything is read from the warehouse;
the browser only aggregates (7-day means) and draws."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from ..settings import sources_config

TYPES = [("tanker", "n_tanker"), ("container", "n_container"), ("dry_bulk", "n_dry_bulk"),
         ("general_cargo", "n_general_cargo"), ("roro", "n_roro")]
START = "2019-01-01"


def _meta(con, key):
    r = con.execute("SELECT value FROM meta WHERE key=?", [key]).fetchone()
    return json.loads(r[0]) if r else None


def _daily(con, sids: list[str]) -> pd.DataFrame:
    if not sids:
        return pd.DataFrame()
    q = ("SELECT series_id, obs_end, value FROM observation_versions WHERE is_current AND obs_end >= CAST(? AS DATE) "
         f"AND series_id IN ({','.join('?' * len(sids))})")
    df = con.execute(q, [START, *sids]).df()
    if df.empty:
        return df
    return df.pivot_table(index="obs_end", columns="series_id", values="value", aggfunc="last")


def world_map(con) -> dict | None:
    cps_geo = (_meta(con, "geo.chokepoints") or {}).get("items") or []
    if not cps_geo:
        return None
    cfg = sources_config()
    base = cfg.get("shipping_baseline", {"start": "2025-01-01", "end": "2025-12-31"})
    ports_geo = (_meta(con, "geo.ports") or {}).get("items") or []
    sids = [f"portwatch.{c['slug']}.{f}" for c in cps_geo for _, f in TYPES]
    sids += [f"portwatch.port.{p['slug']}.export_tanker" for p in ports_geo]
    wide = _daily(con, sids)
    if wide.empty:
        return None
    wide.index = pd.to_datetime(wide.index)
    idx = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    wide = wide.reindex(idx)

    def col(sid, digits=0):
        if sid not in wide:
            return None
        s = wide[sid]
        return [None if pd.isna(v) else (int(round(v)) if digits == 0 else round(float(v), digits)) for v in s.values]

    cps = []
    for c in cps_geo:
        series = {k: col(f"portwatch.{c['slug']}.{f}") for k, f in TYPES}
        if all(v is None for v in series.values()):
            continue
        b = wide.loc[base["start"]:base["end"]]
        baseline = {k: (round(float(b[f"portwatch.{c['slug']}.{f}"].mean()), 2) if f"portwatch.{c['slug']}.{f}" in b else None) for k, f in TYPES}
        cps.append({**c, "s": series, "base": baseline})
    ports = []
    for p in ports_geo:
        x = col(f"portwatch.port.{p['slug']}.export_tanker")
        if x is not None:
            bsl = wide.loc[base["start"]:base["end"], f"portwatch.port.{p['slug']}.export_tanker"].mean()
            ports.append({**p, "export": [None if v is None else round(v / 1000, 1) for v in x],
                          "base": None if pd.isna(bsl) else round(float(bsl) / 1000, 1)})
    land = (_meta(con, "geo.land") or {}).get("rings") or []
    routes = (_meta(con, "geo.routes") or {}).get("paths") or []
    return {"start": idx[0].strftime("%Y-%m-%d"), "n": len(idx), "last": idx[-1].strftime("%Y-%m-%d"),
            "base_label": f"{base['start'][:4]} average", "types": [k for k, _ in TYPES],
            "cps": cps, "ports": ports, "land": land, "routes": routes,
            "events": [{"x": "2023-11-19", "label": "Red Sea attacks begin"}, {"x": "2026-03-01", "label": "Hormuz transits collapse"}]}
