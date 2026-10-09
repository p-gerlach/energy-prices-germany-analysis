"""Copernicus Data Space Ecosystem — PUBLIC read-only OData catalogue search.

No subscriptions, no Sentinel Hub, no openEO. Catalogue metadata being reachable does NOT mean that
products are downloadable: raw downloads need the user's free General User login and are handled
separately (oco.satellite.download), within a disk/download budget.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .. import geo
from ..storage.state import utcnow
from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def odata_filter(mission: str, cfg: dict, wkt: str, start: datetime, end: datetime) -> str:
    c = cfg["collections"][mission]
    parts = [
        f"Collection/Name eq '{c['odata_collection']}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        f"ContentDate/Start gt {start:%Y-%m-%dT%H:%M:%S.000Z}",
        f"ContentDate/Start lt {end:%Y-%m-%dT%H:%M:%S.000Z}",
    ]
    if mission == "sentinel1":
        parts.append(f"contains(Name,'{c.get('product_type_contains', 'GRD')}')")
    else:
        parts.append("Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and "
                     f"att/OData.CSC.StringAttribute/Value eq '{c['product_type']}')")
        if cfg.get("max_cloud_cover_s2") is not None:
            parts.append("Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and "
                         f"att/OData.CSC.DoubleAttribute/Value le {float(cfg['max_cloud_cover_s2']):.1f})")
    return " and ".join(parts)


def _attrs(item: dict) -> dict:
    return {a.get("Name"): a.get("Value") for a in item.get("Attributes", []) or []}


def _ts(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def footprint_wkt(item: dict) -> str | None:
    fp = item.get("Footprint") or ""
    m = re.search(r"(MULTI)?POLYGON\s*\(.*\)", fp, re.I)
    if m:
        return m.group(0)
    g = item.get("GeoFootprint")
    if g and g.get("type") == "Polygon":
        return geo.polygon_wkt(g)
    return None


def scene_record(item: dict, mission: str) -> dict:
    a = _attrs(item)
    return {
        "product_id": item["Id"],
        "product_name": item.get("Name"),
        "mission": "Sentinel-1" if mission == "sentinel1" else "Sentinel-2",
        "product_type": a.get("productType"),
        "collection": mission,
        "acquisition_start": _ts((item.get("ContentDate") or {}).get("Start")),
        "acquisition_end": _ts((item.get("ContentDate") or {}).get("End")),
        "published_at": _ts(item.get("PublicationDate")),
        "modified_at": _ts(item.get("ModificationDate")),
        "footprint_wkt": footprint_wkt(item),
        "orbit_direction": a.get("orbitDirection"),
        "relative_orbit": int(a["relativeOrbitNumber"]) if a.get("relativeOrbitNumber") not in (None, "") else None,
        "polarisation": a.get("polarisationChannels"),
        "instrument_mode": a.get("operationalMode") or a.get("sensorMode"),
        "cloud_cover": float(a["cloudCover"]) if a.get("cloudCover") not in (None, "") else None,
        "tile_id": a.get("tileId"),
        "platform": a.get("platformSerialIdentifier") or a.get("platformShortName"),
        "online": item.get("Online"),
        "size_bytes": item.get("ContentLength"),
        "attrs": json.dumps({k: a[k] for k in sorted(a) if k in (
            "processingBaseline", "processorVersion", "timeliness", "swathIdentifier", "productClass",
            "beginningDateTime", "endingDateTime", "spatialResolution", "orbitNumber")}, default=str),
    }


def intersection_fraction(scene_wkt: str | None, aoi_geom: dict) -> float | None:
    if not scene_wkt:
        return None
    try:
        from shapely import wkt as swkt
        from shapely.geometry import shape
    except ImportError:
        return None  # satellite extra not installed; reported as unknown
    try:
        s = swkt.loads(scene_wkt)
        a = shape(aoi_geom)
        return float(s.intersection(a).area / a.area) if a.area > 0 else None
    except Exception:  # noqa: BLE001
        return None


class CDSECatalogueCollector(Collector):
    key = "cdse"
    connector = "cdse_catalogue"

    def search(self, client, mission: str, aoi: dict, start: datetime, end: datetime, result: RunResult) -> list[dict]:
        flt = odata_filter(mission, self.cfg, geo.polygon_wkt(aoi["geometry"]), start, end)
        params = {"$filter": flt, "$orderby": "ContentDate/Start desc", "$top": int(self.cfg.get("max_items_per_query", 100)),
                  "$expand": "Attributes"}
        url = ODATA
        items: list[dict] = []
        pages = 0
        while url and pages < 10:
            self.ctx.sleep(float(self.cfg.get("min_seconds_between_requests", 3)))  # avoid firewall bursts
            r = client.get(url, params=params if pages == 0 else None)
            result.n_requests += 1
            payload = r.json()
            if "value" not in payload:
                raise SchemaChanged(f"CDSE OData response lacks 'value': {str(payload)[:200]}")
            items.extend(payload["value"])
            url = payload.get("@odata.nextLink")
            pages += 1
        return items

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        days = int(kw.get("days") or self.cfg.get("lookback_days", 60))
        end = utcnow()
        start = end - timedelta(days=days)
        aois = [f for f in geo.regions()] + [f for f in geo.facilities(verified_only=True)]
        with self.ctx.client(self.connector) as client:
            for aoi in aois:
                aid = aoi["properties"]["id"]
                use = aoi["properties"].get("use", "sentinel2_facility")
                missions = ["sentinel1"] if use == "sentinel1_snapshot" else ["sentinel2", "sentinel1"]
                for mission in missions:
                    items = self.search(client, mission, aoi, start, end, result)
                    body = json.dumps(items, sort_keys=True, default=str).encode()
                    sha = wh.store_raw("cdse", body, url=f"{ODATA}?aoi={aid}&mission={mission}", content_type="application/json", ext="json")
                    result.raw.append(sha)
                    for it in items:
                        rec = scene_record(it, mission)
                        exists = wh.con.execute("SELECT modified_at FROM satellite_scenes WHERE product_id=?", [rec["product_id"]]).fetchone()
                        if exists is None:
                            cols = list(rec) + ["first_seen_at", "download_status", "local_path", "local_sha256"]
                            vals = list(rec.values()) + [utcnow(), "not_downloaded", None, None]
                            wh.con.execute(f"INSERT INTO satellite_scenes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
                            result.counts["new"] += 1
                        else:
                            if rec["modified_at"] and exists[0] and rec["modified_at"] > exists[0]:
                                wh.con.execute("UPDATE satellite_scenes SET modified_at=?, online=?, attrs=? WHERE product_id=?",
                                               [rec["modified_at"], rec["online"], rec["attrs"], rec["product_id"]])
                                result.counts["revised"] += 1
                            else:
                                result.counts["unchanged"] += 1
                        frac = intersection_fraction(rec["footprint_wkt"], aoi["geometry"])
                        wh.con.execute("INSERT OR REPLACE INTO scene_aoi VALUES (?,?,?)", [rec["product_id"], aid, frac])
                        if rec["acquisition_start"]:
                            result.saw(rec["acquisition_start"].date())

    def probe(self):
        res = RunResult(self.key)
        aoi = geo.regions()[0]
        end = utcnow()
        with self.ctx.client(self.connector) as client:
            items = self.search(client, "sentinel1", aoi, end - timedelta(days=14), end, res)
        if not items:
            return True, f"catalogue reachable; 0 Sentinel-1 GRD scenes over {aoi['properties']['id']} in 14 days"
        latest = scene_record(items[0], "sentinel1")
        return True, (f"{len(items)} S1 GRD scenes over {aoi['properties']['id']} in 14 days; latest acquisition "
                      f"{latest['acquisition_start']} (catalogue metadata only — downloads untested)")
