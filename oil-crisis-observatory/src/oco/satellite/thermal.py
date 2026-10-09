"""FIRMS thermal detections -> deduplicated events per VERIFIED facility, compared with the site's own history.

Classification vocabulary (never "destroyed", never "attack"):
  routine_flaring_pattern            most detections at known/persistent flare locations
  within_site_thermal_baseline       activity within the site's own historical range
  thermal_anomaly_near_facility      above the site's baseline quantile -> REVIEW ITEM, not a conclusion
  insufficient_baseline              not enough history to judge -> review item
Caveats carried on every event: clouds/smoke/viewing geometry cause missed overpasses (no detection is not
proof of no fire); a nominal 375 m pixel is not a damage footprint; different sensors are not identical.
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from .. import geo
from ..settings import sources_config
from ..storage.warehouse import Warehouse, now_utc, sha1

CAVEATS = [
    "Clouds, smoke, viewing geometry and missed overpasses mean no detection is not proof of no fire.",
    "VIIRS 375 m nominal pixels: footprint varies with scan angle; a small hot source can trigger detection.",
    "Routine flaring produces persistent detections; compare with the site's own history.",
    "A thermal anomaly is a timing clue, not evidence of damage; optical imagery and credible reporting are needed.",
]


def _sensor_family(source: str) -> str:
    return "VIIRS"  # SNPP/NOAA-20/NOAA-21 VIIRS I-band products; MODIS would be a separate family


def persistent_cells(df: pd.DataFrame, cell_deg: float = 0.004, min_share: float = 0.3) -> set[tuple[int, int]]:
    """Grid cells with detections on >= min_share of the days that had ANY detection at the site:
    a data-derived proxy for routine flare stacks (complements manually listed flare points)."""
    if df.empty:
        return set()
    d = df.assign(cx=(df["lon"] / cell_deg).round().astype(int), cy=(df["lat"] / cell_deg).round().astype(int),
                  day=pd.to_datetime(df["acq_datetime"]).dt.date)
    n_days = d["day"].nunique()
    if n_days < 10:
        return set()
    share = d.groupby(["cx", "cy"])["day"].nunique() / n_days
    return set(share[share >= min_share].index)


def cluster_events(det: pd.DataFrame, overpass_min: int = 10, event_gap_h: float = 1.0) -> list[pd.DataFrame]:
    """Detections from the same satellite within `overpass_min` minutes = one overpass; overpasses (any
    sensor) within `event_gap_h` hours = one event. Prevents simultaneous detections becoming many 'fires'."""
    if det.empty:
        return []
    d = det.sort_values("acq_datetime").reset_index(drop=True)
    events, cur = [], [0]
    for i in range(1, len(d)):
        gap = (d.loc[i, "acq_datetime"] - d.loc[cur[-1], "acq_datetime"]).total_seconds() / 3600
        if gap <= event_gap_h:
            cur.append(i)
        else:
            events.append(d.loc[cur])
            cur = [i]
    events.append(d.loc[cur])
    return events


def classify(event: pd.DataFrame, history: pd.DataFrame, routine_mask: pd.Series, cfg: dict) -> tuple[str, dict]:
    q = float(cfg.get("quantile", 0.95))
    min_days = int(cfg.get("min_baseline_days", 60))
    routine_share = float(routine_mask.mean()) if len(routine_mask) else 0.0
    daynight = event["daynight"].mode().iloc[0] if not event["daynight"].empty else "?"
    h = history[history["daynight"] == daynight] if not history.empty else history
    base = {"daynight": daynight, "history_days_with_detections": int(pd.to_datetime(h["acq_datetime"]).dt.date.nunique()) if not h.empty else 0}
    routine_thr = float(cfg.get("routine_share_threshold", 0.8))
    if routine_share >= routine_thr:
        return "routine_flaring_pattern", {**base, "routine_share": routine_share}
    if h.empty or base["history_days_with_detections"] < 5:
        return "insufficient_baseline", {**base, "routine_share": routine_share}
    hd = h.assign(day=pd.to_datetime(h["acq_datetime"]).dt.date)
    span_days = max(1, (hd["day"].max() - hd["day"].min()).days + 1)
    if span_days < min_days:
        return "insufficient_baseline", {**base, "span_days": span_days, "routine_share": routine_share}
    daily_counts = hd.groupby("day").size()
    daily_frp = hd.groupby("day")["frp"].max()
    n_now, frp_now = len(event), float(event["frp"].max())
    c_thr, f_thr = float(daily_counts.quantile(q)), float(daily_frp.quantile(q))
    base.update({"baseline_span_days": span_days, "count_quantile": c_thr, "frp_quantile": f_thr, "quantile": q,
                 "event_count": n_now, "event_max_frp": frp_now, "routine_share": routine_share,
                 "note": "baseline built from days WITH detections only (no-detection days are unobserved, not zero)"})
    if (n_now > c_thr or frp_now > f_thr) and routine_share < routine_thr:
        return "thermal_anomaly_near_facility", base
    return "within_site_thermal_baseline", base


def build_thermal_events(wh: Warehouse) -> dict:
    cfg = sources_config().get("anomaly_rules", {}).get("thermal", {})
    km = float(sources_config()["sources"].get("firms", {}).get("facility_buffer_km", 5))
    fac = geo.facilities(verified_only=True)
    out = {"facilities": len(fac), "events": 0, "review_items": 0}
    if not fac:
        out["note"] = "no verified facilities; thermal events not computed"
        return out
    det = wh.con.execute("SELECT * FROM thermal_detections").df()
    if det.empty:
        return out
    det["acq_datetime"] = pd.to_datetime(det["acq_datetime"], utc=True)
    for f in fac:
        fid = f["properties"]["id"]
        g = f["geometry"]
        dist = det.apply(lambda r: geo.distance_to_geom_km(r["lon"], r["lat"], g), axis=1)
        site = det[dist <= km].copy()
        if site.empty:
            continue
        flare_pts = f["properties"].get("routine_flare_points") or []
        cells = persistent_cells(site)
        cell_deg = 0.004

        def is_routine(r):
            if any(geo.haversine_km(r["lon"], r["lat"], p[0], p[1]) <= 0.5 for p in flare_pts):
                return True
            return (round(r["lon"] / cell_deg), round(r["lat"] / cell_deg)) in cells
        site["routine"] = site.apply(is_routine, axis=1)
        for ev in cluster_events(site):
            start = ev["acq_datetime"].min()
            history = site[site["acq_datetime"] < start - timedelta(days=1)]
            label, base = classify(ev, history, ev["routine"], cfg)
            eid = sha1(fid, start.isoformat(), ",".join(sorted(ev["det_id"])))
            wh.con.execute(
                "INSERT OR REPLACE INTO thermal_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [eid, fid, start.to_pydatetime(), ev["acq_datetime"].max().to_pydatetime(), len(ev),
                 int(ev.groupby(["satellite"]).ngroups), json.dumps(sorted(ev["source"].unique().tolist())),
                 ",".join(sorted(ev["daynight"].unique())), float(ev["frp"].max()), float(ev["routine"].mean()), label,
                 json.dumps({**base, "caveats": CAVEATS}, default=str), json.dumps(sorted(ev["det_id"])),
                 "unreviewed", now_utc()])
            out["events"] += 1
            out["review_items"] += int(label in ("thermal_anomaly_near_facility", "insufficient_baseline"))
    return out
