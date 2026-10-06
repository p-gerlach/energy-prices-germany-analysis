"""Live ship viewer: policy guard for the websocket route, AIS parsing (incl. 'not available' sentinels and
untrusted text), provider refusal stops the stream, and the local server serves page + snapshot.
All messages here are hand-written test fixtures; nothing is presented as live data."""
import asyncio
import json
import threading
import time
import urllib.request

import pytest

from oco.live import ais
from oco.live.ais import FatalStreamError, StreamStatus, VesselStore, run_stream, ship_class
from oco.policy import PolicyDenied, load_policy


def pos_msg(mmsi, lat, lon, sog=12.3, cog=95.0, hdg=94, ns=0, name="TEST TANKER"):
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi, "ShipName": name, "latitude": lat, "longitude": lon},
            "Message": {"PositionReport": {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": cog,
                                           "TrueHeading": hdg, "NavigationalStatus": ns}}}


def static_msg(mmsi, typ=80, name="TEST TANKER", dest="FUJAIRAH"):
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": mmsi, "ShipName": name},
            "Message": {"ShipStaticData": {"UserID": mmsi, "Name": name, "Type": typ, "ImoNumber": 9876543, "CallSign": "ABCD1",
                                           "Destination": dest, "MaximumStaticDraught": 14.2, "Dimension": {"A": 200, "B": 50, "C": 20, "D": 24},
                                           "Eta": {"Month": 10, "Day": 7, "Hour": 6, "Minute": 30}}}}


def test_websocket_route_is_the_only_one_and_https_cannot_reuse_it():
    p = load_policy()
    assert p.check("aisstream", "GET", "wss://stream.aisstream.io/v0/stream")
    for bad in ("https://stream.aisstream.io/v0/stream", "wss://stream.aisstream.io/v1/other", "wss://evil.example.com/v0/stream"):
        with pytest.raises(PolicyDenied):
            p.check("aisstream", "GET", bad)
    with pytest.raises(PolicyDenied):  # websocket scheme never matches ordinary https routes
        p.check("portwatch", "GET", "wss://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query")
    assert "api.myshiptracking.com" in p.excluded


def test_parsing_classes_sentinels_and_untrusted_text():
    s = VesselStore()
    assert s.ingest(pos_msg(538001234, 26.5, 56.3))
    assert s.ingest(static_msg(538001234, dest="<script>alert(1)</script>"))
    assert not s.ingest(pos_msg(538001234, 91, 181))            # 'not available' position is ignored
    assert s.ingest(pos_msg(636009999, 12.6, 43.4, sog=102.3, cog=360, hdg=511, name="CARGO ONE"))
    snap = {v["m"]: v for v in s.snapshot()}
    t = snap[538001234]
    assert t["k"] == "tanker" and t["f"] == "Marshall Islands" and t["imo"] == 9876543 and t["L"] == 250 and t["B"] == 44
    assert "<" not in (t["d"] or "") and ">" not in (t["d"] or "")
    c = snap[636009999]
    assert c["s"] is None and c["c"] is None and c["h"] is None and c["k"] == "unknown" and c["f"] == "Liberia"
    assert [ship_class(x) for x in (84, 71, 60, 30, 52, 35, 37, None)] == ["tanker", "cargo", "passenger", "fishing", "service", "military", "leisure", "unknown"]


def test_stale_vessels_are_dropped():
    s = VesselStore(stale_after_s=60)
    s.ingest(pos_msg(538001234, 26.5, 56.3), now=1000)
    assert len(s.snapshot(now=1030)) == 1 and s.snapshot(now=1100) == []


class FakeWS:
    def __init__(self, messages, sent):
        self.messages, self.sent = messages, sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))

    def __aiter__(self):
        async def gen():
            for m in self.messages:
                yield json.dumps(m)
        return gen()


def test_provider_refusal_stops_and_key_goes_only_in_subscription():
    sent, store, status, stop = [], VesselStore(), StreamStatus(), threading.Event()
    connect = lambda: FakeWS([pos_msg(538001234, 26.5, 56.3), {"error": "Api Key Is Not Valid"}], sent)  # noqa: E731
    asyncio.run(run_stream(load_policy(), "k-123", [[[22, 47], [30, 60]]], store, status, stop, connect=connect, min_backoff=0))
    assert status.state == "stopped" and "Api Key Is Not Valid" in status.detail
    assert sent == [{"APIKey": "k-123", "BoundingBoxes": [[[22, 47], [30, 60]]], "FilterMessageTypes": ais.MESSAGE_TYPES}]
    assert len(store.snapshot()) == 1
    with pytest.raises(FatalStreamError):
        ais.check_provider_message({"message": "Subscription required: upgrade your plan"}, load_policy().stop_markers)


def test_local_server_serves_page_and_snapshot_without_the_key(ctx_factory, monkeypatch):
    from oco.live import server
    monkeypatch.setenv("AISSTREAM_API_KEY", "secret-key-xyz")
    monkeypatch.setattr(server, "land_for_live", lambda ctx: [[[50, 25], [51, 25], [51, 26], [50, 25]]])
    ctx = ctx_factory(lambda r: None)
    stop = threading.Event()
    msgs = [pos_msg(538001234, 26.5, 56.3), static_msg(538001234)]
    th = threading.Thread(target=server.serve, kwargs=dict(ctx=ctx, areas=["gulf"], port=8799, echo=lambda *a: None, stop=stop,
                                                          connect=lambda: FakeWS(msgs, [])), daemon=True)
    th.start()
    try:
        for _ in range(50):
            try:
                page = urllib.request.urlopen("http://127.0.0.1:8799/", timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.1)
        time.sleep(0.3)
        snap = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/ships", timeout=2).read())
    finally:
        stop.set()
        th.join(timeout=5)
    assert "secret-key-xyz" not in page and "secret-key-xyz" not in json.dumps(snap)
    assert "<script src=" not in page and page.count("https://") == page.count("https://www.marinetraffic.com/")
    assert snap["areas"] == ["gulf"] and snap["ships"][0]["k"] == "tanker"
