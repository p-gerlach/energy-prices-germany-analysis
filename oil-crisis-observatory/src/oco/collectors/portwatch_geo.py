"""Static map reference for the world shipping view: chokepoint and port locations plus IMF's global
shipping-lane geometry (anonymous PortWatch reference layers), and a simplified Natural Earth land outline.

These are reference shapes, not measurements. The lanes are IMF's static drawing of common routes; the app
never sizes or animates them by traffic it has not measured. Measured traffic is the daily chokepoint data.
Stored in the warehouse `meta` table as compact JSON (keys geo.chokepoints, geo.ports, geo.routes, geo.land).
"""
from __future__ import annotations

import json

from ..storage.state import utcnow
from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
CHOKEPOINTS = f"{BASE}/PortWatch_chokepoints_database/FeatureServer/0/query"
PORTS = f"{BASE}/PortWatch_ports_database/FeatureServer/0/query"
ROUTES = f"{BASE}/Global_Shipping_Routes/FeatureServer/15/query"


def _json(r, what: str) -> dict:
    payload = r.json()
    if "error" in payload:
        raise SchemaChanged(f"PortWatch {what} error: {str(payload['error'])[:200]}")
    return payload


class PortWatchGeoCollector(Collector):
    key = "portwatch_geo"
    connector = "portwatch"

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        cps_cfg = sources_cfg_chokepoints()
        port_cfg = {p["portid"]: p for p in sources_cfg_ports()}
        with self.ctx.client(self.connector) as client:
            r = client.get(CHOKEPOINTS, params={"where": "1=1", "outFields": "portid,portname,lat,lon",
                                                "returnGeometry": "false", "f": "json"})
            result.n_requests += 1
            wh.store_raw("portwatch", r.content, url=CHOKEPOINTS, content_type="application/json", ext="json", note="chokepoint locations")
            feats = [f["attributes"] for f in _json(r, "chokepoint locations").get("features", [])]
            by_name = {a["portname"].lower(): a for a in feats if a.get("portname")}
            cps = []
            for cp in cps_cfg:
                a = next((by_name[n.lower()] for n in cp["names"] if n.lower() in by_name), None)
                if a is None or a.get("lat") is None:
                    continue
                cps.append({"slug": cp["slug"], "name": a["portname"], "portid": a["portid"],
                            "lat": round(float(a["lat"]), 3), "lon": round(float(a["lon"]), 3), "screen": bool(cp.get("screen"))})
            if len(cps) < len(cps_cfg) // 2:
                raise SchemaChanged(f"only {len(cps)} of {len(cps_cfg)} chokepoint locations resolved")

            ids = ",".join(f"'{pid}'" for pid in port_cfg)
            r = client.get(PORTS, params={"where": f"portid IN ({ids})", "outFields": "portid,portname,country,lat,lon",
                                          "returnGeometry": "false", "f": "json"})
            result.n_requests += 1
            wh.store_raw("portwatch", r.content, url=PORTS, content_type="application/json", ext="json", note="port locations")
            ports = []
            for a in (f["attributes"] for f in _json(r, "port locations").get("features", [])):
                p = port_cfg.get(a.get("portid"))
                if p and a.get("lat") is not None:
                    ports.append({"slug": p["slug"], "name": p["name"], "group": p["group"], "portid": a["portid"],
                                  "lat": round(float(a["lat"]), 3), "lon": round(float(a["lon"]), 3)})

            r = client.get(ROUTES, params={"where": "1=1", "outFields": "FID", "returnGeometry": "true",
                                           "maxAllowableOffset": "0.05", "geometryPrecision": "2", "outSR": "4326", "f": "json"})
            result.n_requests += 1
            wh.store_raw("portwatch", r.content, url=ROUTES, content_type="application/json", ext="json", note="shipping lanes")
            paths = [p for f in _json(r, "shipping lanes").get("features", []) for p in (f.get("geometry") or {}).get("paths", [])]
            if not paths:
                raise SchemaChanged("shipping-lane layer returned no geometry")

        now = utcnow().isoformat()
        wh.set_meta("geo.chokepoints", json.dumps({"fetched_at": now, "items": cps}))
        wh.set_meta("geo.ports", json.dumps({"fetched_at": now, "items": ports}))
        wh.set_meta("geo.routes", json.dumps({"fetched_at": now, "paths": paths}))
        land = simplified_land(self.ctx)
        if land:
            wh.set_meta("geo.land", json.dumps({"source": "Natural Earth 1:10m land, simplified", "rings": land}))
            result.message = f"{len(cps)} chokepoints, {len(ports)} ports, {len(paths)} lane paths, {len(land)} land rings"
        else:
            result.message = (f"{len(cps)} chokepoints, {len(ports)} ports, {len(paths)} lane paths; "
                              "no land outline (install the satellite extra for shapely)")

    def probe(self):
        with self.ctx.client(self.connector) as client:
            r = client.get(CHOKEPOINTS, params={"where": "portname='Strait of Hormuz'", "outFields": "portid,lat,lon",
                                                "returnGeometry": "false", "f": "json"})
        feats = _json(r, "chokepoint locations").get("features", [])
        if not feats:
            return False, "no chokepoint location returned"
        a = feats[0]["attributes"]
        return True, f"Strait of Hormuz at {a['lat']:.2f}, {a['lon']:.2f}"


def sources_cfg_chokepoints() -> list[dict]:
    from ..settings import sources_config
    return sources_config()["sources"]["portwatch"]["chokepoints"]


def sources_cfg_ports() -> list[dict]:
    from ..settings import sources_config
    return sources_config()["sources"]["portwatch_ports"]["ports"]


def simplified_land(ctx, tolerance: float = 0.12, min_area: float = 0.15) -> list[list[list[float]]] | None:
    """World land rings (lon, lat) simplified for an overview map. None if shapely is not installed."""
    try:
        import shapely  # noqa: F401
    except ImportError:
        return None
    from ..satellite.landmask import _read_shp_from_zip, ensure_land
    rings = []
    for g in _read_shp_from_zip(ensure_land(ctx)):
        for poly in getattr(g, "geoms", [g]):
            if poly.area < min_area:
                continue
            s = poly.simplify(tolerance, preserve_topology=True)
            for part in getattr(s, "geoms", [s]):
                if part.is_empty or not hasattr(part, "exterior"):
                    continue
                ring = [[round(x, 2), round(y, 2)] for x, y in part.exterior.coords]
                if len(ring) >= 4:
                    rings.append(ring)
    return rings
