"""Second-wave features: Eurostat decoding, Tankerkönig rules, margins/tax arithmetic, pass-through model,
digest, and the self-contained research page."""
import json
import re
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest

from oco.analysis import extras
from oco.collectors.base import RunResult, run_collector
from oco.collectors.eurostat import decode_jsonstat
from oco.collectors.tankerkoenig import DEMO_KEY, TankerkoenigCollector
from oco.storage.warehouse import Warehouse, observations


def test_jsonstat_decoding_keeps_missing_as_missing():
    d = {"id": ["partner", "time"], "size": [2, 3],
         "dimension": {"partner": {"category": {"index": {"NO": 0, "SA": 1}}}, "time": {"category": {"index": {"2026-01": 0, "2026-02": 1, "2026-03": 2}}}},
         "value": {"0": 10.0, "1": 11.0, "3": 5.0, "5": 7.0}, "status": {"2": ":"}}
    df = decode_jsonstat(d)
    assert len(df) == 6
    no_mar = df[(df.partner == "NO") & (df.time == "2026-03")].iloc[0]
    assert pd.isna(no_mar["value"]) and no_mar["status"] == ":"  # missing, not zero
    assert df[(df.partner == "SA") & (df.time == "2026-03")]["value"].iloc[0] == 7.0


def test_tankerkoenig_refuses_demo_key_and_missing_panel(ctx_factory, monkeypatch):
    ctx = ctx_factory(lambda r: httpx.Response(200, json={"ok": True, "license": "CC BY 4.0", "prices": {}}))
    monkeypatch.setenv("TANKERKOENIG_API_KEY", DEMO_KEY)
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, TankerkoenigCollector(ctx), wh=wh)
    assert res.status == "unconfigured" and "fake" in res.message
    monkeypatch.setenv("TANKERKOENIG_API_KEY", "11111111-2222-3333-4444-555555555555")
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, TankerkoenigCollector(ctx), wh=wh)
    assert res.status == "unconfigured" and "build-panel" in res.message


def test_tankerkoenig_collects_and_aggregates_only_completed_days(ctx_factory, monkeypatch):
    monkeypatch.setenv("TANKERKOENIG_API_KEY", "11111111-2222-3333-4444-555555555555")

    def h(req):
        ids = dict(httpx.QueryParams(req.url.query))["ids"].split(",")
        return httpx.Response(200, json={"ok": True, "license": "CC BY 4.0 - https://creativecommons.tankerkoenig.de",
                                         "prices": {i: {"status": "open", "diesel": 1.999, "e5": 2.059, "e10": 1.999} for i in ids}})
    ctx = ctx_factory(h)
    col = TankerkoenigCollector(ctx)
    col.panel_path.write_text(json.dumps({"stations": [{"id": f"s{i}", "city": "Berlin" if i < 6 else "Köln", "brand": "X"} for i in range(12)]}))
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, col, wh=wh)
        n = wh.con.execute("SELECT COUNT(*) FROM pump_prices").fetchone()[0]
        # pretend yesterday's readings exist -> aggregated; today's are not
        y = datetime.now(timezone.utc) - timedelta(days=1)
        wh.con.execute("INSERT INTO pump_prices VALUES ('s0','Berlin','X',?,'open',1.9,2.0,1.95,NULL)", [y.replace(hour=10)])
        r2 = RunResult("tankerkoenig")
        col.aggregate_days(wh, r2)
        df = observations(wh.con, "tk.panel.diesel.mean")
        key = wh.get_meta("x")
    assert res.status == "ok" and n == 12 and res.n_requests == 2  # batches of 10
    assert len(df) == 1 and abs(df.iloc[0]["value"] - 1.9) < 1e-9, "only the completed day is an observation"
    for f in ctx.paths.raw.rglob("*.json"):
        assert "11111111-2222-3333-4444-555555555555" not in f.read_text()


def test_tankerkoenig_licence_change_fails_visibly(ctx_factory, monkeypatch):
    monkeypatch.setenv("TANKERKOENIG_API_KEY", "11111111-2222-3333-4444-555555555555")
    ctx = ctx_factory(lambda r: httpx.Response(200, json={"ok": True, "license": "proprietary, paid", "prices": {}}))
    col = TankerkoenigCollector(ctx)
    col.panel_path.write_text(json.dumps({"stations": [{"id": "s1", "city": "Berlin"}]}))
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, col, wh=wh)
    assert res.status == "schema_changed"


