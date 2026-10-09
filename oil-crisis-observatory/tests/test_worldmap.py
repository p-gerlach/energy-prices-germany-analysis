"""World shipping map: reference-layer collector stays on approved anonymous routes; map payload is built from
stored observations only (gaps stay gaps); the page stays self-contained."""
import json
import re
from datetime import date, timedelta

import httpx
import pytest

from oco.collectors import portwatch_geo
from oco.collectors.base import run_collector
from oco.policy import PolicyDenied
from oco.storage.warehouse import Warehouse


def _geo_handler(seen):
    def h(req):
        seen.append(str(req.url))
        path = req.url.path
        if "PortWatch_chokepoints_database" in path:
            return httpx.Response(200, json={"features": [
                {"attributes": {"portid": "chokepoint6", "portname": "Strait of Hormuz", "lat": 26.3, "lon": 56.9}},
                {"attributes": {"portid": "chokepoint1", "portname": "Suez Canal", "lat": 30.6, "lon": 32.4}},
                {"attributes": {"portid": "chokepoint4", "portname": "Bab el-Mandeb Strait", "lat": 12.8, "lon": 43.3}},
                {"attributes": {"portid": "chokepoint7", "portname": "Cape of Good Hope", "lat": -34.9, "lon": 20.9}}]})
        if "PortWatch_ports_database" in path:
            return httpx.Response(200, json={"features": [{"attributes": {"portid": "port570", "portname": "Yanbu", "lat": 24.0, "lon": 38.1}}]})
        if "Global_Shipping_Routes" in path:
            return httpx.Response(200, json={"features": [{"geometry": {"paths": [[[170, 10], [-170, 12]], [[30, 30], [40, 20]]]}}]})
        return httpx.Response(404)
    return h


def test_geo_collector_uses_only_approved_anonymous_routes(ctx_factory, monkeypatch):
    seen = []
    ctx = ctx_factory(_geo_handler(seen))
    monkeypatch.setattr(portwatch_geo, "simplified_land", lambda ctx: None)
    # the config lists 28 chokepoints; the stub knows 4, which must fail visibly (fewer than half resolved)
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, portwatch_geo.PortWatchGeoCollector(ctx), wh=wh)
    assert res.status == "schema_changed"
    monkeypatch.setattr(portwatch_geo, "sources_cfg_chokepoints", lambda: [
        {"slug": "hormuz", "names": ["Strait of Hormuz"], "screen": True}, {"slug": "suez", "names": ["Suez Canal"]}])
    with Warehouse.writer(ctx.paths) as wh:
        res = run_collector(ctx, portwatch_geo.PortWatchGeoCollector(ctx), wh=wh)
        cps = json.loads(wh.get_meta("geo.chokepoints"))["items"]
        routes = json.loads(wh.get_meta("geo.routes"))["paths"]
    assert res.status == "ok", res.message
    assert [c["slug"] for c in cps] == ["hormuz", "suez"] and cps[0]["screen"] is True
    assert len(routes) == 2
    assert all("token" not in u and "services9.arcgis.com" in u for u in seen)


def test_other_arcgis_layers_stay_blocked(ctx_factory):
    ctx = ctx_factory(lambda r: httpx.Response(200, json={}))
    with ctx.client("portwatch") as c:
        with pytest.raises(PolicyDenied):
            c.get("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Spillover_Simulator_Maritime_Connections/FeatureServer/0/query")
        with pytest.raises(PolicyDenied):  # reference layers: query only, not other operations
            c.get("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Global_Shipping_Routes/FeatureServer/15/applyEdits")


def _series(wh, sid, rows):
    wh.upsert_series(sid, source="IMF PortWatch", source_key=sid, name=sid, geography="", product="shipping", unit="x", frequency="daily")
    wh.ingest_observations(sid, rows, raw_sha256=None)


def test_world_map_payload_keeps_gaps_and_baseline():
    from oco.web.worldmap import world_map
    wh = Warehouse.memory()
    wh.set_meta("geo.chokepoints", json.dumps({"items": [{"slug": "hormuz", "name": "Strait of Hormuz", "portid": "c6", "lat": 26.3, "lon": 56.9, "screen": True}]}))
    wh.set_meta("geo.routes", json.dumps({"paths": [[[50, 25], [57, 26]]]}))
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    for f, v in (("n_tanker", 40), ("n_container", 5), ("n_dry_bulk", 3), ("n_general_cargo", 2), ("n_roro", 1)):
        _series(wh, f"portwatch.hormuz.{f}", [{"obs_start": d, "value": float(v)} for i, d in enumerate(days) if i != 100])
    w = world_map(wh.con)
    cp = w["cps"][0]
    assert w["start"] == "2025-01-01" and w["n"] == 400
    assert cp["s"]["tanker"][100] is None, "missing day stays missing, never zero"
    assert cp["s"]["tanker"][0] == 40 and cp["base"]["tanker"] == 40.0
    assert w["routes"] and w["land"] == []


def test_page_with_map_is_self_contained(tmp_path, state):
    from oco.web.page import build_page
    wh = Warehouse.memory()
    wh.set_meta("geo.chokepoints", json.dumps({"items": [{"slug": "hormuz", "name": "Strait of Hormuz", "portid": "c6", "lat": 26.3, "lon": 56.9, "screen": True}]}))
    _series(wh, "portwatch.hormuz.n_tanker", [{"obs_start": date(2025, 1, 1) + timedelta(days=i), "value": 40.0} for i in range(30)])
    html = build_page(wh.con, state, tmp_path / "p.html").read_text()
    assert not re.search(r'<script[^>]+src=|<link[^>]+href=["\']https?://|url\(["\']?https?://|@import', html)
    assert "worldMap" in html and '"world"' in html and "stackedBars" in html
