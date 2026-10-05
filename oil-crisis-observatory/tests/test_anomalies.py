"""Acceptance: anomaly edge cases — no future data, zero MAD, insufficient history, missing shipping days."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from oco.analysis.anomalies import evaluate_fuel_lag, evaluate_inventory, evaluate_price, evaluate_shipping
from oco.analysis.stats import robust_z


def frame(values, start=date(2024, 1, 1), step=1):
    d = [pd.Timestamp(start + timedelta(days=i * step)) for i in range(len(values))]
    return pd.DataFrame({"obs_start": d, "obs_end": d, "value": values, "version_id": [f"v{i}" for i in range(len(values))]})


def test_robust_z_basic_and_never_infinite():
    sc = robust_z(10.0, [1, 2, 3, 4, 5] * 10)
    assert sc.method == "MAD" and np.isfinite(sc.z)
    zero_mad = robust_z(5.0, [1.0] * 30 + [2.0] * 3 + [0.5] * 2, min_n=20)  # MAD=0, IQR>0? (IQR 0 too here)
    assert zero_mad.z is None or np.isfinite(zero_mad.z)
    const = robust_z(5.0, [1.0] * 50)
    assert const.z is None and "insufficient variability" in const.reason
    iqr = robust_z(5.0, [1.0] * 26 + [2.0] * 24)  # median 1, MAD 0, IQR 1
    assert iqr.method == "IQR-fallback" and np.isfinite(iqr.z)
    short = robust_z(5.0, [1, 2, 3])
    assert short.z is None and "insufficient history" in short.reason


def test_price_baseline_uses_no_future_observations():
    rng = np.random.default_rng(1)
    vals = list(70 * np.exp(np.cumsum(rng.normal(0, 0.01, 400))))
    df = frame(vals)
    target = df.loc[300, "obs_start"]
    res_a = [f for f in evaluate_price(df, "s", "price_daily", 250, 60, 4.0, 3.5, last_n=200) if pd.Timestamp(f["obs_start"]) == target][0]
    df2 = df.copy()
    df2.loc[301:, "value"] = df2.loc[301:, "value"] * 5  # wild future changes
    res_b = [f for f in evaluate_price(df2, "s", "price_daily", 250, 60, 4.0, 3.5, last_n=200) if pd.Timestamp(f["obs_start"]) == target][0]
    assert res_a["stats"]["z"] == res_b["stats"]["z"]
    assert res_a["baseline"]["to"] < str(target.date()), "current point excluded from its own baseline"


def test_price_spike_fires_and_needs_both_thresholds():
    rng = np.random.default_rng(2)
    vals = list(70 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))))
    vals.append(vals[-1] * 1.12)
    f = evaluate_price(frame(vals), "s", "price_daily", 250, 60, 4.0, 3.5, last_n=1)[-1]
    assert f["fired"] and "FIRED" in f["explanation"]
    f2 = evaluate_price(frame(vals), "s", "price_daily", 250, 60, 50.0, 3.5, last_n=1)[-1]
    assert not f2["fired"], "economically meaningful absolute threshold also required"


def test_insufficient_history_suppresses():
    f = evaluate_price(frame([70, 71, 72, 90]), "s", "price_daily", 250, 60, 4.0, 3.5)
    assert all(not x["fired"] and x["stats"]["method"] == "suppressed" for x in f)


def test_missing_shipping_days_cannot_fake_a_collapse():
    rng = np.random.default_rng(3)
    vals = list(rng.poisson(40, 500).astype(float))
    for i in range(495, 500):
        vals[i] = None  # provider gap at the end (incomplete days)
    vals[490] = None
    df = frame(vals, start=date(2025, 1, 1)).dropna(subset=["value"])
    res = evaluate_shipping(df, "portwatch.hormuz.n_tanker", {"min_days_in_7d": 6, "low_quantile": 0.05, "high_quantile": 0.95,
                                                            "min_abs_change": 5, "persistence_days": 2},
                            {"start": "2025-01-01", "end": "2025-12-31"}, last_n=30)
    # no LOW-traffic alert can arise from gaps (high random excursions are ordinary screening false alarms)
    assert not any(r["fired"] and r["value"] < r["stats"]["baseline_median"] for r in res)
    gap_day = pd.Timestamp(date(2025, 1, 1) + timedelta(days=490)).date()
    gap = [r for r in res if r["obs_end"] == gap_day][0]
    assert not gap["fired"] and "missing, not zero" in gap["explanation"]
    # nothing is inferred after the last observed date (provider lag)
    assert max(r["obs_end"] for r in res) == df["obs_end"].max().date()
    # a window with fewer than 6 observed days is suppressed
    sparse = frame([40.0] * 400 + [40.0, None, None, 10.0, None, 10.0, 10.0], start=date(2025, 1, 1)).dropna(subset=["value"])
    r2 = evaluate_shipping(sparse, "x", {"min_days_in_7d": 6, "persistence_days": 1, "min_abs_change": 5},
                           {"start": "2025-01-01", "end": "2025-12-31"}, last_n=1)[-1]
    assert not r2["fired"] and "of 7 days observed" in r2["explanation"]


def test_shipping_real_drop_fires_with_persistence():
    rng = np.random.default_rng(4)
    vals = list(rng.poisson(40, 400).astype(float)) + [15.0] * 10
    df = frame(vals, start=date(2025, 1, 1))
    res = evaluate_shipping(df, "x", {"min_days_in_7d": 6, "low_quantile": 0.05, "high_quantile": 0.95, "min_abs_change": 5,
                                      "persistence_days": 2}, {"start": "2025-01-01", "end": "2025-12-31"}, last_n=12)
    fired = [r for r in res if r["fired"]]
    assert fired and fired[0]["stats"]["baseline_median"] > 30


def test_inventory_seasonal_baseline_and_zero_variability():
    d0 = date(2019, 1, 4)
    vals = [400000 + 1000 * ((i % 52) - 26) for i in range(52 * 6)]
    df = frame(vals, start=d0, step=7)
    res = evaluate_inventory(df, "eia.x", "thousand barrels", {"years": 5, "week_halfwidth": 1, "min_history": 8,
                                                               "min_abs_change": {"thousand barrels": 4000}}, 3.5, last_n=3)
    assert res and all(np.isfinite(r["stats"]["change"]) for r in res)
    assert all(r["stats"]["z"] is None or np.isfinite(r["stats"]["z"]) for r in res)


def test_fuel_lag_model_suppressed_without_history_and_labelled_exploratory():
    retail = frame([1.5 + 0.01 * i for i in range(20)], step=7)
    brent = frame([0.5 + 0.01 * i for i in range(20)], step=7)
    res = evaluate_fuel_lag(retail, brent, "oil_bulletin.DE.diesel.without_tax", {"lags": 3, "window_weeks": 156, "min_history": 60}, 3.5)
    assert res and all("suppressed" in r["explanation"] for r in res)
    retail = frame(list(1.5 + np.cumsum(np.random.default_rng(5).normal(0, 0.01, 120))), step=7)
    brent = frame(list(0.5 + np.cumsum(np.random.default_rng(6).normal(0, 0.01, 120))), step=7)
    res = evaluate_fuel_lag(retail, brent, "s", {"lags": 3, "window_weeks": 156, "min_history": 60}, 3.5)
    assert any("EXPLORATORY MODEL RESIDUAL" in r["explanation"] for r in res)
