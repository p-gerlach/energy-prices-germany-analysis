"""Acceptance: repeated ingestion, revisions/vintages, missing data, immutable evidence, dependent cards."""
import os
from datetime import date, datetime, timedelta, timezone

from oco.analysis import anomalies, cards
from oco.storage.warehouse import Warehouse, observations


def _series(wh, sid="eia.brent_spot", unit="USD per barrel", freq="daily", source="EIA"):
    wh.upsert_series(sid, source=source, source_key=sid, name=sid, geography="x", product="p", unit=unit, frequency=freq)


def _rows(n=120, start=date(2026, 1, 1), bump=None):
    out = []
    v = 70.0
    for i in range(n):
        d = start + timedelta(days=i)
        v = v * (1.001 if i % 2 else 0.999)
        out.append({"obs_start": d, "obs_end": d, "value": round(v + (bump if bump and i == n - 1 else 0), 4)})
    return out


def test_identical_reingestion_creates_no_duplicates():
    wh = Warehouse.memory()
    _series(wh)
    r1 = wh.ingest_observations("eia.brent_spot", _rows(), raw_sha256="a")
    r2 = wh.ingest_observations("eia.brent_spot", _rows(), raw_sha256="b")
    assert r1["new"] == 120 and r2 == {"new": 0, "revised": 0, "unchanged": 120, "revised_keys": []}
    assert wh.con.execute("SELECT COUNT(*) FROM observation_versions").fetchone()[0] == 120
    a1 = anomalies.run_anomalies(wh)
    a2 = anomalies.run_anomalies(wh)
    assert a2["new"] == 0 and a2["unchanged"] == a1["new"], "re-running analysis must not duplicate alerts"


def test_revision_creates_new_vintage_and_keeps_history():
    wh = Warehouse.memory()
    _series(wh)
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    wh.ingest_observations("eia.brent_spot", _rows(), raw_sha256="a", retrieved_at=t0)
    revised = _rows()
    revised[-1]["value"] += 1.5
    t1 = t0 + timedelta(days=1)
    res = wh.ingest_observations("eia.brent_spot", revised, raw_sha256="b", retrieved_at=t1)
    assert res["revised"] == 1 and res["revised_keys"] == [revised[-1]["obs_start"]]
    allv = observations(wh.con, "eia.brent_spot", current_only=False)
    last = allv[allv["obs_start"] == allv["obs_start"].max()]
    assert list(last["revision_no"]) == [1, 2] and list(last["is_current"]) == [False, True]
    # vintage replay: what was known before the revision
    old = observations(wh.con, "eia.brent_spot", as_of=t0 + timedelta(hours=1))
    assert abs(old.iloc[-1]["value"] - _rows()[-1]["value"]) < 1e-9


def test_missing_is_not_zero():
    wh = Warehouse.memory()
    _series(wh)
    wh.ingest_observations("eia.brent_spot", [{"obs_start": date(2026, 1, 1), "value": None},
                                              {"obs_start": date(2026, 1, 2), "value": float("nan")}], raw_sha256=None)
    df = observations(wh.con, "eia.brent_spot")
    assert df["value"].isna().all() and set(df["status"]) == {"missing"}
    # a later real value is a revision of a missing point, not a duplicate
    r = wh.ingest_observations("eia.brent_spot", [{"obs_start": date(2026, 1, 1), "value": 0.0}], raw_sha256=None)
    assert r["revised"] == 1


def test_raw_evidence_is_immutable_and_deduplicated(paths):
    with Warehouse.writer(paths) as wh:
        s1 = wh.store_raw("ecb", b"abc", url="https://x/y?api_key=<redacted>", content_type="text/csv", ext="csv")
        s2 = wh.store_raw("ecb", b"abc", url="https://x/y", content_type="text/csv", ext="csv")
        rel = wh.con.execute("SELECT rel_path FROM raw_files WHERE sha256=?", [s1]).fetchone()[0]
        n = wh.con.execute("SELECT COUNT(*) FROM raw_files").fetchone()[0]
    assert s1 == s2 and n == 1
    f = paths.raw / rel
    assert f.read_bytes() == b"abc" and not os.access(f, os.W_OK) or (os.geteuid() == 0)  # root ignores mode bits
    assert oct(f.stat().st_mode)[-3:] == "444"
    assert paths.snapshot.exists(), "writer publishes a read-only snapshot for the dashboard"


def test_revised_data_update_dependent_card_without_erasing_history():
    wh = Warehouse.memory()
    _series(wh)
    wh.ingest_observations("eia.brent_spot", _rows(), raw_sha256="a")
    anomalies.run_anomalies(wh)
    c1 = cards.context_card(wh.con, "eia.brent_spot")
    v1, new1 = cards.save_card(wh, cards._finish(c1))
    v_same, new_same = cards.save_card(wh, cards._finish(cards.context_card(wh.con, "eia.brent_spot")))
    assert (v1, new1) == (1, True) and new_same is False
    rev = _rows()
    rev[-1]["value"] += 3.0
    wh.ingest_observations("eia.brent_spot", rev, raw_sha256="b")
    an = anomalies.run_anomalies(wh)
    assert an["superseded"] >= 1, "alerts on revised inputs are superseded, not deleted"
    v2, new2 = cards.save_card(wh, cards._finish(cards.context_card(wh.con, "eia.brent_spot")))
    assert (v2, new2) == (2, True)
    rows = wh.con.execute("SELECT version, status FROM evidence_cards WHERE card_id='c-eia.brent_spot' ORDER BY version").fetchall()
    assert rows == [(1, "superseded"), (2, "current")]
    assert wh.con.execute("SELECT COUNT(*) FROM anomalies WHERE status='superseded'").fetchone()[0] >= 1
