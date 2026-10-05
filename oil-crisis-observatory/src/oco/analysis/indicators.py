"""Derived indicators (stored as versioned observations with source='derived').

Conventions (documented, reproducible):
* Brent EUR/bbl (daily) = EIA Brent USD/bbl / ECB USD-per-EUR reference rate of the SAME date.
  If the ECB has no fixing that day (TARGET holiday) the most recent prior fixing within 3 days is used and
  its date recorded in attrs.fx_date. No forward-fill beyond 3 days; otherwise the value is missing.
* Brent EUR/litre = Brent EUR/bbl / 158.987294928 (litres per US oil barrel).
* Weekly alignment with the Oil Bulletin: for a bulletin price date D (a Monday snapshot), the matching crude
  cost is the MEAN of daily Brent EUR/litre over the trading days D-7 .. D-3 (previous Monday..Friday).
  This is the week before the snapshot; no arbitrary end-of-week value is used.
* Taxes component = price incl. taxes - price excl. taxes (same country/product/date).
* Crude-to-retail spread = pretax retail EUR/L - aligned Brent EUR/L. This is a COST WEDGE (refining,
  logistics, storage, retail costs, margins, biofuel obligations...) — NOT a measured profit margin.
* Monthly series are never interpolated to daily values.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ..storage.warehouse import Warehouse, observations

LITRES_PER_BARREL = 158.987294928


def usd_bbl_to_eur_bbl(usd_per_bbl: float, usd_per_eur: float) -> float:
    return usd_per_bbl / usd_per_eur


def eur_per_1000l_to_eur_per_l(v: float) -> float:
    return v / 1000.0


def eur_bbl_to_eur_l(v: float) -> float:
    return v / LITRES_PER_BARREL


def _cur(con, sid: str, as_of=None) -> pd.DataFrame:
    df = observations(con, sid, as_of=as_of)
    return df[["obs_start", "value", "version_id"]].dropna(subset=["value"]) if not df.empty else df


def brent_eur_daily(con, as_of=None) -> pd.DataFrame:
    b = _cur(con, "eia.brent_spot", as_of)
    fx = _cur(con, "ecb.usd_per_eur", as_of)
    if b.empty or fx.empty:
        return pd.DataFrame(columns=["obs_start", "value", "fx_date", "inputs"])
    b = b.sort_values("obs_start")
    fx = fx.sort_values("obs_start").rename(columns={"value": "fx", "version_id": "fx_vid", "obs_start": "fx_date"})
    m = pd.merge_asof(b, fx, left_on="obs_start", right_on="fx_date", direction="backward", tolerance=pd.Timedelta(days=3))
    m = m.dropna(subset=["fx"])
    m["eur_bbl"] = m["value"] / m["fx"]
    m["inputs"] = m.apply(lambda r: [r["version_id"], r["fx_vid"]], axis=1)
    return m[["obs_start", "eur_bbl", "fx_date", "inputs", "value", "fx"]].rename(columns={"value": "usd_bbl"})


def bulletin_aligned_brent(con, bulletin_dates, as_of=None) -> pd.DataFrame:
    d = brent_eur_daily(con, as_of)
    rows = []
    for D in pd.to_datetime(pd.Series(list(bulletin_dates))).sort_values():
        lo, hi = D - timedelta(days=7), D - timedelta(days=3)
        w = d[(d["obs_start"] >= lo) & (d["obs_start"] <= hi)] if not d.empty else d
        if w is None or w.empty:
            rows.append({"obs_start": D, "value": None, "n_days": 0, "inputs": []})
            continue
        rows.append({"obs_start": D, "value": float(w["eur_bbl"].mean() / LITRES_PER_BARREL), "n_days": int(len(w)),
                     "inputs": [v for lst in w["inputs"] for v in lst]})
    return pd.DataFrame(rows)


def compute_derived(wh: Warehouse) -> dict:
    """Recompute derived series; versioning records any change caused by revised inputs."""
    con = wh.con
    out = {}
    demo = wh.get_meta("mode") == "DEMO_SYNTHETIC"
    tag = " (SYNTHETIC)" if demo else ""
    d = brent_eur_daily(con)
    if not d.empty:
        for sid, name, unit, conv in (
            ("derived.brent_eur_per_barrel", "Brent spot in euros", "EUR per barrel", lambda v: v),
            ("derived.brent_eur_per_litre", "Brent spot in euros per litre", "EUR per litre", eur_bbl_to_eur_l),
        ):
            wh.upsert_series(sid, source="derived", source_key=sid, name=name + tag, geography="Europe (North Sea)",
                             product="crude_brent", unit=unit, frequency="daily",
                             description="EIA Brent USD/bbl ÷ ECB USD-per-EUR (same date; prior fixing ≤3 days on ECB holidays)",
                             metadata={"formula": "brent_usd / usd_per_eur" + (" / 158.987294928" if "litre" in sid else ""),
                                       "inputs": ["eia.brent_spot", "ecb.usd_per_eur"]})
            rows = [{"obs_start": r.obs_start.date(), "value": float(conv(r.eur_bbl)),
                     "attrs": {"fx_date": str(r.fx_date.date()), "inputs": r.inputs, "usd_bbl": r.usd_bbl, "usd_per_eur": r.fx}}
                    for r in d.itertuples()]
            out[sid] = wh.ingest_observations(sid, rows, raw_sha256=None)
    # Oil Bulletin derived series per country/product
    sids = [r[0] for r in con.execute("SELECT series_id FROM series WHERE source='EU Oil Bulletin'").fetchall()]
    keys = sorted({tuple(s.split(".")[1:3]) for s in sids})
    for cc, prod in keys:
        w = _cur(con, f"oil_bulletin.{cc}.{prod}.with_tax")
        wo = _cur(con, f"oil_bulletin.{cc}.{prod}.without_tax")
        if w.empty or wo.empty:
            continue
        m = w.merge(wo, on="obs_start", suffixes=("_w", "_wo"))
        sid = f"derived.oil_bulletin.{cc}.{prod}.taxes"
        wh.upsert_series(sid, source="derived", source_key=sid, name=f"{cc} {prod}: taxes & duties component{tag}",
                         geography=cc, product=prod, unit="EUR per litre", frequency="weekly",
                         description="price incl. taxes − price excl. taxes (same bulletin date)",
                         metadata={"formula": "with_tax - without_tax"})
        out[sid] = wh.ingest_observations(sid, [
            {"obs_start": r.obs_start.date(), "value": float(r.value_w - r.value_wo),
             "attrs": {"inputs": [r.version_id_w, r.version_id_wo]}} for r in m.itertuples()], raw_sha256=None)
        if prod in ("diesel", "euro95"):
            al = bulletin_aligned_brent(con, wo["obs_start"])
            if al.empty:
                continue
            m2 = wo.merge(al, on="obs_start", suffixes=("", "_brent"))
            sid2 = f"derived.cost_wedge.{cc}.{prod}"
            wh.upsert_series(sid2, source="derived", source_key=sid2,
                             name=f"{cc} {prod}: pretax retail minus prior-week Brent (cost wedge, NOT a profit margin){tag}",
                             geography=cc, product=prod, unit="EUR per litre", frequency="weekly",
                             description="pretax retail EUR/L − mean Brent EUR/L over D-7..D-3",
                             metadata={"formula": "without_tax(D) - mean(brent_eur_l[D-7..D-3])"})
            rows = []
            for r in m2.itertuples():
                val = None if r.value_brent is None or (isinstance(r.value_brent, float) and np.isnan(r.value_brent)) else float(r.value - r.value_brent)
                rows.append({"obs_start": r.obs_start.date(), "value": val,
                             "attrs": {"brent_eur_l": r.value_brent, "brent_days": r.n_days, "inputs": [r.version_id] + list(r.inputs)}})
            out[sid2] = wh.ingest_observations(sid2, rows, raw_sha256=None)
    return out


def jodi_balanced_sample(df: pd.DataFrame, months: list[str]) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Restrict to countries with non-missing values in EVERY requested month.

    df columns: country, month (YYYY-MM), value. Returns (filtered, included, excluded).
    Summing an unbalanced, changing set of reporters is not a 'global change'.
    """
    pivot = df[df["month"].isin(months)].pivot_table(index="country", columns="month", values="value", aggfunc="first")
    pivot = pivot.reindex(columns=months)
    complete = pivot.dropna(how="any")
    included = sorted(complete.index)
    excluded = sorted(set(pivot.index) - set(included))
    return df[df["country"].isin(included) & df["month"].isin(months)], included, excluded
