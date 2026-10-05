"""Transparent anomaly screening. Every finding records metric, value, baseline, sample size, formula,
data vintage (input observation version ids) and why it fired — or why it did NOT.

Rules (formula_version in each):
  price_daily      daily log return vs robust z of the previous `window` daily log returns (current excluded)
  price_weekly     weekly log return vs robust z of the previous `window` weekly log returns
  inventory_week   weekly change vs the same calendar week (±k) in the previous N years
  shipping_7d      7-day mean of daily transit counts vs a FIXED user-selected baseline window (quantiles),
                   weekday-adjusted ratio and prior-year same-season comparison; incomplete weeks suppressed
  fuel_lag_resid   residual of an exploratory distributed-lag model of weekly pretax retail price changes on
                   lagged Brent EUR/L changes (MODEL RESIDUAL, not a causal estimate)

Thresholds are tunable SCREENING rules, not calibrated probabilities.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..settings import sources_config
from ..storage.warehouse import Warehouse, now_utc, observations, series_info, sha1
from .stats import empirical_quantile_position, robust_z

FORMULA_VERSIONS = {
    "price_daily": "price_daily/v1: r_t=ln(p_t/p_{t-1}); z=0.67449*(r_t-median(R))/MAD(R), R=previous window returns",
    "price_weekly": "price_weekly/v1: r_t=ln(p_t/p_{t-1}) weekly; robust z vs previous window",
    "inventory_week": "inventory_week/v1: d_t=x_t-x_{t-1}; baseline=d in ISO weeks w±k of previous N years; robust z",
    "shipping_7d": "shipping_7d/v1: m_t=mean(x_{t-6..t}) needing>=6 obs; baseline quantiles of m over fixed window",
    "fuel_lag_resid": "fuel_lag_resid/v1: OLS dP_t = a + sum_{k=0..L-1} b_k dB_{t-k}; residual robust z vs in-sample residuals",
}

PRICE_DAILY_SERIES = {
    "eia.brent_spot": {"min_abs_pct": 4.0}, "eia.wti_spot": {"min_abs_pct": 4.0},
    "derived.brent_eur_per_barrel": {"min_abs_pct": 4.0}, "ecb.usd_per_eur": {"min_abs_pct": 1.0},
}


def _rules_cfg():
    return sources_config().get("anomaly_rules", {})


def _finding(rule, sid, row_start, row_end, value, fired, stats, baseline, thresholds, inputs, explanation, severity=0.0):
    return {"rule_id": rule, "series_id": sid, "obs_start": pd.Timestamp(row_start).date(),
            "obs_end": pd.Timestamp(row_end).date(), "value": None if value is None else float(value), "fired": bool(fired),
            "stats": stats, "baseline": baseline, "thresholds": thresholds, "input_version_ids": sorted(set(inputs)),
            "explanation": explanation, "severity_rank": float(severity), "formula_version": FORMULA_VERSIONS[rule]}


# ---------------------------------------------------------------------------------------------
def evaluate_price(df: pd.DataFrame, sid: str, rule: str, window: int, min_history: int, min_abs_pct: float,
                   z_thr: float, last_n: int = 30) -> list[dict]:
    """df: obs_start, value, version_id (current vintage). Missing values are dropped, never zero-filled.
    Returns are computed only between CONSECUTIVE available observations; a gap > 5 periods breaks the chain."""
    d = df.dropna(subset=["value"]).sort_values("obs_start").reset_index(drop=True)
    d = d[d["value"] > 0]
    if len(d) < 3:
        return []
    d["ret"] = np.log(d["value"] / d["value"].shift(1))
    gap = d["obs_start"].diff().dt.days
    max_gap = 5 if rule == "price_daily" else 15
    d.loc[gap > max_gap, "ret"] = np.nan
    out = []
    for i in range(max(1, len(d) - last_n), len(d)):
        r = d.loc[i, "ret"]
        if pd.isna(r):
            continue
        base = d.loc[max(0, i - window):i - 1, "ret"].dropna().values  # current point EXCLUDED
        sc = robust_z(r, base, min_n=min_history)
        pct = (np.exp(r) - 1) * 100
        fired = sc.z is not None and abs(sc.z) >= z_thr and abs(pct) >= min_abs_pct
        why = (f"{'FIRED' if fired else 'not fired'}: change {pct:+.2f}% (log return {r:+.4f}); "
               + (f"robust z {sc.z:+.2f} ({sc.method}, n={sc.n}); " if sc.z is not None else f"z suppressed: {sc.reason}; ")
               + f"thresholds |z|>={z_thr} AND |change|>={min_abs_pct}%")
        out.append(_finding(rule, sid, d.loc[i, "obs_start"], d.loc[i, "obs_start"], d.loc[i, "value"], fired,
                            {**sc.as_dict(), "log_return": float(r), "pct_change": float(pct),
                             "previous_value": float(d.loc[i - 1, "value"]), "previous_date": str(d.loc[i - 1, "obs_start"].date())},
                            {"kind": "previous returns", "from": str(d.loc[max(0, i - window), "obs_start"].date()),
                             "to": str(d.loc[i - 1, "obs_start"].date())},
                            {"robust_z": z_thr, "min_abs_pct": min_abs_pct, "window": window, "min_history": min_history},
                            [d.loc[i, "version_id"], d.loc[i - 1, "version_id"]], why,
                            severity=min(abs(sc.z or 0) / z_thr, 3) + min(abs(pct) / max(min_abs_pct, 1e-9), 3)))
    return out


def evaluate_inventory(df: pd.DataFrame, sid: str, unit: str, cfg: dict, z_thr: float, last_n: int = 8) -> list[dict]:
    d = df.dropna(subset=["value"]).sort_values("obs_end").reset_index(drop=True)
    if len(d) < 3:
        return []
    d["chg"] = d["value"].diff()
    d.loc[d["obs_end"].diff().dt.days > 8, "chg"] = np.nan  # missing week breaks the change
    iso = d["obs_end"].dt.isocalendar()
    d["week"], d["year"] = iso.week.astype(int), iso.year.astype(int)
    years, hw = int(cfg.get("years", 5)), int(cfg.get("week_halfwidth", 1))
    min_abs = float((cfg.get("min_abs_change") or {}).get(unit, 0))
    out = []
    for i in range(max(1, len(d) - last_n), len(d)):
        c = d.loc[i, "chg"]
        if pd.isna(c):
            continue
        y, w = d.loc[i, "year"], d.loc[i, "week"]
        weeks = {((w - 1 + k) % 52) + 1 for k in range(-hw, hw + 1)}
        mask = (d["year"] < y) & (d["year"] >= y - years) & d["week"].isin(weeks) & (d.index < i)
        base = d.loc[mask, "chg"].dropna().values
        sc = robust_z(c, base, min_n=int(cfg.get("min_history", 8)))
        level_base = d.loc[mask, "value"].values
        fired = sc.z is not None and abs(sc.z) >= z_thr and abs(c) >= min_abs
        why = (f"{'FIRED' if fired else 'not fired'}: weekly change {c:+,.1f} {unit}; seasonal baseline = same ISO week ±{hw} "
               f"in previous {years} years (n={sc.n}); "
               + (f"robust z {sc.z:+.2f} ({sc.method})" if sc.z is not None else f"z suppressed: {sc.reason}")
               + f"; thresholds |z|>={z_thr} AND |change|>={min_abs:,.0f}")
        out.append(_finding("inventory_week", sid, d.loc[i, "obs_start"], d.loc[i, "obs_end"], d.loc[i, "value"], fired,
                            {**sc.as_dict(), "change": float(c), "previous_value": float(d.loc[i - 1, "value"]),
                             "seasonal_level_median": float(np.median(level_base)) if len(level_base) else None,
                             "level_vs_seasonal_median": float(d.loc[i, "value"] - np.median(level_base)) if len(level_base) else None},
                            {"kind": "same-week changes in prior years", "years": years, "week_halfwidth": hw},
                            {"robust_z": z_thr, "min_abs_change": min_abs},
                            [d.loc[i, "version_id"], d.loc[i - 1, "version_id"]], why,
                            severity=min(abs(sc.z or 0) / z_thr, 3)))
    return out


def evaluate_shipping(df: pd.DataFrame, sid: str, cfg: dict, baseline_cfg: dict, last_n: int = 21) -> list[dict]:
    """Daily counts. Missing days stay missing: a 7-day window needs >= min_days observations,
    otherwise the point is suppressed (prevents fake 'collapse' alerts from incomplete data)."""
    if df.empty:
        return []
    s = df.set_index("obs_start")["value"].sort_index()
    vids = df.set_index("obs_start")["version_id"]
    full = pd.date_range(s.index.min(), s.index.max(), freq="D")
    s = s.reindex(full)  # NaN for missing days — NOT zero
    min_days = int(cfg.get("min_days_in_7d", 6))
    roll_mean = s.rolling(7, min_periods=min_days).mean()
    roll_n = s.rolling(7, min_periods=1).count()
    b0, b1 = pd.Timestamp(baseline_cfg["start"]), pd.Timestamp(baseline_cfg["end"])
    base_vals = roll_mean[(roll_mean.index >= b0) & (roll_mean.index <= b1)].dropna()
    base_daily = s[(s.index >= b0) & (s.index <= b1)].dropna()
    weekday_mean = base_daily.groupby(base_daily.index.weekday).mean() if not base_daily.empty else pd.Series(dtype=float)
    lo_q, hi_q = float(cfg.get("low_quantile", 0.05)), float(cfg.get("high_quantile", 0.95))
    min_abs = float(cfg.get("min_abs_change", 5))
    persist = int(cfg.get("persistence_days", 2))
    out = []
    flags = []
    idx = [t for t in s.index[-last_n:]]
    for t in idx:
        m = roll_mean.get(t)
        n_obs = int(roll_n.get(t, 0))
        window_dates = pd.date_range(t - timedelta(days=6), t)
        inputs = [vids[d] for d in window_dates if d in vids.index]
        if pd.isna(s.get(t)) or m is None or pd.isna(m):
            flags.append(False)
            out.append(_finding("shipping_7d", sid, t - timedelta(days=6), t, None, False,
                                {"n_days_present": n_obs, "suppressed": True}, {"start": str(b0.date()), "end": str(b1.date())},
                                {"min_days_in_7d": min_days}, inputs,
                                ("suppressed: no observation for this day (missing, not zero)" if pd.isna(s.get(t)) else
                                 f"suppressed: only {n_obs} of 7 days observed in window (missing days are not zero)")))
            continue
        if base_vals.empty or (t >= b0 and t <= b1):
            reason = "baseline window empty" if base_vals.empty else "point lies inside the baseline window"
            flags.append(False)
            out.append(_finding("shipping_7d", sid, t - timedelta(days=6), t, m, False,
                                {"n_days_present": n_obs, "suppressed": True}, {"start": str(b0.date()), "end": str(b1.date())},
                                {}, inputs, f"not evaluated: {reason}"))
            continue
        q_lo, q_hi, med = base_vals.quantile(lo_q), base_vals.quantile(hi_q), base_vals.median()
        pos = empirical_quantile_position(m, base_vals.values)
        cond = (m < q_lo or m > q_hi) and abs(m - med) >= min_abs
        flags.append(cond)
        persisted = len(flags) >= persist and all(flags[-persist:])
        # weekday-adjusted ratio: observed vs expected (sum of baseline weekday means over the window)
        exp = sum(weekday_mean.get(d.weekday(), np.nan) for d in window_dates if not pd.isna(s.get(d)))
        obs_sum = float(s[window_dates].dropna().sum())
        ratio = obs_sum / exp if exp and not np.isnan(exp) and exp > 0 else None
        # prior-year same-season comparison
        ly = roll_mean.get(t - pd.DateOffset(years=1))
        fired = bool(cond and persisted)
        why = (f"{'FIRED' if fired else 'not fired'}: 7-day mean {m:.1f}/day vs fixed baseline {b0.date()}..{b1.date()} "
               f"median {med:.1f} (q{int(lo_q*100)}={q_lo:.1f}, q{int(hi_q*100)}={q_hi:.1f}); empirical position {pos:.2f}; "
               f"requires outside quantile band AND |diff|>={min_abs} for {persist} consecutive days")
        out.append(_finding("shipping_7d", sid, t - timedelta(days=6), t, m, fired,
                            {"n_days_present": n_obs, "baseline_median": float(med), "q_low": float(q_lo), "q_high": float(q_hi),
                             "empirical_position": pos, "weekday_adjusted_ratio": ratio,
                             "same_period_last_year_7d_mean": None if ly is None or pd.isna(ly) else float(ly),
                             "latest_daily": float(s.get(t)), "n_baseline_windows": int(base_vals.size)},
                            {"kind": "fixed user-selected window", "start": str(b0.date()), "end": str(b1.date()),
                             "label": baseline_cfg.get("label", "")},
                            {"low_quantile": lo_q, "high_quantile": hi_q, "min_abs_change": min_abs, "persistence_days": persist},
                            inputs, why, severity=abs(m - med) / max(med, 1)))
    return out


def evaluate_fuel_lag(retail: pd.DataFrame, brent_l: pd.DataFrame, sid: str, cfg: dict, z_thr: float, last_n: int = 6) -> list[dict]:
    """Exploratory distributed-lag model on weekly first differences (reduces serial correlation of levels).
    Fitted only on data BEFORE the evaluated week. Tax changes are excluded by using PRETAX prices."""
    if retail.empty or brent_l.empty:
        return []
    r = retail.dropna(subset=["value"]).sort_values("obs_start").set_index("obs_start")
    b = brent_l.dropna(subset=["value"]).sort_values("obs_start").set_index("obs_start")
    j = r[["value", "version_id"]].join(b[["value"]].rename(columns={"value": "brent"}), how="inner")
    if len(j) < 10:
        return []
    j["dP"] = j["value"].diff()
    j["dB"] = j["brent"].diff()
    L = int(cfg.get("lags", 3))
    for k in range(L):
        j[f"dB{k}"] = j["dB"].shift(k)
    j = j.dropna(subset=["dP"] + [f"dB{k}" for k in range(L)])
    win, minh = int(cfg.get("window_weeks", 156)), int(cfg.get("min_history", 60))
    min_abs = float(cfg.get("min_abs_eur_per_litre", 0.03))
    out = []
    for i in range(max(0, len(j) - last_n), len(j)):
        train = j.iloc[max(0, i - win):i]
        if len(train) < minh:
            out.append(_finding("fuel_lag_resid", sid, j.index[i], j.index[i], j["value"].iloc[i], False,
                                {"suppressed": True, "n_train": len(train)}, {}, {"min_history": minh},
                                [j["version_id"].iloc[i]], f"suppressed: insufficient history for lag model ({len(train)} < {minh} weeks)"))
            continue
        X = np.column_stack([np.ones(len(train))] + [train[f"dB{k}"].values for k in range(L)])
        coef, *_ = np.linalg.lstsq(X, train["dP"].values, rcond=None)
        resid_in = train["dP"].values - X @ coef
        x_now = np.concatenate([[1.0], [j[f"dB{k}"].iloc[i] for k in range(L)]])
        pred = float(x_now @ coef)
        res = float(j["dP"].iloc[i] - pred)
        sc = robust_z(res, resid_in, min_n=minh)
        fired = sc.z is not None and abs(sc.z) >= z_thr and abs(res) >= min_abs
        why = (f"{'FIRED' if fired else 'not fired'}: weekly pretax change {j['dP'].iloc[i]*100:+.1f} ct/L vs model-predicted "
               f"{pred*100:+.1f} ct/L from Brent EUR/L changes (lags 0..{L-1}); residual {res*100:+.1f} ct/L; "
               + (f"robust z {sc.z:+.2f}" if sc.z is not None else f"z suppressed: {sc.reason}")
               + f". EXPLORATORY MODEL RESIDUAL — not a causal estimate; thresholds |z|>={z_thr} AND |resid|>={min_abs} EUR/L")
        out.append(_finding("fuel_lag_resid", sid, j.index[i], j.index[i], j["value"].iloc[i], fired,
                            {**sc.as_dict(), "residual": res, "predicted_change": pred, "actual_change": float(j["dP"].iloc[i]),
                             "coefficients": [float(c) for c in coef], "n_train": len(train)},
                            {"kind": "in-sample residuals of trailing model", "weeks": len(train)},
                            {"robust_z": z_thr, "min_abs_eur_per_litre": min_abs, "lags": L},
                            [j["version_id"].iloc[i]], why, severity=min(abs(sc.z or 0) / z_thr, 3)))
    return out


# ---------------------------------------------------------------------------------------------
def _store(wh: Warehouse, findings: list[dict], as_of: datetime | None, suppress_days: int) -> dict:
    """Idempotent: identical finding (same inputs) -> no-op; changed inputs -> new row, old superseded."""
    counts = {"new": 0, "superseded": 0, "unchanged": 0}
    for f in findings:
        aid = sha1(f["rule_id"], f["series_id"], f["obs_start"], f["formula_version"], json.dumps(f["input_version_ids"]), as_of)
        if wh.con.execute("SELECT 1 FROM anomalies WHERE anomaly_id=?", [aid]).fetchone():
            counts["unchanged"] += 1
            continue
        status = "replay" if as_of else "active"
        if not as_of:
            prev = wh.con.execute(
                "SELECT anomaly_id FROM anomalies WHERE rule_id=? AND series_id=? AND obs_start=? AND status='active' AND as_of IS NULL",
                [f["rule_id"], f["series_id"], f["obs_start"]]).fetchall()
            for (pid,) in prev:
                wh.con.execute("UPDATE anomalies SET status='superseded', superseded_at=?, superseded_by=? WHERE anomaly_id=?",
                               [now_utc(), aid, pid])
                counts["superseded"] += 1
            if f["fired"]:
                dup = wh.con.execute(
                    "SELECT anomaly_id FROM anomalies WHERE rule_id=? AND series_id=? AND fired AND status='active' "
                    "AND obs_start < ? AND obs_start >= ? ORDER BY obs_start DESC LIMIT 1",
                    [f["rule_id"], f["series_id"], f["obs_start"], f["obs_start"] - timedelta(days=suppress_days)]).fetchone()
                if dup:
                    f["stats"]["continuation_of"] = dup[0]
                    f["explanation"] += f" (continuation of alert {dup[0]} — duplicate suppressed in ranking)"
                    f["severity_rank"] *= 0.25
        wh.con.execute(
            "INSERT INTO anomalies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [aid, f["rule_id"], f["series_id"], f["obs_start"], f["obs_end"], f["value"], f["fired"], status,
             f["severity_rank"], f["formula_version"], json.dumps(f["baseline"], default=str), json.dumps(f["stats"], default=str),
             json.dumps(f["thresholds"], default=str), json.dumps(f["input_version_ids"]), f["explanation"], now_utc(),
             None, None, as_of])
        counts["new"] += 1
    return counts


def run_anomalies(wh: Warehouse, as_of: datetime | None = None, last_n: int | None = None) -> dict:
    cfg = _rules_cfg()
    z_thr = float(cfg.get("robust_z_threshold", 3.5))
    con = wh.con
    findings: list[dict] = []
    pd_cfg = cfg.get("price_daily", {})
    for sid, extra in PRICE_DAILY_SERIES.items():
        df = observations(con, sid, as_of=as_of)
        if not df.empty:
            findings += evaluate_price(df, sid, "price_daily", int(pd_cfg.get("window", 250)), int(pd_cfg.get("min_history", 60)),
                                       float(extra.get("min_abs_pct", pd_cfg.get("min_abs_pct", 4))), z_thr, last_n or 30)
    pw_cfg = cfg.get("price_weekly", {})
    for (sid,) in con.execute("SELECT series_id FROM series WHERE source='EU Oil Bulletin'").fetchall():
        df = observations(con, sid, as_of=as_of)
        if not df.empty:
            findings += evaluate_price(df, sid, "price_weekly", int(pw_cfg.get("window", 156)), int(pw_cfg.get("min_history", 52)),
                                       float(pw_cfg.get("min_abs_pct", 3)), z_thr, last_n or 6)
    inv_cfg = cfg.get("inventory_weekly", {})
    for (sid, unit) in con.execute("SELECT series_id, unit FROM series WHERE source='EIA' AND frequency='weekly'").fetchall():
        df = observations(con, sid, as_of=as_of)
        if not df.empty:
            findings += evaluate_inventory(df, sid, unit, inv_cfg, z_thr, last_n or 8)
    sh_cfg = cfg.get("shipping", {})
    base = sources_config().get("shipping_baseline", {"start": "2025-01-01", "end": "2025-12-31"})
    for (sid,) in con.execute("SELECT series_id FROM series WHERE source='IMF PortWatch' AND (series_id LIKE '%.n_tanker' OR series_id LIKE '%.n_total' "
            "OR series_id LIKE 'portwatch.port.%')").fetchall():
        df = observations(con, sid, as_of=as_of)
        if not df.empty:
            findings += evaluate_shipping(df, sid, sh_cfg, base, last_n or 21)
    fl_cfg = cfg.get("fuel_lag_model", {})
    brent_l = observations(con, "derived.brent_eur_per_litre", as_of=as_of)
    if not brent_l.empty:
        from .indicators import bulletin_aligned_brent
        for (sid,) in con.execute("SELECT series_id FROM series WHERE source='EU Oil Bulletin' AND series_id LIKE '%.without_tax' "
                                  "AND (series_id LIKE '%.diesel.%' OR series_id LIKE '%.euro95.%')").fetchall():
            retail = observations(con, sid, as_of=as_of)
            if retail.empty:
                continue
            al = bulletin_aligned_brent(con, retail["obs_start"], as_of=as_of)
            findings += evaluate_fuel_lag(retail, al, sid, fl_cfg, z_thr, last_n or 6)
    counts = _store(wh, findings, as_of, int(cfg.get("suppress_duplicates_days", 3)))
    counts["evaluated"] = len(findings)
    counts["fired"] = sum(f["fired"] for f in findings)
    return counts


def backtest(con, sid: str, rule: str, start: str, end: str) -> dict:
    """Alert frequency over a held-out historical period. Uses TODAY's revised history unless vintages
    exist — this is NOT a real-time backtest, and the report says so."""
    cfg = _rules_cfg()
    z_thr = float(cfg.get("robust_z_threshold", 3.5))
    df = observations(con, sid)
    if df.empty:
        return {"error": f"no data for {sid}"}
    n_points = len(df[(df["obs_start"] >= start) & (df["obs_start"] <= end)])
    if rule == "price_daily":
        c = cfg.get("price_daily", {})
        f = evaluate_price(df[df["obs_start"] <= end], sid, rule, int(c.get("window", 250)), int(c.get("min_history", 60)),
                           float(PRICE_DAILY_SERIES.get(sid, {}).get("min_abs_pct", c.get("min_abs_pct", 4))), z_thr, last_n=n_points + 1)
    elif rule == "price_weekly":
        c = cfg.get("price_weekly", {})
        f = evaluate_price(df[df["obs_start"] <= end], sid, rule, int(c.get("window", 156)), int(c.get("min_history", 52)),
                           float(c.get("min_abs_pct", 3)), z_thr, last_n=n_points + 1)
    elif rule == "shipping_7d":
        f = evaluate_shipping(df[df["obs_start"] <= end], sid, cfg.get("shipping", {}),
                              sources_config().get("shipping_baseline"), last_n=n_points + 1)
    else:
        return {"error": f"backtest not implemented for {rule}"}
    f = [x for x in f if str(x["obs_start"]) >= start]
    evaluated = [x for x in f if "suppressed" not in x["explanation"][:12]]
    fired = [x for x in f if x["fired"]]
    first_seen = df["first_seen_at"].min()
    return {"series": sid, "rule": rule, "period": f"{start}..{end}", "evaluated": len(evaluated), "fired": len(fired),
            "alert_rate": (len(fired) / len(evaluated)) if evaluated else None,
            "fired_dates": [str(x["obs_end"]) for x in fired],
            "vintage_note": (f"Uses current revised data. Real-time vintages exist only from {first_seen} (when this tool "
                             "started collecting); earlier history is NOT a real-time backtest.")}
