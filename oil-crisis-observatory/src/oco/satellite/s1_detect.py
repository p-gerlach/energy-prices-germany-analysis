"""EXPERIMENTAL Sentinel-1 GRD candidate-vessel detection — local processing of a downloaded SAFE zip.

Workflow (Python equivalent of the SNAP chain; validate against SNAP's Object Detection operator before
publishing results):
  1. read measurement GeoTIFF (VV preferred; VH optional) for the AOI window only
  2. thermal noise removal with the product's range noise LUT (IPF >= 2.9 noiseRangeVector); azimuth noise
     scaling is NOT applied (documented limitation)
  3. radiometric calibration to sigma0 with the calibration LUT: sigma0 = (DN^2 - noise) / A_sigma^2
  4. geolocation from the product's tie-point GCPs (ellipsoid; adequate over open water — no terrain
     correction needed at sea level; NOT valid for land targets)
  5. land mask = Natural Earth land + configurable buffer; fixed offshore structures masked from a user list
  6. two-parameter CFAR on calibrated linear sigma0, water pixels only, with guard window; NO smoothing that
     would erase compact bright targets
  7. connected components -> candidate detections with geometry and quality indicators, deduplicated

Output = candidate vessel COUNT in ONE SNAPSHOT for the AOI (+ valid-water coverage). It is not a daily
transit count, not vessel identity/type/cargo, and detections days apart are never linked into tracks.
Wind/sea state is not modelled: rough seas raise clutter and false positives. Image pixel spacing (10 m GRDH)
is not physical resolution (~20 x 22 m IW).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

METHOD_VERSION = "s1_cfar/v2 (calib+range-noise, gamma-CFAR PFA=1e-6 local ENL, bg=41 guard=11, NE land+1km)"
SNAPSHOT_UNIT = "candidate vessels present in one radar snapshot (count)"


@dataclass
class CFARParams:
    background: int = 41
    guard: int = 11
    pfa: float = 1e-6          # probability of false alarm per pixel under the local gamma clutter model
    min_contrast_db: float = 6.0
    min_px: int = 3
    max_px: int = 3000
    land_buffer_km: float = 1.0
    min_valid_water_fraction: float = 0.8
    dedupe_m: float = 100.0
    fixed_structure_m: float = 250.0


def _xml(zf, pattern):
    names = [n for n in zf.namelist() if re.search(pattern, n)]
    return ET.fromstring(zf.read(names[0])) if names else None


def _vectors(root, list_tag, vec_tag, lut_tag):
    lines, pix, vals = [], [], []
    for v in root.iter():
        if v.tag.split("}")[-1] != vec_tag:
            continue
        d = {c.tag.split("}")[-1]: c.text for c in v}
        if lut_tag not in d or "pixel" not in d:
            continue
        lines.append(int(d["line"]))
        pix.append(np.array(d["pixel"].split(), dtype=float))
        vals.append(np.array(d[lut_tag].split(), dtype=float))
    return lines, pix, vals


def _lut_grid(lines, pix, vals, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Bilinear: interpolate each vector along pixels, then along lines."""
    per_line = np.stack([np.interp(cols, p, v) for p, v in zip(pix, vals)])  # n_vec x W
    out = np.empty((rows.size, cols.size), dtype="float32")
    for j in range(cols.size):
        out[:, j] = np.interp(rows, lines, per_line[:, j])
    return out


def open_grd(zip_path: Path, pol: str = "vv") -> dict:
    zf = zipfile.ZipFile(zip_path)
    names = zf.namelist()
    meas = [n for n in names if re.search(rf"measurement/.*-{pol}-.*\.tiff?$", n)]
    if not meas:
        raise ValueError(f"{zip_path.name}: no {pol.upper()} measurement — is this a GRD product with that polarisation?")
    cal = _xml(zf, rf"annotation/calibration/calibration-.*-{pol}-.*\.xml$")
    noise = _xml(zf, rf"annotation/calibration/noise-.*-{pol}-.*\.xml$")
    if cal is None:
        raise ValueError("calibration annotation missing")
    return {"zip": zip_path, "tiff": f"/vsizip/{zip_path}/{meas[0]}", "cal": cal, "noise": noise, "pol": pol}


