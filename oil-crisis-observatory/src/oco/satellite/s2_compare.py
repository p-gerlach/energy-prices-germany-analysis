"""Sentinel-2 L2A before/after review for a VERIFIED facility — local processing of downloaded SAFE zips only.

Products:
  * true colour (B04,B03,B02) at native 10 m
  * false colour SWIR (B12,B11,B8A) at native 20 m — never upsampled for display detail
  * quality from the SCL band at facility level (cloud, shadow, cirrus, no-data); an obscured facility is
    UNOBSERVABLE, not undamaged. Smoke may be classed as cloud: the unmasked true-colour preview is kept.
  * change layer: dNBR = median(NBR_pre scenes) − NBR_post, NBR=(B8A−B12)/(B8A+B12), only where usable in all
    scenes, after a radiometric comparability check on surrounding pixels. Labelled "candidate visible change"
    until reviewed and corroborated. No generative enhancement of any kind.
Comparisons are refused (valid=False with reasons) for: different tiles/grids, facility outside footprint,
insufficient usable pixels, radiometric mismatch.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

USABLE_SCL = {2, 4, 5, 6, 7}  # dark area, vegetation, bare, water, unclassified
SCL_LABELS = {0: "no data", 1: "saturated/defective", 3: "cloud shadow", 8: "cloud (medium)", 9: "cloud (high)",
              10: "thin cirrus", 11: "snow/ice"}
DNBR_CANDIDATE = 0.27  # USGS "moderate-low" burn severity lower bound; a screening threshold only


@dataclass
class Scene:
    product: str
    zip_path: Path
    bands: dict
    offset: float
    quant: float
    sensing: str
    tile: str


@dataclass
class SceneSubset:
    scene: Scene
    rgb: np.ndarray            # 3 x H x W reflectance at 10 m
    swir: np.ndarray           # 3 x h x w reflectance at 20 m (B12, B11, B8A)
    scl20: np.ndarray
    fac_mask10: np.ndarray     # True inside facility
    fac_mask20: np.ndarray
    transform10: object
    transform20: object
    crs: object
    stats: dict = field(default_factory=dict)


def open_scene(zip_path: Path) -> Scene:
    zf = zipfile.ZipFile(zip_path)
    names = zf.namelist()
    want = {"B02": r"_B02_10m\.jp2$", "B03": r"_B03_10m\.jp2$", "B04": r"_B04_10m\.jp2$", "B8A": r"_B8A_20m\.jp2$",
            "B11": r"_B11_20m\.jp2$", "B12": r"_B12_20m\.jp2$", "SCL": r"_SCL_20m\.jp2$"}
    bands = {}
    for b, pat in want.items():
        m = [n for n in names if re.search(pat, n)]
        if not m:
            raise ValueError(f"{zip_path.name}: band {b} not found — not an L2A SAFE product?")
        bands[b] = f"/vsizip/{zip_path}/{m[0]}"
    offset, quant = 0.0, 10000.0
    mtd = [n for n in names if n.endswith("MTD_MSIL2A.xml")]
    sensing, tile = "", ""
    if mtd:
        root = ET.fromstring(zf.read(mtd[0]))
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag == "BOA_ADD_OFFSET" and el.get("band_id") in ("1", "2", "3"):
                offset = float(el.text)
            elif tag == "BOA_QUANTIFICATION_VALUE":
                quant = float(el.text)
            elif tag == "PRODUCT_START_TIME":
                sensing = el.text
    m = re.search(r"_T(\d{2}[A-Z]{3})_", zip_path.name)
    tile = m.group(1) if m else ""
    return Scene(zip_path.stem, zip_path, bands, offset, quant, sensing, tile)


def _read(path: str, bounds_ll, geom_ll):
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_bounds, transform_geom
    from rasterio.windows import from_bounds

    with rasterio.open(path) as ds:
        b = transform_bounds("EPSG:4326", ds.crs, *bounds_ll, densify_pts=21)
        win = from_bounds(*b, transform=ds.transform).round_offsets().round_lengths()
        arr = ds.read(1, window=win, boundless=True, fill_value=0)
        tr = ds.window_transform(win)
        g = transform_geom("EPSG:4326", ds.crs, geom_ll)
        inside = ~geometry_mask([g], out_shape=arr.shape, transform=tr)
        full_inside = (win.col_off >= 0 and win.row_off >= 0 and win.col_off + win.width <= ds.width
                       and win.row_off + win.height <= ds.height)
        return arr, tr, ds.crs, inside, full_inside


def subset(scene: Scene, facility: dict, buffer_km: float = 1.5) -> SceneSubset:
    from .. import geo

    bb = geo.buffer_bbox(geo.bbox(facility["geometry"]), buffer_km)
    refl = lambda a: (a.astype("float32") + scene.offset) / scene.quant  # noqa: E731
    r, tr10, crs, m10, ok10 = _read(scene.bands["B04"], bb, facility["geometry"])
    g, *_ = _read(scene.bands["B03"], bb, facility["geometry"])
    b, *_ = _read(scene.bands["B02"], bb, facility["geometry"])
    s12, tr20, _, m20, ok20 = _read(scene.bands["B12"], bb, facility["geometry"])
    s11, *_ = _read(scene.bands["B11"], bb, facility["geometry"])
    s8a, *_ = _read(scene.bands["B8A"], bb, facility["geometry"])
    scl, *_ = _read(scene.bands["SCL"], bb, facility["geometry"])
    nodata = (r == 0) & (g == 0) & (b == 0)
    sub = SceneSubset(scene, np.stack([refl(r), refl(g), refl(b)]), np.stack([refl(s12), refl(s11), refl(s8a)]),
                      scl, m10, m20, tr10, tr20, crs)
    fac = scl[m20]
    usable = np.isin(fac, list(USABLE_SCL))
    sub.stats = {
        "product": scene.product, "sensing": scene.sensing, "tile": scene.tile, "crs": str(crs),
        "facility_fully_in_raster": bool(ok10 and ok20),
        "facility_pixels_20m": int(m20.sum()), "usable_fraction_at_facility": float(usable.mean()) if fac.size else 0.0,
        "scl_classes_at_facility": {SCL_LABELS.get(int(k), str(int(k))): int(v) for k, v in zip(*np.unique(fac, return_counts=True))
                                    if int(k) not in USABLE_SCL},
        "nodata_fraction_10m_at_facility": float(nodata[m10].mean()) if m10.any() else 1.0,
        "boa_add_offset": scene.offset,
    }
    return sub


def nbr(sub: SceneSubset) -> np.ndarray:
    b12, _, b8a = sub.swir
    with np.errstate(divide="ignore", invalid="ignore"):
        v = (b8a - b12) / (b8a + b12)
    usable = np.isin(sub.scl20, list(USABLE_SCL))
    return np.where(usable, v, np.nan)


def radiometric_check(pre: SceneSubset, post: SceneSubset, lo=0.8, hi=1.25) -> tuple[bool, dict]:
    ring = ~pre.fac_mask20 & ~post.fac_mask20
    u = np.isin(pre.scl20, list(USABLE_SCL)) & np.isin(post.scl20, list(USABLE_SCL)) & ring
    out = {"n_pixels": int(u.sum())}
    if u.sum() < 200:
        return False, {**out, "reason": "too few clear surrounding pixels for radiometric comparison"}
    ok = True
    for i, name in ((2, "B8A"), (1, "B11")):
        a, b = pre.swir[i][u], post.swir[i][u]
        ratio = float(np.median(b) / np.median(a)) if np.median(a) > 0 else float("nan")
        out[f"median_ratio_{name}"] = ratio
        if not (lo <= ratio <= hi):
            ok = False
    if not ok:
        out["reason"] = f"surrounding-area median reflectance ratio outside [{lo}, {hi}] — illumination/atmosphere/processing differ"
    return ok, out


def compare(pre_zips: list[Path], post_zip: Path, facility: dict, out_dir: Path, min_usable: float = 0.7) -> dict:
    """Validate and render. Returns summary dict with valid flag, reasons and output files."""
    pres = [subset(open_scene(p), facility) for p in pre_zips]
    post = subset(open_scene(post_zip), facility)
    reasons = []
    if not pres:
        reasons.append("no pre-event scene supplied")
    shapes = {p.swir.shape for p in pres} | {post.swir.shape}
    crss = {str(p.crs) for p in pres} | {str(post.crs)}
    tiles = {p.scene.tile for p in pres} | {post.scene.tile}
    if len(crss) > 1 or len(shapes) > 1 or len(tiles) > 1:
        reasons.append(f"scenes not on one grid (tiles {sorted(tiles)}, CRS {sorted(crss)}) — no co-registration attempted")
    for s in pres + [post]:
        if not s.stats["facility_fully_in_raster"] or s.stats["nodata_fraction_10m_at_facility"] > 0.01:
            reasons.append(f"{s.scene.product}: facility not fully covered by valid pixels (non-overlapping footprint)")
        if s.stats["usable_fraction_at_facility"] < min_usable:
            reasons.append(f"{s.scene.product}: only {s.stats['usable_fraction_at_facility']:.0%} of facility usable "
                           f"(cloud/shadow/cirrus {s.stats['scl_classes_at_facility']}) — facility unobservable, not undamaged")
    radiometry = []
    if not reasons:
        for p in pres:
            ok, info = radiometric_check(p, post)
            radiometry.append(info)
            if not ok:
                reasons.append(f"{p.scene.product}: {info.get('reason')}")
    valid = not reasons
    change = None
    if valid:
        pre_nbr = np.nanmedian(np.stack([nbr(p) for p in pres]), axis=0)
        d = pre_nbr - nbr(post)
        inside = post.fac_mask20 & ~np.isnan(d)
        cand = inside & (d > DNBR_CANDIDATE)
        change = {"dnbr": d, "candidate_pixels": int(cand.sum()), "evaluated_pixels": int(inside.sum()),
                  "candidate_area_m2": int(cand.sum()) * 400, "threshold": DNBR_CANDIDATE,
                  "label": "candidate visible change (unreviewed) — not a damage assessment"}
    out_dir.mkdir(parents=True, exist_ok=True)
    png = render(pres[0] if pres else post, post, change, valid, reasons, facility, out_dir)
    summary = {"valid": valid, "reasons": reasons, "pre": [p.stats for p in pres], "post": post.stats,
               "radiometry": radiometry, "png": str(png),
               "change": {k: v for k, v in (change or {}).items() if k != "dnbr"} or None,
               "display_note": "Pre panel shows ONE dated scene (not a mosaic); the change layer uses the median of all pre scenes.",
               "attribution": "Contains modified Copernicus Sentinel data"}
    (out_dir / "comparison.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def _stretch(arrs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    stack = np.concatenate([a.reshape(a.shape[0], -1) for a in arrs], axis=1)
    lo = np.nanpercentile(stack, 2, axis=1)
    hi = np.nanpercentile(stack, 98, axis=1)
    return lo, hi


def _img(a, lo, hi):
    x = (a - lo[:, None, None]) / np.maximum(hi - lo, 1e-6)[:, None, None]
    return np.clip(np.moveaxis(x, 0, -1), 0, 1)


def _scalebar(ax, px_m: float, shape, km: float = 1.0):
    n = km * 1000 / px_m
    y = shape[0] * 0.93
    x0 = shape[1] * 0.05
    ax.plot([x0, x0 + n], [y, y], color="white", lw=4)
    ax.plot([x0, x0 + n], [y, y], color="black", lw=1.5)
    ax.text(x0, y - shape[0] * 0.03, f"{km:g} km", color="white", fontsize=11, weight="bold")


def render(pre: SceneSubset, post: SceneSubset, change, valid: bool, reasons: list[str], facility: dict, out_dir: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lo, hi = _stretch([pre.rgb, post.rgb])          # SAME stretch for both dates
    slo, shi = _stretch([pre.swir, post.swir])
    fig, ax = plt.subplots(2, 3, figsize=(16, 9), dpi=120)
    for a in ax.ravel():
        a.set_xticks([]), a.set_yticks([])
    for col, s, lab in ((0, pre, "BEFORE"), (1, post, "AFTER")):
        ax[0, col].imshow(_img(s.rgb, lo, hi))
        ax[0, col].contour(s.fac_mask10, levels=[0.5], colors="#eda100", linewidths=1.5)
        ax[0, col].set_title(f"{lab} · TRUE COLOUR B4/B3/B2 (10 m)\n{s.scene.sensing[:19]} UTC · {s.scene.product[:38]}", fontsize=10)
        _scalebar(ax[0, col], 10, s.rgb.shape[1:])
        ax[1, col].imshow(_img(s.swir, slo, shi), interpolation="nearest")
        ax[1, col].contour(s.fac_mask20, levels=[0.5], colors="#eda100", linewidths=1.5)
        bad = ~np.isin(s.scl20, list(USABLE_SCL))
        if bad.any():
            ax[1, col].contour(bad, levels=[0.5], colors="#e87ba4", linewidths=0.8)
        ax[1, col].set_title(f"{lab} · FALSE COLOUR SWIR B12/B11/B8A (native 20 m)\nusable at facility "
                             f"{s.stats['usable_fraction_at_facility']:.0%} (pink = SCL cloud/shadow/cirrus)", fontsize=10)
        _scalebar(ax[1, col], 20, s.swir.shape[1:])
    if valid and change:
        im = ax[0, 2].imshow(change["dnbr"], cmap="RdBu_r", vmin=-0.6, vmax=0.6, interpolation="nearest")
        ax[0, 2].contour(post.fac_mask20, levels=[0.5], colors="#0b0b0b", linewidths=1)
        ax[0, 2].set_title("dNBR (median pre − post), 20 m\nred = candidate change (UNREVIEWED)", fontsize=10)
        fig.colorbar(im, ax=ax[0, 2], fraction=0.046)
        txt = (f"VALID comparison (automated checks passed)\ncandidate pixels {change['candidate_pixels']} of "
               f"{change['evaluated_pixels']} evaluated\n(~{change['candidate_area_m2']/1e4:.1f} ha, dNBR>{DNBR_CANDIDATE})\n\n"
               "Visible change ≠ confirmed damage.\nCorroborate with reporting & other imagery.")
    else:
        ax[0, 2].axis("off")
        txt = "NOT A VALID COMPARISON\n\n" + "\n".join(f"• {r[:90]}" for r in reasons[:6])
    ax[1, 2].axis("off")
    ax[1, 2].text(0, 1, txt, va="top", fontsize=11, wrap=True)
    fig.suptitle(f"{facility['properties']['name']} — Sentinel-2 before/after review", x=0.02, ha="left", fontsize=16, weight="bold")
    fig.text(0.02, 0.01, "Contains modified Copernicus Sentinel data. Same stretch both dates; no enhancement/upsampling detail added. "
             "Facility outline (amber) from user-verified boundary.", fontsize=9, color="#52514e")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    p = out_dir / "before_after.png"
    fig.savefig(p)
    plt.close(fig)
    return p
