"""Second-wave indicators: refining margins, fuel tax take, asymmetric pass-through ("rockets and feathers"),
German crude import origins, household energy prices. All derived values are stored as versioned observations
(source='derived') with their formula in series metadata, so cards and exports can cite them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..settings import sources_config
from ..storage.warehouse import Warehouse, observations
from .indicators import bulletin_aligned_brent

GALLONS_PER_BARREL = 42.0
GULF_PARTNERS = {"SA": "Saudi Arabia", "IQ": "Iraq", "AE": "United Arab Emirates", "KW": "Kuwait", "QA": "Qatar", "OM": "Oman",
                 "IR": "Iran", "BH": "Bahrain"}


def _cur(con, sid):
    df = observations(con, sid)
    return df.dropna(subset=["value"])[["obs_start", "value", "version_id"]] if not df.empty else df


def _cfg():
    return sources_config().get("extras", {})


# ------------------------------------------------------------------------------------------- margins
def compute_cracks(wh: Warehouse) -> dict:
    """Crack spread (USD/bbl) = product spot (USD/gal) * 42 - Brent spot (USD/bbl), same date. US Gulf/East-coast
    product prices are a PROXY for refining margins; they are not European margins and not company profits."""
    out = {}
    brent = _cur(wh.con, "eia.brent_spot")
    for prod_sid, sid, name in (("eia.ulsd_ny_spot", "derived.crack.ulsd_ny", "Diesel refining margin proxy (NY Harbor ULSD − Brent)"),
                                ("eia.gasoline_ny_spot", "derived.crack.gasoline_ny", "Gasoline refining margin proxy (NY Harbor gasoline − Brent)")):
        p = _cur(wh.con, prod_sid)
        if p.empty or brent.empty:
            continue
        m = p.merge(brent, on="obs_start", suffixes=("_p", "_b"))
        wh.upsert_series(sid, source="derived", source_key=sid, name=name, geography="US (proxy)", product="crack", unit="USD per barrel",
                         frequency="daily", description="product spot × 42 gal/bbl − Brent spot (same date)",
                         metadata={"formula": f"{prod_sid} * 42 - eia.brent_spot",
                                   "limitations": ["US wholesale prices, a proxy for refining margins", "not a company profit measure"]})
        out[sid] = wh.ingest_observations(sid, [{"obs_start": r.obs_start.date(), "value": round(float(r.value_p * GALLONS_PER_BARREL - r.value_b), 4),
                                                 "attrs": {"inputs": [r.version_id_p, r.version_id_b]}} for r in m.itertuples()], raw_sha256=None)
    return out


# ------------------------------------------------------------------------------------------- tax take
def compute_tax_take(wh: Warehouse) -> dict:
    """VAT per litre = gross × r/(1+r) with the configured VAT rate (default 19 %). 'Other taxes and duties'
    = bulletin taxes component − VAT (energy tax and any other levies as reported). Per-tank values × tank size."""
    vat = float(_cfg().get("vat_rate_de", 0.19))
    tank = float(_cfg().get("tank_litres", 50))
    out = {}
    for prod in ("diesel", "euro95"):
        w = _cur(wh.con, f"oil_bulletin.DE.{prod}.with_tax")
        wo = _cur(wh.con, f"oil_bulletin.DE.{prod}.without_tax")
        if w.empty or wo.empty:
            continue
        m = w.merge(wo, on="obs_start", suffixes=("_w", "_wo"))
        m["vat"] = m["value_w"] * vat / (1 + vat)
        m["other"] = (m["value_w"] - m["value_wo"]) - m["vat"]
        for col, sid, name in (("vat", f"derived.tax.DE.{prod}.vat", f"Germany {prod}: VAT per litre (at {vat:.0%})"),
                               ("other", f"derived.tax.DE.{prod}.other_taxes", f"Germany {prod}: energy tax & other duties per litre")):
            wh.upsert_series(sid, source="derived", source_key=sid, name=name, geography="DE", product=prod, unit="EUR per litre",
                             frequency="weekly", description="from the EU Weekly Oil Bulletin (gross and pretax prices)",
                             metadata={"formula": (f"with_tax * {vat}/(1+{vat})" if col == "vat" else f"(with_tax - without_tax) - VAT"),
                                       "assumption": f"VAT rate {vat:.0%} for all dates (edit extras.vat_rate_de if a temporary rate applied)",
                                       "tank_litres": tank})
            out[sid] = wh.ingest_observations(sid, [{"obs_start": r.obs_start.date(), "value": round(float(getattr(r, col)), 5),
                                                     "attrs": {"inputs": [r.version_id_w, r.version_id_wo]}} for r in m.itertuples()], raw_sha256=None)
    return out


def tax_table(con) -> dict | None:
    vat = float(_cfg().get("vat_rate_de", 0.19))
    tank = float(_cfg().get("tank_litres", 50))
    rows = []
    for prod, label in (("diesel", "Diesel"), ("euro95", "Super E5")):
        v = _cur(con, f"derived.tax.DE.{prod}.vat")
        o = _cur(con, f"derived.tax.DE.{prod}.other_taxes")
        if v.empty:
            continue
        b25 = v[(v.obs_start >= "2025-01-01") & (v.obs_start <= "2025-12-31")]["value"].mean()
        last = v.iloc[-1]
        ol = o.iloc[-1]["value"] if not o.empty else None
        o25 = o[(o.obs_start >= "2025-01-01") & (o.obs_start <= "2025-12-31")]["value"].mean() if not o.empty else None
        rows.append([label, str(last.obs_start.date()), float(last.value), float(b25), float(last.value - b25), float((last.value - b25) * tank),
                     None if ol is None else float(ol), None if o25 is None else float(o25)])
    if not rows:
        return None
    return {"title": f"State revenue per litre and per {tank:.0f}-litre tank",
            "note": f"VAT computed at {vat:.0%} of the pump price (assumption, editable). Energy tax and other duties = bulletin taxes − VAT. "
                    "Higher pump prices raise VAT automatically; the energy tax is a fixed amount per litre unless the law changes (e.g. a 'Tankrabatt').",
            "columns": ["Fuel", "Latest Monday", "VAT €/L now", "VAT €/L 2025 avg", "Extra VAT €/L", f"Extra VAT per {tank:.0f} L tank €",
                        "Energy tax & other €/L now", "… 2025 avg"],
            "digits": [0, 0, 3, 3, 3, 2, 3, 3], "rows": rows}


# ------------------------------------------------------------------------------------------- rockets & feathers
def _newey_west(X, e, lags):
    n, k = X.shape
    S = (X * e[:, None]).T @ (X * e[:, None])
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        G = (X[L:] * e[L:, None]).T @ (X[:-L] * e[:-L, None])
        S += w * (G + G.T)
    XtX_inv = np.linalg.pinv(X.T @ X)
    return XtX_inv @ S @ XtX_inv


def asymmetric_passthrough(retail: pd.Series, crude: pd.Series, lags: int = 4) -> dict | None:
    """Exploratory asymmetric distributed-lag model on weekly first differences:
        dP_t = a + sum_k b+_k * max(dC_{t-k},0) + sum_k b-_k * min(dC_{t-k},0) + g * dP_{t-1} + e_t
    Cumulative pass-through after K weeks for rises vs falls; Wald test of equal long-run pass-through with
    Newey-West (HAC) standard errors. Descriptive evidence of asymmetry, NOT proof of collusion or profiteering."""
    df = pd.concat({"P": retail, "C": crude}, axis=1).dropna()
    if len(df) < 60:
        return None
    d = df.diff().dropna()
    cols = {}
    for k in range(lags + 1):
        cols[f"up{k}"] = d["C"].clip(lower=0).shift(k)
        cols[f"dn{k}"] = d["C"].clip(upper=0).shift(k)
    cols["arP"] = d["P"].shift(1)
    Xdf = pd.DataFrame(cols).dropna()
    y = d["P"].loc[Xdf.index].values
    X = np.column_stack([np.ones(len(Xdf)), Xdf.values])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta
    V = _newey_west(X, e, lags=max(1, int(round(4 * (len(y) / 100) ** (2 / 9)))))
    names = ["const"] + list(Xdf.columns)
    b = dict(zip(names, beta))
    g = b["arP"]
    up = [b[f"up{k}"] for k in range(lags + 1)]
    dn = [b[f"dn{k}"] for k in range(lags + 1)]

    def cumulative(coefs, horizon=8):
        """Response of the retail LEVEL to a one-time permanent 1-unit crude change at week 0:
        dP_h = b_h (h <= lags, else 0) + g * dP_{h-1};  level_h = sum of dP_0..dP_h."""
        level, prev, path = 0.0, 0.0, []
        for h in range(horizon + 1):
            dp = (coefs[h] if h <= lags else 0.0) + g * prev
            level += dp
            prev = dp
            path.append(level)
        return path
    cu, cd = cumulative(up), cumulative(dn)  # both = share of a 1-unit crude move passed to the pump (falls: as positive share)
    from math import erf, sqrt

    def wald(ks):
        R = np.zeros(len(names))
        for k in ks:
            R[names.index(f"up{k}")] = 1
            R[names.index(f"dn{k}")] = -1
        dval = float(R @ beta)
        se_ = float(np.sqrt(max(R @ V @ R, 1e-18)))
        z_ = dval / se_
        return dval, se_, z_, 2 * (1 - 0.5 * (1 + erf(abs(z_) / sqrt(2))))
    diff, se, z, p = wald(range(lags + 1))          # long run: total pass-through equal?
    sdiff, sse, sz, sp = wald([0])                  # speed: first-week pass-through equal?
    return {"n_weeks": int(len(y)), "from": str(Xdf.index.min().date()), "to": str(Xdf.index.max().date()), "lags": lags,
            "cum_up": [round(float(x), 3) for x in cu], "cum_down": [round(float(x), 3) for x in cd],
            "speed_diff_week0": round(sdiff, 3), "speed_p_value": round(sp, 4),
            "sum_up": round(float(sum(up)), 3), "sum_down": round(float(sum(dn)), 3), "diff": round(diff, 3), "se_hac": round(se, 3),
            "z": round(z, 2), "p_value": round(p, 4), "ar1": round(float(g), 3),
            "interpretation": "; ".join([
                ("first-week response: rises passed on faster than falls" if sdiff > 0 and sp < 0.05 else
                 "first-week response: falls passed on faster than rises" if sdiff < 0 and sp < 0.05 else
                 "first-week response: no clear difference (5% level)"),
                ("long run: rises passed on more than falls" if diff > 0 and p < 0.05 else
                 "long run: falls passed on more than rises" if diff < 0 and p < 0.05 else
                 "long run: no clear difference (5% level)")])}


def rockets_feathers(con, country="DE", products=("diesel", "euro95")) -> list[dict]:
    res = []
    for prod in products:
        r = _cur(con, f"oil_bulletin.{country}.{prod}.without_tax")
        if r.empty:
            continue
        al = bulletin_aligned_brent(con, r["obs_start"])
        if al.empty:
            continue
        retail = r.set_index("obs_start")["value"]
        crude = al.set_index("obs_start")["value"]
        for label, start, end in (("since 2015", "2015-01-01", None), ("crisis period since 1 Mar 2026", "2026-03-01", None)):
            rr = retail[retail.index >= start]
            cc = crude[crude.index >= start]
            out = asymmetric_passthrough(rr, cc, lags=4 if label.startswith("since 2015") else 2)
            if out is None:
                res.append({"product": prod, "period": label, "insufficient": True, "n_weeks": int(len(rr))})
            else:
                res.append({"product": prod, "period": label, **out})
    return res


# ------------------------------------------------------------------------------------------- imports & household
def import_shares(con, year_from="2025-01-01") -> dict | None:
    total = _cur(con, "eurostat.nrg_ti_oilm.de.total")
    df = con.execute("SELECT series_id, obs_start, value FROM observation_versions WHERE is_current AND value IS NOT NULL "
                     "AND series_id LIKE 'eurostat.nrg_ti_oilm.de.%'").df()
    if df.empty or total.empty:
        return None
    df["code"] = df["series_id"].str.split(".").str[-1].str.upper()
    df["obs_start"] = pd.to_datetime(df["obs_start"])
    gulf = df[df["code"].isin(GULF_PARTNERS)].groupby("obs_start")["value"].sum()
    tot = total.set_index("obs_start")["value"]
    share = (gulf.reindex(tot.index).fillna(0) / tot * 100).dropna()  # missing partner rows treated as absent from the sum, disclosed
    recent = df[df["obs_start"] >= year_from]
    agg = recent.groupby("code")["value"].sum()
    agg = agg.drop([c for c in ("TOTAL", "EU27_2020", "EU28", "EXT_EU27_2020", "EXT_EU28") if c in agg.index], errors="ignore")
    tot_recent = tot[tot.index >= year_from].sum()
    labels = dict(con.execute("SELECT upper(split_part(series_id,'.',4)), name FROM series WHERE series_id LIKE 'eurostat.nrg_ti_oilm.de.%'").fetchall())
    top = agg.sort_values(ascending=False).head(10)
    rows = [[labels.get(k, k).replace("Germany crude oil imports from ", ""), float(v), float(v / tot_recent * 100) if tot_recent else None] for k, v in top.items()]
    return {"share": {"x": [d.strftime("%Y-%m-%d") for d in share.index], "y": [round(float(v), 2) for v in share.values]},
            "table": {"title": f"Germany's crude oil suppliers since {year_from[:7]} (Eurostat)", "columns": ["Partner", "Thousand tonnes", "Share %"],
                      "digits": [0, 0, 1], "rows": rows,
                      "note": "Imports by country of origin as reported to Eurostat. Aggregates (EU, 'other') excluded from the ranking; shares relative to reported total."}}


def household_illustration(con) -> dict | None:
    ex = _cfg().get("household_example", {"CP0722": 160.0, "CP0453": 60.0, "CP0451": 110.0, "CP0452": 90.0})
    names = {"CP0722": "Car fuel", "CP0453": "Heating oil", "CP0451": "Electricity", "CP0452": "Gas"}
    rows = []
    for code, spend in ex.items():
        s = _cur(con, f"eurostat.prc_hicp_minr.de.{code.lower()}")
        if s.empty:
            continue
        s = s.set_index("obs_start")["value"]
        base = s[(s.index >= "2025-01-01") & (s.index <= "2025-12-31")].mean()
        last_d, last_v = s.index[-1], s.iloc[-1]
        rows.append([names.get(code, code), float(spend), str(last_d.date())[:7], float(last_v / base * 100), float(spend * last_v / base), float(spend * last_v / base - spend)])
    if not rows:
        return None
    return {"title": "What a typical household pays now (illustration)",
            "note": "Example 2025 monthly spending (editable in config: extras.household_example) scaled by Eurostat's German consumer price index "
                    "for each energy type. Illustrative arithmetic, not a measured household budget.",
            "columns": ["Energy", "2025 example €/month", "Latest month", "Index (2025 avg = 100)", "Same use now €/month", "Difference €/month"],
            "digits": [0, 0, 0, 1, 0, 0], "rows": rows}


def compute_all(wh: Warehouse) -> dict:
    return {"cracks": compute_cracks(wh), "tax": compute_tax_take(wh)}