def _gcp_interpolators(ds):
    from scipy.interpolate import LinearNDInterpolator

    gcps, _ = ds.gcps
    if not gcps:
        raise ValueError("no GCPs in measurement file")
    cr = np.array([[g.col, g.row] for g in gcps])
    ll = np.array([[g.x, g.y] for g in gcps])
    to_ll = LinearNDInterpolator(cr, ll)
    to_cr = LinearNDInterpolator(ll, cr)
    return to_ll, to_cr


def calibrated_window(grd: dict, aoi_geom: dict):
    import rasterio
    from rasterio.windows import Window

    from .. import geo

    with rasterio.open(grd["tiff"]) as ds:
        to_ll, to_cr = _gcp_interpolators(ds)
        w, s, e, n = geo.bbox(aoi_geom)
        corners = to_cr(np.array([[w, s], [w, n], [e, s], [e, n]]))
        if np.isnan(corners).any():
            # AOI partly outside the swath: clip to image and let coverage fraction report it
            corners = np.nan_to_num(corners, nan=-1)
        c0, r0 = np.clip(corners.min(axis=0), 0, [ds.width - 1, ds.height - 1]).astype(int)
        c1, r1 = np.clip(corners.max(axis=0), 0, [ds.width - 1, ds.height - 1]).astype(int)
        if c1 - c0 < 10 or r1 - r0 < 10:
            raise ValueError("AOI does not intersect this scene's swath")
        win = Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
        dn = ds.read(1, window=win).astype("float32")
    rows = np.arange(r0, r1 + 1, dtype=float)
    cols = np.arange(c0, c1 + 1, dtype=float)
    A = _lut_grid(*_vectors(grd["cal"], "calibrationVectorList", "calibrationVector", "sigmaNought"), rows, cols)
    noise_note = "range noise LUT applied"
    N = np.zeros_like(A)
    if grd["noise"] is not None:
        lines, pix, vals = _vectors(grd["noise"], "noiseRangeVectorList", "noiseRangeVector", "noiseRangeLut")
        if not lines:
            lines, pix, vals = _vectors(grd["noise"], "noiseVectorList", "noiseVector", "noiseLut")  # IPF < 2.9
            noise_note = "legacy noise LUT applied"
        if lines:
            N = _lut_grid(lines, pix, vals, rows, cols)
        else:
            noise_note = "noise LUT not found — NOT noise-corrected"
    else:
        noise_note = "noise annotation missing — NOT noise-corrected"
    nodata = dn <= 0
    sigma0 = np.maximum(dn ** 2 - N, 0) / np.maximum(A, 1e-6) ** 2
    sigma0[nodata] = np.nan
    return {"sigma0": sigma0, "rows": rows, "cols": cols, "to_ll": to_ll, "nodata": nodata, "noise_note": noise_note}


def lonlat_grid(to_ll, rows, cols, step: int = 10):
    from scipy.ndimage import zoom

    rr = rows[::step]
    cc = cols[::step]
    C, R = np.meshgrid(cc, rr)
    ll = to_ll(np.column_stack([C.ravel(), R.ravel()])).reshape(R.shape + (2,))
    lon = zoom(ll[..., 0], (rows.size / rr.size, cols.size / cc.size), order=1)[: rows.size, : cols.size]
    lat = zoom(ll[..., 1], (rows.size / rr.size, cols.size / cc.size), order=1)[: rows.size, : cols.size]
    return lon, lat


MASK_STEP = 10


