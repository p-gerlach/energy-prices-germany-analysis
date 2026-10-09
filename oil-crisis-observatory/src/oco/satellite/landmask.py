"""Land masking from Natural Earth 10m land polygons (public domain), fetched once through the guarded client.

Natural Earth is coarse (~1:10M): adequate for open-water screening, NOT for precise shoreline masking near
ports/anchorages. A configurable buffer (default 1 km) reduces shoreline false positives; users can replace the
file with a more detailed coastline that has passed the same zero-charge review.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from ..collectors.base import Context

URL = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip"


def land_path(ctx: Context) -> Path:
    return ctx.paths.satellite / "landmask" / "ne_10m_land.zip"


def ensure_land(ctx: Context) -> Path:
    p = land_path(ctx)
    if p.exists():
        return p
    with ctx.client("naturalearth") as c:
        c.get(URL, stream_to=p)
    return p


def load_land_geoms(zip_path: Path, bbox: tuple[float, float, float, float] | None = None):
    """Return a shapely geometry (union of land polygons intersecting bbox). Needs the satellite extra."""
    import shapely
    from shapely.geometry import box

    geoms = _read_shp_from_zip(zip_path)
    if bbox:
        b = box(*bbox)
        # clip continent-sized polygons to the study box: keeps point-in-polygon tests fast
        geoms = [g.intersection(b) for g in geoms if g.intersects(b)]
        geoms = [g for g in geoms if not g.is_empty]
    return shapely.union_all(geoms) if geoms else None


def _read_shp_from_zip(zip_path: Path):
    """Minimal pure-Python ESRI shapefile polygon reader (avoids a fiona/GDAL-vector dependency)."""
    import struct

    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    zf = zipfile.ZipFile(zip_path)
    shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
    data = zf.read(shp_name)
    pos = 100
    polys = []
    while pos + 8 <= len(data):
        _, length = struct.unpack(">ii", data[pos:pos + 8])
        content = data[pos + 8: pos + 8 + length * 2]
        pos += 8 + length * 2
        if len(content) < 44:
            continue
        stype = struct.unpack("<i", content[:4])[0]
        if stype not in (5, 15, 25):
            continue
        nparts, npoints = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{nparts}i", content[44:44 + 4 * nparts]))
        off = 44 + 4 * nparts
        pts = struct.unpack(f"<{2 * npoints}d", content[off: off + 16 * npoints])
        xy = list(zip(pts[0::2], pts[1::2]))
        parts.append(npoints)
        rings = [xy[parts[i]:parts[i + 1]] for i in range(nparts)]
        shell, holes = rings[0], []
        for r in rings[1:]:
            # shapefile: outer rings clockwise, holes counter-clockwise
            area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]))
            if area > 0:
                holes.append(r)
            else:
                polys.append(Polygon(shell, holes).buffer(0))
                shell, holes = r, []
        polys.append(Polygon(shell, holes).buffer(0))
    return [p for p in polys if not p.is_empty]


def save_geojson(geom, path: Path):
    from shapely.geometry import mapping

    path.write_text(json.dumps({"type": "Feature", "properties": {"source": "Natural Earth"}, "geometry": mapping(geom)}))
