"""Minimal geometry helpers without optional dependencies (bbox, point-in-polygon, distances)."""
from __future__ import annotations

import json
import math

from .settings import CONFIG_DIR


def load_geojson(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


def regions() -> list[dict]:
    return load_geojson("regions.geojson")["features"]


def facilities(verified_only: bool = False) -> list[dict]:
    feats = load_geojson("facilities.geojson")["features"]
    if verified_only:
        feats = [f for f in feats if f["properties"].get("verified") and f.get("geometry")]
    return feats


def _coords(geom: dict):
    t = geom["type"]
    if t == "Point":
        yield geom["coordinates"]
    elif t == "Polygon":
        for ring in geom["coordinates"]:
            yield from ring
    elif t == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                yield from ring
    else:
        raise ValueError(f"unsupported geometry {t}")


def bbox(geom: dict) -> tuple[float, float, float, float]:
    xs, ys = zip(*[(c[0], c[1]) for c in _coords(geom)])
    return min(xs), min(ys), max(xs), max(ys)


def buffer_bbox(b, km: float):
    w, s, e, n = b
    dlat = km / 111.32
    dlon = km / (111.32 * max(0.1, math.cos(math.radians((s + n) / 2))))
    return w - dlon, s - dlat, e + dlon, n + dlat


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_in_polygon(lon: float, lat: float, geom: dict) -> bool:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"] if geom["type"] == "MultiPolygon" else []
    for poly in polys:
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def _in_ring(x, y, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def distance_to_geom_km(lon, lat, geom) -> float:
    if geom["type"] == "Point":
        return haversine_km(lon, lat, *geom["coordinates"][:2])
    if point_in_polygon(lon, lat, geom):
        return 0.0
    return min(haversine_km(lon, lat, c[0], c[1]) for c in _coords(geom))


def polygon_wkt(geom: dict) -> str:
    ring = geom["coordinates"][0]
    return "POLYGON((" + ", ".join(f"{c[0]} {c[1]}" for c in ring) + "))"