def _series(wh, sid, rows, unit="x", freq="daily"):
    wh.upsert_series(sid, source="t", source_key=sid, name=sid, geography="", product="", unit=unit, frequency=freq)
    wh.ingest_observations(sid, rows, raw_sha256=None)


def test_crack_spread_and_tax_arithmetic():
    wh = Warehouse.memory()
    d = date(2026, 9, 28)
    _series(wh, "eia.brent_spot", [{"obs_start": d, "value": 100.0}])
    _series(wh, "eia.ulsd_ny_spot", [{"obs_start": d, "value": 3.0}])
    _series(wh, "oil_bulletin.DE.diesel.with_tax", [{"obs_start": d, "value": 2.38}], freq="weekly")
    _series(wh, "oil_bulletin.DE.diesel.without_tax", [{"obs_start": d, "value": 1.40}], freq="weekly")
    extras.compute_cracks(wh)
    extras.compute_tax_take(wh)
    crack = observations(wh.con, "derived.crack.ulsd_ny").iloc[0]["value"]
    assert abs(crack - (3.0 * 42 - 100.0)) < 1e-9
    vat = observations(wh.con, "derived.tax.DE.diesel.vat").iloc[0]["value"]
    other = observations(wh.con, "derived.tax.DE.diesel.other_taxes").iloc[0]["value"]
    assert abs(vat - 2.38 * 0.19 / 1.19) < 1e-6 and abs(other - (0.98 - 2.38 * 0.19 / 1.19)) < 1e-6


def test_passthrough_recovers_known_asymmetry_and_reports_no_difference_when_symmetric():
    rng = np.random.default_rng(0)
    n = 500
    idx = pd.date_range("2015-01-05", periods=n, freq="W-MON")
    dC = rng.normal(0, 0.02, n)
    up, dn = np.clip(dC, 0, None), np.clip(dC, None, 0)
    asym = 0.8 * up + 0.3 * dn + 0.5 * np.r_[0, dn[:-1]] + rng.normal(0, 0.002, n)
    r = extras.asymmetric_passthrough(pd.Series(np.cumsum(asym) + 1.5, idx), pd.Series(np.cumsum(dC) + 0.5, idx))
    assert r["speed_p_value"] < 0.01 and r["p_value"] > 0.05
    assert abs(r["cum_up"][0] - 0.8) < 0.05 and abs(r["cum_down"][0] - 0.3) < 0.05 and abs(r["cum_down"][2] - 0.8) < 0.05
    sym = 0.6 * dC + rng.normal(0, 0.002, n)
    r2 = extras.asymmetric_passthrough(pd.Series(np.cumsum(sym) + 1.5, idx), pd.Series(np.cumsum(dC) + 0.5, idx))
    assert r2["speed_p_value"] > 0.01 and "no clear difference" in r2["interpretation"]
    assert extras.asymmetric_passthrough(pd.Series([1.0] * 20, idx[:20]), pd.Series([1.0] * 20, idx[:20])) is None


def test_research_page_is_self_contained(tmp_path, paths, state):
    from oco.demo import build  # noqa: F401  (fixtures exercise sections)
    from oco.web.page import build_page
    wh = Warehouse.memory()
    rows = [{"obs_start": date(2025, 1, 1) + timedelta(days=i), "value": 40.0 + (i % 7)} for i in range(500)]
    _series(wh, "portwatch.hormuz.n_tanker", rows)
    _series(wh, "eia.brent_spot", [{"obs_start": date(2026, 1, 1) + timedelta(days=i), "value": 70.0 + i * 0.1} for i in range(200)])
    out = build_page(wh.con, state, tmp_path / "p.html")
    html = out.read_text()
    assert not re.search(r'<script[^>]+src=|<link[^>]+href=["\']https?://|url\(["\']?https?://|@import', html), "no external resources"
    assert "lineChart" in html and '"sections"' in html
