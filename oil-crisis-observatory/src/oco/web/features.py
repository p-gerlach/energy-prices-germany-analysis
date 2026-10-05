"""Page sections for the second-wave analyses (registered into sections.EXTRA_SECTIONS on import)."""
from __future__ import annotations

from ..analysis import extras
from . import sections as S
from .sections import C1, C2, C3, CM, EVENT, _line, _pts, chart


def section_pumps(con):
    from ..collectors.tankerkoenig import latest_snapshot
    snap = latest_snapshot(con)
    charts = [chart("Panel average pump prices (completed days)", "Daily mean over 10-minute readings of open stations in the fixed panel · Tankerkönig / MTS-K, CC BY 4.0",
                    "EUR per litre", [_line(con, "tk.panel.diesel.mean", "Diesel", C1), _line(con, "tk.panel.e10.mean", "E10", C2),
                                      _line(con, "tk.panel.e5.mean", "E5", C3)], digits=3,
                    empty_note="Waiting for data: add your free Tankerkönig key to .env, run `oco tankerkoenig build-panel` once, then keep `oco run-scheduler` running.")]
    tables = []
    if snap:
        tables.append({"title": "Latest panel reading", "note": f"{snap['n_open']} open stations at {snap['fetched_at'][:16]} UTC. Sample panel, not the national average.",
                       "columns": ["Diesel €/L", "E5 €/L", "E10 €/L"], "digits": [3, 3, 3], "rows": [[snap["diesel"], snap["e5"], snap["e10"]]]})
    return {"id": "pumps", "nav": "Live pumps", "title": "German pump prices, live panel",
            "lede": "Official prices reported to the Markttransparenzstelle für Kraftstoffe, via Tankerkönig, read every 10 minutes for a fixed panel of about 100 stations in 10 cities.",
            "charts": charts, "tables": tables}


def section_bypass(con):
    from .page import rolling7
    ser = []
    for slug, name, col in (("yanbu", "Yanbu (Red Sea, bypass)", C1), ("fujairah", "Fujairah (Gulf of Oman, bypass)", C2)):
        p = _pts(con, f"portwatch.port.{slug}.export_tanker", "2025-01-01", daily_gaps=True)
        if p:
            r = rolling7(p)
            ser.append({"name": name, "color": col, "x": r["x"], "y": [None if v is None else round(v / 1000, 1) for v in r["y"]]})
    inside = None
    import pandas as pd
    parts = []
    for slug in ("ras_tanura", "juaymah", "mina_al_ahmadi", "kharg", "jebel_ali"):
        p = _pts(con, f"portwatch.port.{slug}.export_tanker", "2025-01-01", daily_gaps=True)
        if p:
            parts.append(pd.Series(p["y"], index=pd.to_datetime(p["x"]), dtype="float64"))
    if parts:
        tot = pd.concat(parts, axis=1).sum(axis=1, min_count=len(parts)).rolling(7, min_periods=6).mean()
        inside = {"name": "Inside-Gulf terminals (sum)", "color": CM, "x": [d.strftime("%Y-%m-%d") for d in tot.index],
                  "y": [None if pd.isna(v) else round(float(v) / 1000, 1) for v in tot.values]}
    calls = []
    for slug, name, col in (("yanbu", "Yanbu", C1), ("fujairah", "Fujairah", C2)):
        p = _pts(con, f"portwatch.port.{slug}.portcalls_tanker", "2025-01-01", daily_gaps=True)
        if p:
            calls.append({"name": name, "color": col, **rolling7(p)})
    return {"id": "bypass", "nav": "Bypass ports", "title": "Did oil go around the strait?",
            "lede": "Saudi Arabia can pipe crude to Yanbu on the Red Sea; the UAE can pipe it to Fujairah outside the strait. PortWatch estimates tanker export volumes per port from AIS (ship positions and draught); these are model estimates, not customs data.",
            "charts": [chart("Estimated tanker exports, 7-day mean", "Thousand tonnes per day · IMF PortWatch port estimates. Inside-Gulf = Ras Tanura, Juaymah, Mina Al Ahmadi, Kharg Island, Jebel Ali (Iranian tankers often switch AIS off, so Kharg reads near zero).",
                           "thousand tonnes per day", ser + ([inside] if inside else []), vlines=EVENT, digits=0, height=380),
                       chart("Tanker port calls per day, 7-day mean", "Tanker visits (AIS) · IMF PortWatch", "tanker calls per day", calls, vlines=EVENT, digits=1)]}


def section_margins(con):
    return {"id": "margins", "nav": "Margins", "title": "Who earns in the middle: refining margins",
            "lede": "The gap between wholesale fuel prices and crude oil (the 'crack spread') is what refiners earn before costs. EIA publishes US wholesale prices for free; they are a proxy for European margins, not company profits.",
            "charts": [chart("Refining margin proxies (US East Coast)", "USD per barrel · product spot × 42 − Brent · source: U.S. EIA",
                             "USD per barrel", [_line(con, "derived.crack.ulsd_ny", "Diesel (ULSD) − Brent", C1, "2024-01-01"),
                                                _line(con, "derived.crack.gasoline_ny", "Gasoline − Brent", C2, "2024-01-01")], vlines=EVENT, digits=0)]}


