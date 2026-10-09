"""Satellite catalogue coverage report and comparison-validity rules.

Establish ACTUAL coverage from the catalogue before promising any comparison. Nominal mission revisit is
not usable coverage over a specific polygon.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

S1_KEYS = ("relative_orbit", "orbit_direction", "instrument_mode", "polarisation")


def scenes_for(con, aoi_id: str, mission: str | None = None, min_fraction: float = 0.0) -> pd.DataFrame:
    q = ("SELECT s.*, a.intersection_fraction FROM satellite_scenes s JOIN scene_aoi a USING(product_id) WHERE a.aoi_id=?")
    args = [aoi_id]
    if mission:
        q += " AND s.mission=?"
        args.append(mission)
    df = con.execute(q + " ORDER BY acquisition_start", args).df()
    if not df.empty and min_fraction > 0:
        df = df[df["intersection_fraction"].fillna(0) >= min_fraction]
    return df


def s1_group(row) -> str:
    return f"rel{row['relative_orbit']}-{(row['orbit_direction'] or '?')[:3]}-{row['instrument_mode']}-{row['polarisation']}"


def coverage_summary(con, aoi_id: str, mission: str, min_fraction: float = 0.9, download_tested: bool = False) -> dict:
    df = scenes_for(con, aoi_id, mission)
    if df.empty:
        return {"aoi": aoi_id, "mission": mission, "n_scenes": 0, "note": "no catalogue scenes recorded (run `oco refresh --source cdse`)"}
    usable = df[df["intersection_fraction"].fillna(0) >= min_fraction]
    days = sorted(pd.to_datetime(usable["acquisition_start"]).dt.normalize().unique())
    gaps = np.diff(np.array(days, dtype="datetime64[D]")).astype(int) if len(days) > 1 else np.array([])
    lat = (pd.to_datetime(df["published_at"]) - pd.to_datetime(df["acquisition_end"])).dt.total_seconds() / 3600
    out = {
        "aoi": aoi_id, "mission": mission, "n_scenes": int(len(df)),
        "n_scenes_covering_min_fraction": int(len(usable)), "min_fraction": min_fraction,
        "first_acquisition": str(pd.to_datetime(df["acquisition_start"]).min()),
        "last_acquisition": str(pd.to_datetime(df["acquisition_start"]).max()),
        "distinct_usable_days": len(days),
        "median_gap_days": float(np.median(gaps)) if gaps.size else None,
        "max_gap_days": int(gaps.max()) if gaps.size else None,
        "median_catalogue_latency_h": float(lat.median()) if lat.notna().any() else None,
        "intersection_fraction_unknown": int(df["intersection_fraction"].isna().sum()),
        "asset_access": ("raw download tested with your free CDSE login" if download_tested else
                         "UNTESTED: raw downloads need a free CDSE General User login; catalogue metadata alone does not prove downloadability"),
    }
    if mission == "Sentinel-1":
        grp = usable.apply(s1_group, axis=1) if not usable.empty else pd.Series(dtype=str)
        out["comparable_groups"] = grp.value_counts().to_dict()
    else:
        out["cloud_cover_median"] = float(usable["cloud_cover"].median()) if not usable.empty and usable["cloud_cover"].notna().any() else None
        out["tiles"] = sorted(usable["tile_id"].dropna().unique().tolist()) if not usable.empty else []
    return out


def scene_table(con, aoi_id: str) -> pd.DataFrame:
    df = scenes_for(con, aoi_id)
    if df.empty:
        return df
    cols = ["product_id", "product_name", "mission", "product_type", "acquisition_start", "published_at", "first_seen_at",
            "intersection_fraction", "orbit_direction", "relative_orbit", "polarisation", "instrument_mode", "cloud_cover",
            "tile_id", "platform", "online", "size_bytes", "download_status"]
    return df[[c for c in cols if c in df.columns]]


def comparable_s1(a: dict, b: dict, aoi_fraction_min: float = 0.9) -> tuple[bool, list[str]]:
    reasons = []
    for k in S1_KEYS:
        if a.get(k) != b.get(k):
            reasons.append(f"{k} differs ({a.get(k)} vs {b.get(k)})")
    for s in (a, b):
        fr = s.get("intersection_fraction")
        if fr is None or fr < aoi_fraction_min:
            reasons.append(f"{s.get('product_id')}: AOI coverage {fr} < {aoi_fraction_min}")
    if (a.get("product_type") or "")[:7] != (b.get("product_type") or "")[:7]:
        reasons.append("product types differ")
    return (not reasons), reasons


def comparable_s2(a: dict, b: dict, aoi_fraction_min: float = 0.99) -> tuple[bool, list[str]]:
    reasons = []
    if a.get("tile_id") != b.get("tile_id"):
        reasons.append(f"different MGRS tiles ({a.get('tile_id')} vs {b.get('tile_id')}) — grids not co-registered")
    for s in (a, b):
        fr = s.get("intersection_fraction")
        if fr is None or fr < aoi_fraction_min:
            reasons.append(f"{s.get('product_id')}: facility not fully inside footprint ({fr})")
    return (not reasons), reasons


def to_json(d) -> str:
    return json.dumps(d, default=str, indent=2)
