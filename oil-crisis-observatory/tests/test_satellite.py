"""Acceptance: satellite comparison validity, local processing, thermal classification, coverage, downloads.
Synthetic rasters exercise the LOGIC only; real-imagery accuracy is untested until real products are processed."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from oco.collectors.base import NotConfigured, RunResult
from oco.collectors.cdse import CDSECatalogueCollector
from oco.satellite import coverage, thermal
from oco.satellite.s1_detect import CFARParams, _lut_grid, cfar
from oco.storage.warehouse import Warehouse

pytest.importorskip("rasterio")
import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

LON, LAT = 56.30, 26.50
FAC = {"type": "Feature", "properties": {"id": "test_refinery", "name": "Synthetic test refinery", "verified": True,
                                         "routine_flare_points": [[56.301, 26.501]]},
       "geometry": {"type": "Polygon", "coordinates": [[[56.29, 26.49], [56.31, 26.49], [56.31, 26.51], [56.29, 26.51], [56.29, 26.49]]]}}


def _tif(arr, res, x0, y0):
    mem = io.BytesIO()
    with rasterio.MemoryFile() as mf:
        with mf.open(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="uint16",
                     crs="EPSG:32640", transform=from_origin(x0, y0, res, res)) as ds:
            ds.write(arr.astype("uint16"), 1)
        mem.write(mf.read())
    return mem.getvalue()


def make_s2_zip(tmp: Path, name: str, tile="40RCN", cloud_over_facility=False, burn=False, scale=1.0) -> Path:
    t = Transformer.from_crs("EPSG:4326", "EPSG:32640", always_xy=True)
    cx, cy = t.transform(LON, LAT)
    x0, y0 = cx - 3000, cy + 3000
    n10, n20 = 600, 300
    refl = lambda r: np.full((n10, n10), r * scale * 10000 + 1000)  # noqa: E731  (BOA_ADD_OFFSET -1000)
    b8a = np.full((n20, n20), 0.30 * scale * 10000 + 1000)
    b12 = np.full((n20, n20), 0.10 * scale * 10000 + 1000)
    b11 = np.full((n20, n20), 0.15 * scale * 10000 + 1000)
    scl = np.full((n20, n20), 4)
    mid = slice(140, 160)
    if burn:
        b8a[mid, mid] = 0.12 * 10000 + 1000
        b12[mid, mid] = 0.30 * 10000 + 1000
    if cloud_over_facility:
        scl[120:180, 120:180] = 9
    safe = f"{name}.SAFE"
    stamp = "20260901T070000"
    p = tmp / f"{name}.zip"
    with zipfile.ZipFile(p, "w") as z:
        for b, arr, res in (("B02", refl(0.05), 10), ("B03", refl(0.07), 10), ("B04", refl(0.08), 10)):
            z.writestr(f"{safe}/GRANULE/L2A_T{tile}/IMG_DATA/R10m/T{tile}_{stamp}_{b}_10m.jp2", _tif(arr, res, x0, y0))
        for b, arr in (("B8A", b8a), ("B11", b11), ("B12", b12), ("SCL", scl)):
            z.writestr(f"{safe}/GRANULE/L2A_T{tile}/IMG_DATA/R20m/T{tile}_{stamp}_{b}_20m.jp2", _tif(arr, 20, x0, y0))
        z.writestr(f"{safe}/MTD_MSIL2A.xml", "<root><PRODUCT_START_TIME>2026-09-01T07:00:00Z</PRODUCT_START_TIME>"
                   "<BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>"
                   '<BOA_ADD_OFFSET band_id="1">-1000</BOA_ADD_OFFSET></root>')
    return p.rename(tmp / f"S2X_MSIL2A_{stamp}_N0511_R120_T{tile}_{name}.zip")


def test_s2_valid_comparison_detects_candidate_change(tmp_path):
    from oco.satellite.s2_compare import compare
    pre = make_s2_zip(tmp_path, "pre1")
    post = make_s2_zip(tmp_path, "post", burn=True)
    s = compare([pre], post, FAC, tmp_path / "out")
    assert s["valid"], s["reasons"]
    assert s["change"]["candidate_pixels"] > 0 and "not a damage assessment" in s["change"]["label"]
    assert Path(s["png"]).exists()


def test_s2_cloud_covered_facility_cannot_yield_comparison(tmp_path):
    from oco.satellite.s2_compare import compare
    s = compare([make_s2_zip(tmp_path, "pre1")], make_s2_zip(tmp_path, "post", cloud_over_facility=True, burn=True), FAC, tmp_path / "o")
    assert not s["valid"] and any("unobservable, not undamaged" in r for r in s["reasons"])
    assert s["change"] is None


def test_s2_nonoverlapping_tiles_cannot_yield_comparison(tmp_path):
    from oco.satellite.s2_compare import compare
    s = compare([make_s2_zip(tmp_path, "pre1", tile="40RCN")], make_s2_zip(tmp_path, "post", tile="40RDN"), FAC, tmp_path / "o")
    assert not s["valid"] and any("not on one grid" in r for r in s["reasons"])


def test_s2_radiometric_mismatch_refused(tmp_path):
    from oco.satellite.s2_compare import compare
    s = compare([make_s2_zip(tmp_path, "pre1")], make_s2_zip(tmp_path, "post", scale=1.6), FAC, tmp_path / "o")
    assert not s["valid"] and any("reflectance ratio" in r for r in s["reasons"])


def test_s1_comparability_rules():
    a = {"product_id": "a", "relative_orbit": 130, "orbit_direction": "DESCENDING", "instrument_mode": "IW", "polarisation": "VV&VH",
         "intersection_fraction": 0.98, "product_type": "IW_GRDH_1S"}
    ok, _ = coverage.comparable_s1(a, {**a, "product_id": "b"})
    assert ok
    ok, reasons = coverage.comparable_s1(a, {**a, "product_id": "b", "relative_orbit": 57})
    assert not ok and "relative_orbit" in reasons[0]
    ok, reasons = coverage.comparable_s1(a, {**a, "product_id": "b", "intersection_fraction": 0.4})
    assert not ok


def test_cfar_detects_compact_targets_not_land():
    rng = np.random.default_rng(0)
    x = rng.exponential(0.01, (300, 300)).astype("float32")
    water = np.ones_like(x, dtype=bool)
    water[:, :60] = False  # land strip
    x[:, :60] = 0.5         # bright land must not be detected
    targets = [(100, 150), (200, 220), (50, 250)]
    for r, c in targets:
        x[r:r + 2, c:c + 2] = 1.0
    det = cfar(x, water, CFARParams())
    from scipy.ndimage import label
    lab, n = label(det)
    assert not det[:, :60].any()
    assert all(det[r:r + 2, c:c + 2].any() for r, c in targets)
    assert n <= 6, f"too many false alarms on clean clutter: {n}"


def test_lut_interpolation():
    g = _lut_grid([0, 10], [np.array([0.0, 10.0]), np.array([0.0, 10.0])], [np.array([1.0, 3.0]), np.array([5.0, 7.0])],
                  np.array([0.0, 5.0, 10.0]), np.array([0.0, 5.0]))
    assert np.allclose(g, [[1, 2], [3, 4], [5, 6]])


def test_thermal_routine_flaring_is_not_labelled_attack(monkeypatch):
    monkeypatch.setattr("oco.geo.facilities", lambda verified_only=False: [FAC])
    wh = Warehouse.memory()
    t0 = datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc)
    rows = []
    for d in range(120):  # routine flare at the flare stack nightly
        rows.append((f"f{d}", "VIIRS_SNPP_NRT", "N", "VIIRS", t0 + timedelta(days=d), 26.501, 56.301, "n", "N", 5.0))
    for k in range(12):  # large unusual cluster away from the flare
        rows.append((f"x{k}", "VIIRS_NOAA20_NRT", "1", "VIIRS", t0 + timedelta(days=125, minutes=k % 3), 26.495 + k * 0.0005, 56.295, "h", "N", 80.0))
    for r in rows:
        wh.con.execute("INSERT INTO thermal_detections VALUES (?,?,?,?,?,?,?,?,?,?,1,1,300,290,'2',NULL,now())", list(r))
    out = thermal.build_thermal_events(wh)
    ev = wh.con.execute("SELECT classification, n_detections FROM thermal_events ORDER BY start_at").fetchall()
    labels = {c for c, _ in ev}
    assert "routine_flaring_pattern" in labels
    assert ev[-1][0] == "thermal_anomaly_near_facility" and ev[-1][1] == 12, "simultaneous detections grouped into ONE event"
    assert not any(w in " ".join(labels) for w in ("destroy", "attack", "damage"))


def test_coverage_report_gaps_and_untested_downloads():
    wh = Warehouse.memory()
    base = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)
    for i, gap in enumerate([0, 6, 12, 24, 30]):
        pid = f"p{i}"
        wh.con.execute("INSERT INTO satellite_scenes (product_id, mission, acquisition_start, acquisition_end, published_at, "
                       "relative_orbit, orbit_direction, instrument_mode, polarisation) VALUES (?,?,?,?,?,?,?,?,?)",
                       [pid, "Sentinel-1", base + timedelta(days=gap), base + timedelta(days=gap, minutes=1),
                        base + timedelta(days=gap, hours=5), 130, "DESCENDING", "IW", "VV&VH"])
        wh.con.execute("INSERT INTO scene_aoi VALUES (?,?,?)", [pid, "hormuz_strait", 0.95 if i != 2 else 0.3])
    s = coverage.coverage_summary(wh.con, "hormuz_strait", "Sentinel-1", 0.9, download_tested=False)
    assert s["n_scenes"] == 5 and s["n_scenes_covering_min_fraction"] == 4
    assert s["max_gap_days"] == 18 and s["median_gap_days"] == 6.0
    assert "UNTESTED" in s["asset_access"] and abs(s["median_catalogue_latency_h"] - 5) < 0.1


def test_cdse_catalogue_collector_parses_odata(ctx_factory):
    item = {"Id": "11111111-2222-3333-4444-555555555555", "Name": "S1A_IW_GRDH_1SDV_20260901T020000_x.SAFE",
            "ContentDate": {"Start": "2026-09-01T02:00:00.000Z", "End": "2026-09-01T02:00:25.000Z"},
            "PublicationDate": "2026-09-01T05:10:00.000Z", "ModificationDate": "2026-09-01T05:10:00.000Z", "Online": True,
            "ContentLength": 900000000,
            "Footprint": "geography'SRID=4326;POLYGON ((55.5 25.8, 57.5 25.8, 57.5 27.5, 55.5 27.5, 55.5 25.8))'",
            "Attributes": [{"Name": "orbitDirection", "Value": "DESCENDING"}, {"Name": "relativeOrbitNumber", "Value": 130},
                           {"Name": "polarisationChannels", "Value": "VV&VH"}, {"Name": "operationalMode", "Value": "IW"},
                           {"Name": "productType", "Value": "IW_GRDH_1S"}]}
    ctx = ctx_factory(lambda r: httpx.Response(200, json={"value": [item]}))
    col = CDSECatalogueCollector(ctx)
    with Warehouse.writer(ctx.paths) as wh:
        col.collect(wh, RunResult("cdse"))
        row = wh.con.execute("SELECT relative_orbit, orbit_direction, polarisation FROM satellite_scenes").fetchone()
        frac = wh.con.execute("SELECT intersection_fraction FROM scene_aoi WHERE aoi_id='hormuz_strait'").fetchone()[0]
    assert row == (130, "DESCENDING", "VV&VH") and abs(frac - 1.0) < 1e-6
    ok, detail = col.probe()
    assert "downloads untested" in detail


def test_downloads_need_user_credential_and_respect_budget(ctx_factory, monkeypatch):
    from oco.satellite import download
    ctx = ctx_factory(lambda r: httpx.Response(500))
    with pytest.raises(NotConfigured):
        download.CDSEAuth(ctx)
    monkeypatch.setenv("OCO_DOWNLOAD_BUDGET_GB", "1")
    with pytest.raises(download.BudgetExceeded):
        download.check_budget(ctx, 5 * 10**9)
