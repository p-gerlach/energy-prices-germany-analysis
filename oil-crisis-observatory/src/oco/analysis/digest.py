"""Weekly story finder: a ranked list of leads built only from stored calculations.

Kinds: unusual (screening rule fired, current episode), supported / contradicted (headline cards), pending (a claim
the data cannot test yet + the next release that could), context (standing findings: tax take, pass-through,
margins, household costs). Every number is copied from a stored calculation; nothing is estimated here.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd

from ..storage.state import utcnow


def _fmt(v, d=1):
    return "–" if v is None else f"{v:,.{d}f}"


def _alert_line(name: str, rule: str, value, stats: dict, obs_end: str) -> tuple[str, str]:
    if rule == "shipping_7d":
        k = 1000.0 if "export volume" in name else 1.0
        u = "thousand t/day" if k > 1 else "per day"
        f = lambda v: _fmt(None if v is None else v / k, 1)  # noqa: E731
        return (f"{name}: {f(value)} {u} (7-day mean)",
                f"2025 normal: median {f(stats.get('baseline_median'))} {u}, usual range {f(stats.get('q_low'))}–{f(stats.get('q_high'))}. Through {obs_end}.")
    if rule in ("price_daily", "price_weekly"):
        return (f"{name}: {stats.get('pct_change', 0):+.1f}% in one {'day' if rule == 'price_daily' else 'week'}",
                f"Now {_fmt(value, 2)}; robust z {_fmt(stats.get('z'), 1)} against its own history. Observation {obs_end}.")
    if rule == "inventory_week":
        return (f"{name}: weekly change {_fmt(stats.get('change'), 0)}", f"Outside the usual range for this time of year. Week ending {obs_end}.")
    if rule == "fuel_lag_resid":
        return (f"{name}: moved {_fmt((stats.get('residual') or 0) * 100)} ct/L differently than crude costs predict",
                f"Exploratory model residual, week of {obs_end}. Check taxes, refinery issues or local factors.")
    return (f"{name}: unusual reading", f"Rule {rule}, {obs_end}.")


def build_digest(con, max_items: int = 14) -> dict:
    items = []
    cutoff = (utcnow() - timedelta(days=45)).date()
    an = con.execute("""SELECT a.anomaly_id, a.series_id, s.name, a.rule_id, a.obs_end, a.value, a.stats, a.severity_rank
                        FROM anomalies a JOIN series s USING(series_id)
                        WHERE a.fired AND a.status='active' AND a.as_of IS NULL AND a.obs_end >= ? ORDER BY a.obs_end DESC""", [cutoff]).df()
    if not an.empty:
        an = an.drop_duplicates(["series_id", "rule_id"])  # latest reading per series & rule (ongoing episodes included)
        an = an[~an["series_id"].str.contains(r"\.(?:AT|BE|ES|FR|IT|NL|PL)\.|jebel_ali|kharg", regex=True)]
        cand = []
        for r in an.itertuples():
            st = json.loads(r.stats)
            if r.rule_id == "shipping_7d":
                med = st.get("baseline_median") or 0
                score = 10 * min(1.0, abs((r.value or 0) - med) / med) if med else 0   # 10 = fully collapsed / doubled
            else:
                score = min(10.0, abs(st.get("z") or 0))
            fam = (r.name.replace(" incl. taxes", "").replace(" excl. taxes", "").replace(" in euros", "").replace(" FOB", "")
                   .replace("Brent spot price", "Brent spot").split(":")[0])
            prio = 1 if r.series_id.startswith("portwatch.hormuz") else (0.5 if "yanbu" in r.series_id or "fujairah" in r.series_id else 0)
            cand.append((score + prio, fam, r))
        cand.sort(key=lambda x: -x[0])
        seen_fam, per_rule = set(), {}
        for score, fam, r in cand:
            if fam in seen_fam or per_rule.get(r.rule_id, 0) >= 4:
                continue
            seen_fam.add(fam)
            per_rule[r.rule_id] = per_rule.get(r.rule_id, 0) + 1
            t, d = _alert_line(r.name, r.rule_id, r.value, json.loads(r.stats), str(r.obs_end)[:10])
            items.append({"kind": "unusual", "title": t, "detail": d, "ref": r.anomaly_id, "rank": 100 + score})
            if sum(1 for i in items if i["kind"] == "unusual") >= 6:
                break
    cards = con.execute("SELECT card_id, relationship, payload FROM evidence_cards WHERE status='current' AND kind='headline' ORDER BY created_at DESC").df()
    for r in cards.itertuples():
        p = json.loads(r.payload)
        h = p.get("headline") or {}
        if r.relationship in ("supports", "complicates"):
            items.append({"kind": "supported" if r.relationship == "supports" else "contradicted", "title": h.get("title", p["question"]),
                          "detail": f"{h.get('publisher', '')}: {p['calculations'].get('relationship_reason', '')}", "ref": r.card_id,
                          "rank": 80 if r.relationship == "complicates" else 70})
    pending = [json.loads(x)["headline"]["title"] for x in cards[cards["relationship"] == "insufficient_evidence"]["payload"]][:3]
    try:
        from ..settings import get_paths
        from ..storage.state import StateStore
        st = {h["source"]: h for h in StateStore(get_paths().jobs_db).health()}
        rel = st.get("oil_bulletin", {}).get("next_expected_release")
    except Exception:  # noqa: BLE001
        rel = None
    if pending:
        items.append({"kind": "pending", "title": f"{len(pending)} recent fuel-price headlines cannot be tested yet",
                      "detail": "Newest weekly bulletin predates them" + (f"; next bulletin expected {str(rel)[:10]}" if rel else "") + ". E.g. “" + pending[0] + "”.",
                      "rank": 60})
    # standing context findings (numbers from stored derived series / analyses)
    try:
        from . import extras
        t = extras.tax_table(con)
        if t:
            d = t["rows"][0]
            items.append({"kind": "context", "title": f"State earns {d[4]*100:.1f} ct more VAT per litre of diesel than in 2025",
                          "detail": f"VAT {d[2]:.3f} €/L on {d[1]} vs {d[3]:.3f} €/L 2025 average: {d[5]:.2f} € more per 50-litre tank (VAT at configured rate).", "rank": 50})
        rf = [x for x in extras.rockets_feathers(con, products=("diesel",)) if not x.get("insufficient") and x["period"].startswith("since 2015")]
        if rf:
            x = rf[0]
            items.append({"kind": "context", "title": f"Diesel: crude rises reach the pump faster than falls (week 0: {x['cum_up'][0]*100:.0f}% vs {x['cum_down'][0]*100:.0f}%)",
                          "detail": f"Weekly German pretax prices {x['from']}–{x['to']}, p(speed)={x['speed_p_value']:.3f}, p(long run)={x['p_value']:.3f}. {x['interpretation']}.",
                          "rank": 55})
        cr = con.execute("SELECT obs_start, value FROM observation_versions WHERE series_id='derived.crack.ulsd_ny' AND is_current ORDER BY obs_start").df()
        if not cr.empty:
            cr["obs_start"] = pd.to_datetime(cr["obs_start"])
            med = cr[(cr.obs_start >= "2025-01-01") & (cr.obs_start <= "2025-12-31")]["value"].median()
            last = cr.iloc[-1]
            items.append({"kind": "context", "title": f"US diesel refining margin proxy at ${last.value:.0f}/bbl ({last.value/med:.1f}× its 2025 median)",
                          "detail": f"NY Harbor ULSD minus Brent on {last.obs_start.date()}; 2025 median ${med:.0f}/bbl. A proxy for refiners' gross margin, not profits.", "rank": 52})
        hh = extras.household_illustration(con)
        if hh:
            car = next((r for r in hh["rows"] if r[0] == "Car fuel"), None)
            if car:
                items.append({"kind": "context", "title": f"Car fuel prices {car[3]-100:+.0f}% vs 2025 average ({car[2]})",
                              "detail": f"A household spending €{car[1]:.0f}/month on fuel in 2025 would pay about €{car[4]:.0f} for the same amount now (Eurostat HICP, illustration).", "rank": 45})
    except Exception as e:  # noqa: BLE001
        items.append({"kind": "context", "title": "Some context findings could not be computed", "detail": f"{type(e).__name__}: {e}", "rank": 0})
    items.sort(key=lambda x: -x["rank"])
    return {"generated": str(utcnow())[:16], "items": items[:max_items]}


def digest_markdown(d: dict) -> str:
    lab = {"unusual": "UNUSUAL", "contradicted": "DATA CONTRADICT", "supported": "DATA SUPPORT", "pending": "WAITING", "context": "CONTEXT"}
    lines = [f"# Story finder — {d['generated']} UTC", "", "Leads to check, not conclusions. Every number comes from a stored calculation.", ""]
    for it in d["items"]:
        lines.append(f"- **[{lab.get(it['kind'], it['kind'])}] {it['title']}**  \n  {it['detail']}")
    return "\n".join(lines) + "\n"