def section_passthrough(con):
    res = extras.rockets_feathers(con)
    charts, rows = [], []
    for r in res:
        label = {"diesel": "Diesel", "euro95": "Super E5"}[r["product"]]
        if r.get("insufficient"):
            rows.append([label, r["period"], r["n_weeks"], None, None, None, None, "too few weeks for a reliable estimate (needs ≥ 60)"])
            continue
        rows.append([label, r["period"], r["n_weeks"], r["cum_up"][0] * 100, r["cum_down"][0] * 100, r["speed_p_value"], r["p_value"], r["interpretation"]])
        if r["period"].startswith("since 2015"):
            weeks = list(range(len(r["cum_up"])))  # weeks after the crude move
            charts.append(chart(f"{label}: how much of a crude move reaches the pump, week by week",
                                f"Share of a 1-cent change in Brent (EUR per litre) that appears in the pretax pump price after 0–8 weeks. Weekly EU Oil Bulletin {r['from']}–{r['to']} ({r['n_weeks']} weeks).",
                                "share passed on (%)", [{"name": "Crude rises", "color": C2, "x": weeks, "y": [round(v * 100, 1) for v in r["cum_up"]]},
                                                        {"name": "Crude falls", "color": C1, "x": weeks, "y": [round(v * 100, 1) for v in r["cum_down"]]}],
                                digits=0) | {"weeks_axis": True})
    tables = [{"title": "Rockets and feathers test results", "note": "Asymmetric distributed-lag model on weekly changes with Newey-West standard errors. "
               "p-values below 0.05 indicate a statistically clear difference. This describes pricing behaviour; it does not by itself prove collusion or profiteering.",
               "columns": ["Fuel", "Period", "Weeks", "Week-0 pass-through, rises %", "Week-0 pass-through, falls %", "p (speed)", "p (long run)", "Reading"],
               "digits": [0, 0, 0, 0, 0, 3, 3, 0], "rows": rows}]
    return {"id": "passthrough", "nav": "Rockets & feathers", "title": "Do pump prices rise like rockets and fall like feathers?",
            "lede": "Compares how quickly German pretax pump prices follow rising versus falling crude prices (in euros). Pretax prices are used so tax changes do not distort the test.",
            "charts": charts, "tables": tables}


def section_tax(con):
    t = extras.tax_table(con)
    return {"id": "tax", "nav": "Tax take", "title": "What the state earns per litre",
            "lede": "VAT is a percentage of the pump price, so it rises with every price increase; the energy tax is a fixed amount per litre.",
            "charts": [chart("Diesel: VAT and other taxes per litre", "EUR per litre · from EU Weekly Oil Bulletin; VAT computed at the configured rate",
                             "EUR per litre", [_line(con, "derived.tax.DE.diesel.vat", "VAT", C1, "2024-01-01"),
                                               _line(con, "derived.tax.DE.diesel.other_taxes", "Energy tax & other duties", C2, "2024-01-01")],
                             vlines=EVENT, digits=3)],
            "tables": [t] if t else []}


def section_imports_household(con):
    im = extras.import_shares(con)
    hh = extras.household_illustration(con)
    charts = []
    if im:
        charts.append(chart("Share of Germany's crude imports from Gulf producers", "Saudi Arabia, Iraq, UAE, Kuwait, Qatar, Oman, Iran, Bahrain · % of reported monthly total · Eurostat",
                            "% of crude imports", [{"name": "Gulf share", "color": C1, **im["share"]}], vlines=EVENT, digits=1))
    charts.append(chart("German consumer prices for energy (2025 = 100)", "Harmonised index of consumer prices, monthly · Eurostat",
                        "index", [_line(con, "eurostat.prc_hicp_minr.de.cp0722", "Car fuel", C1, "2024-01-01"),
                                  _line(con, "eurostat.prc_hicp_minr.de.cp0453", "Heating oil", C2, "2024-01-01"),
                                  _line(con, "eurostat.prc_hicp_minr.de.cp0451", "Electricity", C3, "2024-01-01"),
                                  _line(con, "eurostat.prc_hicp_minr.de.cp00", "All items", CM, "2024-01-01")], vlines=EVENT, digits=0))
    tables = [x for x in ((im or {}).get("table"), hh) if x]
    return {"id": "households", "nav": "Imports & households", "title": "Germany's exposure: imports and household bills",
            "lede": "Where Germany's crude comes from, and how much more households pay for energy than in 2025.",
            "charts": charts, "tables": tables}


S.EXTRA_SECTIONS[:] = [section_pumps, section_bypass, section_passthrough, section_margins, section_tax, section_imports_household]