def coarse_masks(to_ll, rows, cols, aoi_poly, land_poly, step: int = MASK_STEP):
    import shapely

    rr, cc = rows[::step], cols[::step]
    C, R = np.meshgrid(cc, rr)
    ll = to_ll(np.column_stack([C.ravel(), R.ravel()])).reshape(R.shape + (2,))
    lon, lat = ll[..., 0], ll[..., 1]
    ok = ~np.isnan(lon)
    in_aoi_c = np.zeros(lon.shape, bool)
    in_aoi_c[ok] = shapely.contains_xy(aoi_poly, lon[ok], lat[ok])
    land_c = np.zeros(lon.shape, bool)
    if land_poly is not None:
        sel = ok & in_aoi_c  # only test points inside the AOI
        land_c[sel] = shapely.contains_xy(land_poly, lon[sel], lat[sel])

    def expand(m):
        return np.repeat(np.repeat(m, step, axis=0), step, axis=1)[: rows.size, : cols.size]
    return expand(in_aoi_c), expand(land_c)


def cfar(x: np.ndarray, water: np.ndarray, p: CFARParams) -> np.ndarray:
    """Constant-false-alarm-rate detector on linear sigma0, water pixels only, with a guard window.

    Multilook SAR intensity clutter is modelled locally as Gamma(L, mean/L) with the equivalent number of looks
    L = mean^2 / var estimated from the background ring (clipped to [1, 50]). The threshold is the (1 - PFA)
    quantile of that distribution, so the false-alarm rate stays ~PFA regardless of the local clutter level.
    No smoothing is applied, so compact bright targets are preserved.
    """
    from scipy.ndimage import uniform_filter
    from scipy.stats import gamma

    xv = np.where(water, np.nan_to_num(x), 0.0).astype("float32")
    wv = water.astype("float32")
    B, G = p.background, p.guard

    def box_sum(a, size):
        return uniform_filter(a, size=size, mode="constant") * float(size * size)
    s1 = box_sum(xv, B) - box_sum(xv, G)
    n = box_sum(wv, B) - box_sum(wv, G)
    s2 = box_sum(xv * xv, B) - box_sum(xv * xv, G)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s1 / n
        del s1
        var = np.maximum(s2 / n - mean * mean, 1e-30)
        del s2
        enl = np.clip(mean * mean / var, 1.0, 50.0)
        del var
    # threshold factor depends only on ENL: table on a 0.05 grid, looked up by integer index (no per-pixel Python)
    steps = np.arange(20, 1001)  # ENL 1.00 .. 50.00 in 0.05 steps
    table = (gamma.isf(p.pfa, a=steps * 0.05) / (steps * 0.05)).astype("float32")
    idx = np.clip(np.rint(np.nan_to_num(enl, nan=1.0) / 0.05).astype(np.int32), 20, 1000) - 20
    del enl
    factor = table[idx]
    del idx
    with np.errstate(invalid="ignore", divide="ignore"):
        contrast_ok = xv >= mean * np.float32(10 ** (p.min_contrast_db / 10))
    enough = n >= 0.5 * (B * B - G * G)
    return water & enough & (xv > factor * mean) & contrast_ok & (mean > 0)


