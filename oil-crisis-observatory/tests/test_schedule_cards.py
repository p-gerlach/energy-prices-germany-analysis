"""Acceptance: release-aware scheduling (EIA holiday/DST), scheduler loop, headline evidence logic,
narrative validation, export unit guards."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from oco.analysis import anomalies, cards, matching, narrative
from oco.exports import UnitMismatch, check_units, export_series, export_snapshot_counts
from oco.schedule import eia_expected_week_ending, eia_wpsr_release, next_due
from oco.settings import sources_config
from oco.storage.warehouse import Warehouse

UTC = timezone.utc
WPSR = {"tz": "America/New_York", "release_time": "10:30"}


@pytest.mark.parametrize("week_day,expected_utc", [
    (date(2026, 10, 5), datetime(2026, 10, 7, 14, 30, tzinfo=UTC)),   # EDT: Wed 10:30 = 14:30 UTC
    (date(2026, 11, 2), datetime(2026, 11, 4, 15, 30, tzinfo=UTC)),   # after DST ends (Nov 1): EST = 15:30 UTC
    (date(2026, 9, 7), datetime(2026, 9, 10, 15, 0, tzinfo=UTC)),     # Labor Day Mon -> Thu 11:00 EDT
    (date(2026, 11, 23), datetime(2026, 11, 25, 15, 30, tzinfo=UTC)), # Thanksgiving is Thu: Wed release unchanged
    (date(2027, 1, 18), datetime(2027, 1, 21, 16, 0, tzinfo=UTC)),    # MLK Day -> Thu 11:00 EST
    (date(2026, 3, 9), datetime(2026, 3, 11, 14, 30, tzinfo=UTC)),    # first week of US DST (Mar 8)
])
def test_eia_release_holiday_and_dst(week_day, expected_utc):
    assert eia_wpsr_release(week_day, WPSR) == expected_utc


def test_eia_release_override_wins():
    cfg = {**WPSR, "release_overrides": {"2026-12-23": "2026-12-23T12:00"}}
    assert eia_wpsr_release(date(2026, 12, 21), cfg) == datetime(2026, 12, 23, 17, 0, tzinfo=UTC)


def test_eia_expected_week_ending():
    assert eia_expected_week_ending(datetime(2026, 10, 7, 14, 30, tzinfo=UTC)) == date(2026, 10, 2)
    assert eia_expected_week_ending(datetime(2026, 9, 10, 15, 0, tzinfo=UTC)) == date(2026, 9, 4)


def test_rapid_checks_only_inside_release_window_and_until_data_arrive():
    cfg = sources_config()["sources"]["eia_weekly"]
    rel = datetime(2026, 10, 7, 14, 30, tzinfo=UTC)
    now = rel + timedelta(minutes=20)
    nd, why = next_due(cfg, now, now - timedelta(minutes=1), latest_observation=date(2026, 9, 25))
    assert nd - (now - timedelta(minutes=1)) == timedelta(minutes=5) and "rapid" in why
    nd2, why2 = next_due(cfg, now, now - timedelta(minutes=1), latest_observation=date(2026, 10, 2))
    assert nd2 - now > timedelta(hours=1) and "received" in why2
    nd3, _ = next_due(cfg, rel + timedelta(hours=4), rel + timedelta(hours=4), latest_observation=date(2026, 9, 25))
    assert nd3 - (rel + timedelta(hours=4)) >= timedelta(hours=12), "no endless rapid polling after the window"


def test_bulletin_thursday_window_in_brussels_time():
    cfg = sources_config()["sources"]["oil_bulletin"]
    thu = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)  # 11:00 CEST Thursday
    nd, why = next_due(cfg, thu, thu - timedelta(minutes=10))
    assert nd - (thu - timedelta(minutes=10)) == timedelta(minutes=60) and "inside" in why
    tue = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    nd, why = next_due(cfg, tue, tue - timedelta(minutes=10))
    assert nd > tue + timedelta(hours=12)


def test_scheduler_loop_bounded_and_stops(paths, state, monkeypatch):
    from oco import pipeline
    from oco.collectors.base import Context, RunResult

    calls = []

    def fake_refresh(ctx, keys, **kw):
        calls.append(list(keys))
        out = {}
        for k in keys:
            ctx.state.update_health(k, last_attempt_at=datetime.now(UTC))
            out[k] = RunResult(k)
        return out
    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    ctx = Context(paths=paths, state=state)
    msgs = []
    runs = pipeline.run_scheduler(ctx, ["ecb_fx", "portwatch"], max_cycles=2, tick_seconds=0, sleep=lambda s: None, echo=msgs.append)
    assert runs == 2 and calls == [["ecb_fx", "portwatch"]], "second cycle: nothing due yet"
    assert not paths.scheduler_pid.exists() and "stopped" in msgs[-1]


# ------------------------------------------------------------------------ cards
def _wh_with_brent(last_day: date, slope: float = -0.1):
    wh = Warehouse.memory()
    wh.upsert_series("eia.brent_spot", source="EIA", source_key="x", name="Brent spot", geography="Europe", product="crude",
                     unit="USD per barrel", frequency="daily")
    # value(last_day - i) = 80 + slope*i : slope<0 -> prices RISE towards last_day, slope>0 -> prices FALL
    rows = [{"obs_start": last_day - timedelta(days=i), "value": 80 + slope * i} for i in range(200)]
    wh.ingest_observations("eia.brent_spot", rows, raw_sha256=None)
    anomalies.run_anomalies(wh)
    return wh


def test_headline_after_all_observations_is_insufficient_evidence():
    wh = _wh_with_brent(date(2026, 9, 30))
    h = {"headline_id": "h1", "title": "Oil prices surge after tanker attack", "excerpt": "", "url": "https://x.org/a",
         "publisher": "x", "published_at": pd.Timestamp("2026-10-04T08:00Z"), "discovered_at": pd.Timestamp("2026-10-04T09:00Z"),
         "cluster_id": None, "discovery_source": "manual"}
    card = cards.headline_card(wh.con, h)
    assert card["relationship"] == "insufficient_evidence"
    assert "Temporal mismatch" in card["calculations"]["temporal_note"]
    m = card["calculations"]["measurements"][0]
    assert "previous_value" not in m, "no change is computed across an event that the data do not reach"


def test_headline_with_post_event_data_and_contradiction():
    wh = _wh_with_brent(date(2026, 10, 9), slope=+0.1)  # prices FALL over time in this fixture
    h = {"headline_id": "h2", "title": "Oil prices surge", "excerpt": "", "url": "https://x.org/b", "publisher": "x",
         "published_at": pd.Timestamp("2026-09-20T08:00Z"), "discovered_at": pd.Timestamp("2026-09-20T09:00Z"),
         "cluster_id": None, "discovery_source": "manual"}
    card = cards.headline_card(wh.con, h)
    assert card["relationship"] in ("complicates",)
    assert card["alternative_explanations"] and card["limitations"]
    q = card["quality"]
    assert {"evidence_quality", "freshness", "editorial_relevance"} <= set(q) and "probability" not in str(q).lower()


def test_topic_matching_german_and_english():
    assert "german_fuel_prices" in matching.match_topics("Dieselpreise an Tankstellen steigen")
    assert "hormuz_shipping" in matching.match_topics("Tanker traffic through the Strait of Hormuz")
    assert matching.claims("Ölpreis steigt") == {"up"}
    assert "disruption" in matching.claims("Shipping disrupted near Hormuz")


def test_narrative_validator_rejects_invented_numbers_and_causal_language():
    calcs = {"measurements": [{"latest_value": 81.73, "previous_value": 77.71, "pct_change": 5.17, "latest_date": "2026-10-02"}]}
    ok = "Brent was 81.73 on 2026-10-02 versus 77.71 (+5.2%)."
    assert narrative.validate_narrative(ok, calcs) == []
    bad = "Brent jumped to 95.00 because of the attack."
    issues = narrative.validate_narrative(bad, calcs)
    assert any("95.00" in i for i in issues) and any("because" in i for i in issues)


def test_every_template_number_traces_to_calculations():
    wh = _wh_with_brent(date(2026, 10, 9))
    c = cards._finish(cards.context_card(wh.con, "eia.brent_spot"))
    assert c["review_flag"] is None, c["review_flag"]


def test_snapshot_counts_cannot_be_exported_as_daily_transits(tmp_path):
    with pytest.raises(UnitMismatch):
        check_units(["candidate vessels present in one radar snapshot (count)", "vessel transits per day (AIS transit calls)"])
    wh = Warehouse.memory()
    with pytest.raises(UnitMismatch):
        export_snapshot_counts(wh.con, "hormuz_strait", tmp_path, unit_label="vessels per day")


def test_export_writes_169_png_svg_csv_note(tmp_path):
    wh = _wh_with_brent(date(2026, 10, 9))
    out = export_series(wh.con, ["eia.brent_spot"], tmp_path, "Brent test", event_date="2026-09-20")
    from PIL import Image
    w, h = Image.open(out["png"]).size
    assert (w, h) == (1920, 1080)
    csv = pd.read_csv(out["csv"])
    assert {"version_id", "obs_start", "retrieved_at", "unit"} <= set(csv.columns)
    note = open(out["note"]).read()
    assert "Observation dates" in note and "Retrieved" in note
