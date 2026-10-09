"""Satellite task runners: S1 snapshot detection, S2 before/after comparison, CEMS manual import.
All processing is local; inputs are files downloaded through the approved raw route (or imported manually)."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from .. import geo
from ..collectors.base import Context
from ..storage.warehouse import Warehouse, now_utc, sha1


def _scene(wh: Warehouse, product_id: str) -> dict:
    df = wh.con.execute("SELECT s.*, a.intersection_fraction FROM satellite_scenes s LEFT JOIN scene_aoi a USING(product_id) "
                        "WHERE product_id=?", [product_id]).df()
    if df.empty:
        raise KeyError(f"unknown product {product_id}")
    return df.iloc[0].to_dict()


def run_s1(ctx: Context, wh: Warehouse, product_id: str, aoi_id: str, zip_path: Path | None = None) -> dict:
    from . import landmask, s1_detect
    from .coverage import s1_group

    aoi = next((r for r in geo.regions() if r["properties"]["id"] == aoi_id), None)
    if aoi is None:
        raise KeyError(f"AOI {aoi_id} not in config/regions.geojson")
    sc = _scene(wh, product_id)
    zp = Path(zip_path or sc.get("local_path") or "")
    if not zp.exists():
        raise FileNotFoundError("product not downloaded; queue it with `oco satellite download --product-id ...`")
    land = landmask.load_land_geoms(landmask.ensure_land(ctx), geo.buffer_bbox(geo.bbox(aoi["geometry"]), 50))
    res = s1_detect.detect(zp, aoi, land, s1_detect.fixed_structures())
    group = s1_group(sc)
    for c in res["candidates"]:
        did = sha1(product_id, aoi_id, round(c["lon"], 5), round(c["lat"], 5), res["method_version"])
        wh.con.execute("INSERT OR REPLACE INTO candidate_detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       [did, product_id, aoi_id, c["lon"], c["lat"], c["area_px"], c["peak_db"], c["contrast_db"],
                        json.dumps(c["quality"]), c["review_status"], res["method_version"], now_utc()])
    wh.con.execute("INSERT OR REPLACE INTO snapshot_counts VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [product_id, aoi_id, sc["acquisition_start"], res["count"], res["quality"]["valid_water_fraction"],
                    group, res["method_version"], res["unit"], json.dumps(res["quality"]), now_utc()])
    return {"product_id": product_id, "aoi": aoi_id, "count": res["count"], "group": group, "quality": res["quality"],
            "n_candidates_total": len(res["candidates"])}


def snapshot_comparison(con, aoi_id: str, before: str, after: str) -> dict:
    """Compare snapshot counts only within ONE comparable acquisition group; refuse otherwise."""
    from .coverage import comparable_s1

    rows = {r["product_id"]: r for r in con.execute(
        "SELECT c.*, s.relative_orbit, s.orbit_direction, s.instrument_mode, s.polarisation, s.product_type, a.intersection_fraction "
        "FROM snapshot_counts c JOIN satellite_scenes s USING(product_id) LEFT JOIN scene_aoi a ON a.product_id=c.product_id AND a.aoi_id=c.aoi_id "
        "WHERE c.aoi_id=?", [aoi_id]).df().to_dict("records")}
    if before not in rows or after not in rows:
        return {"valid": False, "reasons": ["snapshot counts missing for one or both products"]}
    a, b = rows[before], rows[after]
    ok, reasons = comparable_s1(a, b)
    for r in (a, b):
        if r["candidate_count"] is None:
            ok = False
            reasons.append(f"{r['product_id']}: insufficient valid water coverage ({r['valid_water_fraction']:.0%})")
    out = {"valid": ok, "reasons": reasons, "unit": "candidate vessels present in one radar snapshot (count)",
           "note": "Occupancy snapshot, not throughput. Compare with PortWatch daily transits separately; anchoring/slow steaming raise occupancy without raising transits."}
    if ok:
        out.update({"before": {"time": str(a["acquisition_start"]), "count": a["candidate_count"]},
                    "after": {"time": str(b["acquisition_start"]), "count": b["candidate_count"]},
                    "difference": b["candidate_count"] - a["candidate_count"]})
    return out


def run_s2(ctx: Context, wh: Warehouse, facility_id: str, pre_ids: list[str], post_id: str, event_date: str) -> dict:
    from . import s2_compare

    fac = next((f for f in geo.facilities(verified_only=True) if f["properties"]["id"] == facility_id), None)
    if fac is None:
        raise KeyError(f"facility {facility_id} not VERIFIED (needs boundary + source links + verified: true)")
    zips = []
    for pid in pre_ids + [post_id]:
        sc = _scene(wh, pid)
        if not sc.get("local_path") or not Path(sc["local_path"]).exists():
            raise FileNotFoundError(f"{pid} not downloaded")
        zips.append(Path(sc["local_path"]))
    cid = sha1(facility_id, ",".join(sorted(pre_ids)), post_id)
    out_dir = ctx.paths.satellite / "comparisons" / cid
    summary = s2_compare.compare(zips[:-1], zips[-1], fac, out_dir)
    wh.con.execute("INSERT OR REPLACE INTO comparisons VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [cid, "s2_before_after", facility_id, date.fromisoformat(event_date), json.dumps(pre_ids), json.dumps([post_id]),
                    summary["valid"], json.dumps(summary["reasons"]), json.dumps({"png": summary["png"], "change": summary["change"]}, default=str), now_utc()])
    return {"comparison_id": cid, **summary}


def cems_import(ctx: Context, wh: Warehouse, file: Path, activation: str, title: str, product_date: str, licence: str,
                source_url: str, notes: str = "") -> str:
    """Manual importer for ALREADY-PUBLISHED free Copernicus EMS products the user downloaded themselves."""
    if not file.exists():
        raise FileNotFoundError(file)
    h = hashlib.sha256(file.read_bytes()).hexdigest()
    dest = ctx.paths.satellite / "cems" / activation / file.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(file, dest)
    pid = sha1(activation, h)
    wh.con.execute("INSERT OR REPLACE INTO assessment_products VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   [pid, "Copernicus EMS (manual import)", activation, title, date.fromisoformat(product_date), licence, source_url,
                    str(dest), h, now_utc(), notes + " | derived map; underlying imagery may not be openly available"])
    return pid