def detect(grd_zip: Path, aoi: dict, land_geom, fixed_points: list[tuple[float, float]] = (), params: CFARParams | None = None,
           pol: str = "vv") -> dict:
    import shapely
    from scipy.ndimage import label, find_objects

    from .. import geo

    p = params or CFARParams()
    grd = open_grd(grd_zip, pol)
    w = calibrated_window(grd, aoi["geometry"])
    # Masks are evaluated on a coarse tie grid (every MASK_STEP pixels, ~100 m) and expanded by nearest neighbour:
    # exact point-in-polygon tests for ~1e8 pixels against detailed coastlines take far too long, and the
    # coastal buffer (default 1 km) is much larger than the mask granularity.
    in_aoi, land = coarse_masks(w["to_ll"], w["rows"], w["cols"], shapely.geometry.shape(aoi["geometry"]),
                                land_geom.buffer(p.land_buffer_km / 111.0) if land_geom is not None else None)
    water_aoi = in_aoi & ~land
    valid = water_aoi & ~np.isnan(w["sigma0"])
    valid_fraction = float(valid.sum() / max(water_aoi.sum(), 1))
    det = cfar(w["sigma0"], valid, p)
    lab, n = label(det)
    sig_db = 10 * np.log10(np.maximum(np.nan_to_num(w["sigma0"]), 1e-10)).astype("float32")
    background_db = float(np.nanmedian(sig_db[valid][::50])) if valid.any() else None  # once, subsampled
    cands = []
    for i, sl in enumerate(find_objects(lab), start=1):
        if sl is None:
            continue
        m = lab[sl] == i
        area = int(m.sum())
        rr, cc = np.nonzero(m)
        r0, c0 = sl[0].start, sl[1].start
        rc, ccen = r0 + rr.mean(), c0 + cc.mean()
        ll = w["to_ll"](np.array([[w["cols"][0] + ccen, w["rows"][0] + rc]]))[0]
        q = {"area_px": area, "pixel_spacing_m": 10, "physical_resolution_note": "IW GRDH ~20x22 m",
             "touches_window_edge": bool(sl[0].start == 0 or sl[1].start == 0 or sl[0].stop == lab.shape[0] or sl[1].stop == lab.shape[1])}
        status = "unreviewed"
        if area < p.min_px:
            status = "rejected_auto:too_small"
        elif area > p.max_px:
            status = "rejected_auto:too_large (structure/artefact?)"
        elif any(geo.haversine_km(ll[0], ll[1], fx, fy) * 1000 <= p.fixed_structure_m for fx, fy in fixed_points):
            status = "masked:fixed_structure"
        peak = float(np.nanmax(sig_db[sl][m]))
        cands.append({"lon": float(ll[0]), "lat": float(ll[1]), "area_px": area, "peak_db": peak,
                      "contrast_db": float(peak - background_db) if background_db is not None else None,
                      "quality": q, "review_status": status})
    # dedupe (overlapping components / split targets)
    kept = []
    for c in sorted(cands, key=lambda c: -c["peak_db"]):
        if c["review_status"] != "unreviewed":
            kept.append(c)
            continue
        if any(k["review_status"] == "unreviewed" and geo.haversine_km(c["lon"], c["lat"], k["lon"], k["lat"]) * 1000 < p.dedupe_m for k in kept):
            continue
        kept.append(c)
    count = sum(1 for c in kept if c["review_status"] == "unreviewed")
    quality = {"valid_water_fraction": valid_fraction, "noise": w["noise_note"], "polarisation": pol.upper(),
               "wind_sea_state": "not assessed — rough seas increase false positives",
               "land_mask": f"Natural Earth 10m + {p.land_buffer_km} km buffer (coarse near coasts)",
               "comparable": valid_fraction >= p.min_valid_water_fraction}
    return {"count": count if quality["comparable"] else None, "raw_count": count, "candidates": kept,
            "quality": quality, "unit": SNAPSHOT_UNIT, "method_version": METHOD_VERSION}


def precision_report(con) -> dict:
    df = con.execute("SELECT review_status FROM candidate_detections").df()
    if df.empty:
        return {"reviewed": 0, "note": "no detections"}
    tp = int((df["review_status"] == "confirmed").sum())
    fp = int((df["review_status"] == "rejected").sum())
    return {"reviewed": tp + fp, "confirmed": tp, "rejected": fp,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": "not computable without independent ground truth (e.g. simultaneous AIS)",
            "status": "EXPERIMENTAL until a reviewed sample exists" if tp + fp < 50 else "reviewed sample available"}


def fixed_structures() -> list[tuple[float, float]]:
    from ..settings import CONFIG_DIR

    p = CONFIG_DIR / "fixed_structures.geojson"
    if not p.exists():
        return []
    fc = json.loads(p.read_text())
    return [tuple(f["geometry"]["coordinates"][:2]) for f in fc.get("features", []) if f.get("geometry", {}) and f["geometry"]["type"] == "Point"]
