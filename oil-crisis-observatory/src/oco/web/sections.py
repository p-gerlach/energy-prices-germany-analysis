"""Content for the self-contained research page. Each section function returns plain dicts; charts are
pre-computed point lists (dates + values) so the browser only draws."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..settings import get_paths, sources_config
from ..storage.warehouse import series_info

C1, C2, C3, CM = "var(--s1)", "var(--s2)", "var(--s3)", "var(--muted)"
EVENT = [{"x": "2026-03-01", "label": "1 Mar 2026"}]


def _pts(con, sid, start=None, daily_gaps=False):
    from .page import series_points
    return series_points(con, sid, start, daily_gaps)


def _line(con, sid, name=None, color=C1, start=None, **kw):
    p = _pts(con, sid, start, daily_gaps=kw.pop("daily_gaps", False))
    if not p:
        return None
    return {"name": name or series_info(con, sid).get("name", sid), "color": color, **p, **kw}


def chart(title, note, ytitle, series, band=None, vlines=None, digits=None, height=340, empty_note=None):
    series = [s for s in series if s]
    return {"title": title, "note": note, "ytitle": ytitle, "series": series, "band": band, "vlines": vlines or [],
            "digits": digits, "height": height, "empty": None if series else (empty_note or "No data stored yet for this chart.")}


def _last(con, sid):
    r = con.execute("SELECT obs_end, value FROM observation_versions WHERE series_id=? AND is_current AND value IS NOT NULL "
                    "ORDER BY obs_end DESC LIMIT 1", [sid]).fetchone()
    return (str(r[0]), float(r[1])) if r else (None, None)


def _value_at_or_before(con, sid, date):
    r = con.execute("SELECT obs_end, value FROM observation_versions WHERE series_id=? AND is_current AND value IS NOT NULL "
                    "AND obs_end<=CAST(? AS DATE) ORDER BY obs_end DESC LIMIT 1", [sid, date]).fetchone()
    return (str(r[0]), float(r[1])) if r else (None, None)


# ------------------------------------------------------------------------------------------------
def section_market(con):
    return {"id": "market", "title": "Crude oil prices",
            "lede": "EIA daily spot prices, published by EIA in weekly batches (several days late). Brent in euros divides the USD price by the ECB rate of the same day.",
            "charts": [
                chart("Brent and WTI spot", "US dollars per barrel · source: U.S. EIA", "USD per barrel",
                      [_line(con, "eia.brent_spot", "Brent", C1, "2024-01-01"), _line(con, "eia.wti_spot", "WTI", C2, "2024-01-01")], vlines=EVENT),
                chart("Brent in euros", "Euros per barrel · EIA ÷ ECB reference rate", "EUR per barrel",
                      [_line(con, "derived.brent_eur_per_barrel", "Brent in EUR", C1, "2024-01-01")], vlines=EVENT),
                chart("US commercial crude stocks", "Thousand barrels, weekly · EIA · US evidence only (SPR separate)", "thousand barrels",
                      [_line(con, "eia.us_commercial_crude_stocks", "Commercial crude", C1, "2023-01-01")], digits=0),
            ]}


def section_shipping(con):
    from .page import band_from, rolling7
    charts = []
    raw = _pts(con, "portwatch.hormuz.n_tanker", "2024-12-20", daily_gaps=True)
    if raw:
        r7 = rolling7(raw)
        b = band_from(r7, "2025-01-01", "2025-12-31")
        charts.append(chart("Strait of Hormuz: tanker transits per day",
                            "IMF PortWatch AIS transit calls (ships, not barrels). Thin line = daily count, thick = 7-day mean (≥6 observed days). "
                            "Shaded = 2025 normal range. Latest day shown is the latest PortWatch has published.",
                            "transits per day", [{"name": "Daily count", "color": C1, "width": 1, "opacity": 0.55, "noDot": True, **raw},
                                                 {"name": "7-day mean", "color": C2, "width": 3, **r7}], band=b, vlines=EVENT, digits=1, height=380))
    multi = []
    for sid, name, col in (("portwatch.hormuz.n_total", "Hormuz", C1), ("portwatch.bab_el_mandeb.n_total", "Bab el-Mandeb", C2),
                           ("portwatch.suez.n_total", "Suez", C3), ("portwatch.cape_good_hope.n_total", "Cape of Good Hope", CM)):
        p = _pts(con, sid, "2024-12-20", daily_gaps=True)
        if p:
            multi.append({"name": name, "color": col, **rolling7(p)})
    charts.append(chart("All vessels through four chokepoints (7-day mean)",
                        "Rerouting around the Cape lowers Suez and Bab el-Mandeb counts without changing total trade.",
                        "transits per day", multi, vlines=EVENT, digits=1))
    return {"id": "shipping", "title": "Chokepoint transits", "lede": "Daily counts from IMF PortWatch. Ships that switch off AIS transmitters are not counted.", "charts": charts}


def section_fuel(con):
    return {"id": "fuel", "title": "German fuel prices (weekly)",
            "lede": "EU Weekly Oil Bulletin: Monday price snapshots, published later in the week. Converted from EUR per 1,000 litres to EUR per litre.",
            "charts": [
                chart("Diesel: with and without taxes", "Germany · EUR per litre", "EUR per litre",
                      [_line(con, "oil_bulletin.DE.diesel.with_tax", "incl. taxes", C1, "2024-01-01"),
                       _line(con, "oil_bulletin.DE.diesel.without_tax", "excl. taxes", C2, "2024-01-01")], vlines=EVENT, digits=3),
                chart("Euro-super 95: with and without taxes", "Germany · EUR per litre", "EUR per litre",
                      [_line(con, "oil_bulletin.DE.euro95.with_tax", "incl. taxes", C1, "2024-01-01"),
                       _line(con, "oil_bulletin.DE.euro95.without_tax", "excl. taxes", C2, "2024-01-01")], vlines=EVENT, digits=3),
                chart("Diesel cost wedge", "Pretax pump price minus last week's average Brent per litre. Covers refining, transport, retail costs and margins — not a profit margin.",
                      "EUR per litre", [_line(con, "derived.cost_wedge.DE.diesel", "cost wedge", C1, "2024-01-01")], vlines=EVENT, digits=3),
            ]}


def section_cards(con):
    df = con.execute("SELECT card_id, version, kind, relationship, signal_strength, payload FROM evidence_cards "
                     "WHERE status='current' ORDER BY created_at DESC").df()
    out = []
    for r in df.itertuples():
        p = json.loads(r.payload)
        ms = p["calculations"].get("measurements", [])
        sid0 = ms[0]["series_id"] if ms else ""
        if r.kind == "anomaly" and not any(k in sid0 for k in ("hormuz", "brent", "wti", "DE.", "EU.", "bab_el", "yanbu", "fujairah", "tk.", "crack")):
            continue
        m = [{k: x.get(k) for k in ("name", "unit", "latest_value", "latest_period", "previous_value", "previous_date", "pct_change", "digits")}
             | {"rule": (x.get("anomaly") or {}).get("rule_id"), "fired": (x.get("anomaly") or {}).get("fired"),
                "why": (x.get("anomaly") or {}).get("explanation")} for x in ms]
        out.append({"id": r.card_id, "v": int(r.version), "kind": r.kind, "rel": r.relationship, "sig": r.signal_strength, "q": p["question"],
                    "reason": p["calculations"].get("relationship_reason"), "temporal": p["calculations"].get("temporal_note"),
                    "headline": p.get("headline"), "m": m, "contrary": p.get("contrary_observations", []), "alts": p.get("alternative_explanations", []),
                    "lims": p.get("limitations", []), "quality": p.get("quality", {}), "follow": p.get("follow_up"), "related": p.get("related_reporting", [])})
    return out


def alerts(con):
    df = con.execute("SELECT a.series_id, s.name, a.rule_id, a.obs_end, a.explanation FROM anomalies a LEFT JOIN series s USING(series_id) "
                     "WHERE a.fired AND a.status='active' AND a.as_of IS NULL ORDER BY a.obs_end DESC").df()
    df = df.drop_duplicates(["series_id", "rule_id"])
    return [{"s": r.series_id, "n": r.name or r.series_id, "r": r.rule_id, "d": str(r.obs_end)[:10], "why": r.explanation[:260]} for r in df.itertuples()][:40]


def tiles(con):
    from .page import band_from, rolling7
    out = []
    raw = _pts(con, "portwatch.hormuz.n_tanker", "2024-12-20", daily_gaps=True)
    if raw:
        r7 = rolling7(raw)
        b = band_from(r7, "2025-01-01", "2025-12-31")
        i = max(k for k, v in enumerate(r7["y"]) if v is not None)
        out.append({"lab": "Hormuz tanker transits", "val": f"{r7['y'][i]:.1f}", "unit": "per day, 7-day mean",
                    "sub": f"2025 median {b['median']:.1f} · through {r7['x'][i]}" if b else f"through {r7['x'][i]}",
                    "chip": ["Far below normal", "c-crit"] if b and r7["y"][i] < b["y0"] else None})
    d, v = _last(con, "tk.panel.diesel.mean")
    if v is not None:
        out.append({"lab": "Diesel at the pump, live panel", "val": f"{v:.3f}", "unit": "EUR/L", "sub": f"Tankerkönig station panel · {d}", "chip": ["Live", "c-ok"]})
    d, v = _last(con, "eia.brent_spot")
    if v is not None:
        d0, v0 = _value_at_or_before(con, "eia.brent_spot", "2026-02-27")
        out.append({"lab": "Brent crude", "val": f"{v:.2f}", "unit": "USD/bbl", "sub": f"{d} · {v0:.2f} on {d0} (before 1 Mar)" if v0 else d, "chip": None})
    d, v = _last(con, "oil_bulletin.DE.diesel.with_tax")
    if v is not None:
        d0, v0 = _value_at_or_before(con, "oil_bulletin.DE.diesel.with_tax", "2026-02-23")
        out.append({"lab": "German diesel (weekly)", "val": f"{v:.3f}", "unit": "EUR/L", "sub": f"Monday {d} · {v0:.3f} on {d0}" if v0 else d, "chip": None})
    d, v = _last(con, "derived.crack.ulsd_ny")
    if v is not None:
        out.append({"lab": "US diesel refining margin", "val": f"{v:.1f}", "unit": "USD/bbl", "sub": f"NY Harbor ULSD − Brent · {d}", "chip": None})
    d, v = _last(con, "ecb.usd_per_eur")
    if v is not None:
        out.append({"lab": "Euro", "val": f"{v:.4f}", "unit": "USD per EUR", "sub": f"ECB reference rate · {d}", "chip": None})
    return out


def headlines(con):
    df = con.execute("SELECT title, publisher, url, COALESCE(published_at, discovered_at) t, published_at IS NULL disc FROM headlines ORDER BY t DESC LIMIT 40").df()
    return [{"title": r.title, "pub": r.publisher, "url": r.url, "t": str(r.t)[:16], "disc": bool(r.disc)} for r in df.itertuples()]


def satellite(con, state):
    from ..satellite.coverage import coverage_summary
    tested = any(p["connector"] == "cdse_download" and p["kind"] == "authenticated_download" and p["ok"] for p in state.probes())
    cov = []
    for a in ("hormuz_strait", "hormuz_west_approach", "gulf_of_oman_approach", "fujairah_anchorage"):
        x = coverage_summary(con, a, "Sentinel-1", 0.9, tested)
        cov.append({k: x.get(k) for k in ("aoi", "n_scenes", "n_scenes_covering_min_fraction", "distinct_usable_days", "median_gap_days",
                                          "max_gap_days", "median_catalogue_latency_h", "last_acquisition")})
    return cov


def health(state):
    paths = get_paths()
    rep_p = paths.reports / "connector_verification.json"
    if not rep_p.exists():
        return [], []
    rep = json.loads(rep_p.read_text())
    conn = [{"c": x["connector"], "s": x["status"], "cred": x["credential"], "p": ((x.get("last_probe") or {}).get("detail") or "")[:170]}
            for x in rep["connectors"] if not x["connector"].startswith("(excluded)")]
    excl = [x["connector"].replace("(excluded) ", "") + " — " + x["title"] for x in rep["connectors"] if x["connector"].startswith("(excluded)")]
    return conn, excl


EXTRA_SECTIONS = []  # populated by feature modules: functions (con) -> section dict or None


def collect(con, state) -> dict:
    secs = [section_market(con), section_shipping(con), section_fuel(con)]
    for fn in EXTRA_SECTIONS:
        try:
            s = fn(con)
        except Exception as e:  # noqa: BLE001 — one failing section must not blank the page
            s = {"id": fn.__name__, "title": fn.__name__, "lede": f"Section could not be built: {type(e).__name__}: {e}", "charts": []}
        if s:
            secs.append(s)
    conn, excl = health(state)
    s1 = get_paths().exports / "s1_hormuz_2026-09-25_candidates.png"
    digest = None
    try:
        from ..analysis import digest as dg
        digest = dg.build_digest(con)
    except Exception:  # noqa: BLE001
        digest = None
    world = None
    try:
        from .worldmap import world_map
        world = world_map(con)
    except Exception as e:  # noqa: BLE001 — the map must never blank the rest of the page
        world = {"error": f"{type(e).__name__}: {e}"}
    return {"world": world, "tiles": tiles(con), "alerts": alerts(con), "sections": secs, "cards": section_cards(con), "heads": headlines(con),
            "cov": satellite(con, state), "conn": conn, "excl": excl, "digest": digest, "_s1_image": str(s1) if s1.exists() else None}
