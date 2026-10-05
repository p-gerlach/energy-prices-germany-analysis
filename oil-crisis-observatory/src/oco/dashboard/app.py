"""Oil Crisis Observatory — local Streamlit dashboard.

Reads ONLY the published read-only snapshot (data/snapshot.duckdb). Review decisions go to a separate
SQLite file. No map tiles, geocoders or remote services: maps are drawn on a blank lon/lat plane from
local GeoJSON. Run with `oco dashboard` (or `oco --demo dashboard` for SYNTHETIC fixtures).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from oco import geo  # noqa: E402
from oco.analysis.anomalies import evaluate_shipping  # noqa: E402
from oco.exports import export_card, export_series, render_card_markdown  # noqa: E402
from oco.satellite.coverage import coverage_summary, scene_table  # noqa: E402
from oco.settings import get_paths, sources_config  # noqa: E402
from oco.storage.review import ReviewStore  # noqa: E402
from oco.storage.state import StateStore  # noqa: E402
from oco.storage.warehouse import observations, open_snapshot  # noqa: E402

DEMO = "--demo" in sys.argv
BERLIN = ZoneInfo("Europe/Berlin")
COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
st.set_page_config(page_title="Oil Crisis Observatory" + (" — DEMO" if DEMO else ""), layout="wide")

paths = get_paths(demo=DEMO)
review = ReviewStore(paths.review_db)
state = StateStore(paths.jobs_db)


@st.cache_resource(ttl=60)
def con_cached(mtime: float):
    return open_snapshot(paths)


con = con_cached(paths.snapshot.stat().st_mtime if paths.snapshot.exists() else 0)
if con is None:
    st.error("No published snapshot yet. Run `oco refresh` (live data) or `oco demo-build` (synthetic) first.")
    st.stop()
mode = (con.execute("SELECT value FROM meta WHERE key='mode'").fetchone() or [None])[0]
if DEMO or mode == "DEMO_SYNTHETIC":
    st.error("SYNTHETIC DEMONSTRATION DATA — NOT LIVE. Nothing on these pages describes real markets.", icon="⚠️")


def berlin(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or ts is pd.NaT:
        return "—"
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(BERLIN).strftime("%Y-%m-%d %H:%M %Z")


def series_list(where: str = "1=1") -> pd.DataFrame:
    return con.execute(f"SELECT series_id, name, unit, frequency, source FROM series WHERE {where} ORDER BY series_id").df()


def line_chart(sids: list[str], title: str, start=None, event=None, baseline=None, rolling7=False):
    fig = go.Figure()
    units = set()
    for i, sid in enumerate(sids):
        df = observations(con, sid)
        if df.empty:
            continue
        info = con.execute("SELECT name, unit, frequency FROM series WHERE series_id=?", [sid]).fetchone()
        units.add(info[1])
        if start is not None:
            df = df[df["obs_end"] >= pd.Timestamp(start)]
        s = df.set_index("obs_end")["value"]
        if info[2] == "daily":
            s = s.asfreq("D")
        fig.add_trace(go.Scatter(x=s.index, y=s.values, name=info[0], mode="lines", line=dict(width=1.5 if rolling7 else 2, color=COLORS[i % 3]),
                                 opacity=0.6 if rolling7 else 1, connectgaps=False,
                                 hovertemplate="%{x|%Y-%m-%d}: %{y:,.3f}<extra>" + info[0][:40] + "</extra>"))
        if rolling7:
            r = s.rolling(7, min_periods=6).mean()
            fig.add_trace(go.Scatter(x=r.index, y=r.values, name="7-day mean (≥6 observed days)", line=dict(width=3, color="#eb6834")))
    if len(units) > 1:
        st.warning(f"Different units {units}: shown separately below instead of on one axis.")
        for sid in sids:
            line_chart([sid], sid, start, event, baseline, rolling7)
        return
    if baseline:
        fig.add_vrect(x0=baseline[0], x1=baseline[1], fillcolor="#8a8984", opacity=0.12, line_width=0, annotation_text="baseline")
    if event:
        fig.add_vline(x=pd.Timestamp(event), line_dash="dash", line_color="#52514e")
    fig.update_layout(title=title, height=380, margin=dict(l=10, r=10, t=40, b=10), yaxis_title=next(iter(units), ""),
                      hovermode="x unified", legend=dict(orientation="h", y=-0.15), plot_bgcolor="#fcfcfb")
    st.plotly_chart(fig, use_container_width=True)


def export_button(sids, title, key, **kw):
    if st.button("Export 16:9 PNG/SVG + CSV + source note", key=key):
        try:
            out = export_series(con, sids, paths.exports, title, demo=DEMO or mode == "DEMO_SYNTHETIC", **kw)
            st.success(f"Exported to {Path(out['png']).parent}")
            st.image(out["png"])
        except Exception as e:  # noqa: BLE001
            st.error(f"{type(e).__name__}: {e}")


tabs = st.tabs(["What changed", "Market", "Shipping", "Fuel prices", "News & Evidence", "Satellite review", "Source health"])

# ------------------------------------------------------------------------------------- What changed
with tabs[0]:
    if "prev_visit" not in st.session_state:
        st.session_state.prev_visit = review.get("last_visit", "1970-01-01T00:00:00+00:00")
    pv = datetime.fromisoformat(st.session_state.prev_visit)
    c1, c2 = st.columns([3, 1])
    c1.subheader(f"Since your last visit ({berlin(pv)})")
    if c2.button("Mark everything as seen"):
        review.set("last_visit", datetime.now(timezone.utc).isoformat())
        st.session_state.prev_visit = review.get("last_visit")
        st.rerun()
    new_obs = con.execute("SELECT s.name, COUNT(*) n, MAX(o.obs_end) latest FROM observation_versions o JOIN series s USING(series_id) "
                          "WHERE o.first_seen_at > ? AND o.revision_no = 1 GROUP BY s.name ORDER BY latest DESC", [pv]).df()
    revised = con.execute("SELECT s.name, o.obs_start, o.value, o.revision_no, o.first_seen_at FROM observation_versions o JOIN series s USING(series_id) "
                          "WHERE o.first_seen_at > ? AND o.revision_no > 1 ORDER BY o.first_seen_at DESC LIMIT 200", [pv]).df()
    heads = con.execute("SELECT title, publisher, COALESCE(published_at, discovered_at) t, CASE WHEN published_at IS NULL THEN 'discovered' ELSE 'published' END basis, url "
                        "FROM headlines WHERE discovered_at > ? ORDER BY t DESC LIMIT 100", [pv]).df()
    alerts = con.execute("SELECT a.series_id, a.obs_end, a.explanation FROM anomalies a WHERE a.fired AND a.status='active' AND a.created_at > ? "
                         "AND a.as_of IS NULL ORDER BY a.severity_rank DESC LIMIT 30", [pv]).df()
    a, b, c, d = st.columns(4)
    a.metric("New measurements", int(new_obs["n"].sum()) if not new_obs.empty else 0)
    b.metric("Revised old values", len(revised))
    c.metric("New headlines", len(heads))
    d.metric("New screening alerts", len(alerts))
    st.markdown("**New measurements** (first vintage)")
    st.dataframe(new_obs, use_container_width=True, hide_index=True)
    st.markdown("**Revisions of previously published values** (history retained)")
    st.dataframe(revised, use_container_width=True, hide_index=True)
    st.markdown("**New screening alerts** (screening rules, not conclusions)")
    st.dataframe(alerts, use_container_width=True, hide_index=True)
    st.markdown("**New headlines** (untrusted text; time basis shown)")
    st.dataframe(heads, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------------------------- Market
with tabs[1]:
    st.subheader("Crude prices, euro conversion and US inventories")
    days = st.slider("Days shown", 30, 3650, 365, key="mk_days")
    start = date.today() - timedelta(days=days)
    line_chart(["eia.brent_spot", "eia.wti_spot"], "Brent and WTI spot (USD/bbl)", start)
    line_chart(["derived.brent_eur_per_barrel"], "Brent in euros (EIA ÷ ECB same-date rate)", start)
    export_button(["eia.brent_spot", "eia.wti_spot"], "Brent and WTI spot prices", "ex_mk", start=str(start))
    inv = series_list("source='EIA' AND frequency='weekly'")
    if not inv.empty:
        pick = st.selectbox("Weekly US series (EIA — US evidence only)", inv["series_id"], format_func=lambda s: inv.set_index("series_id").loc[s, "name"])
        line_chart([pick], inv.set_index("series_id").loc[pick, "name"], start)
        st.caption("Strategic (SPR) and commercial stocks are separate series. 'Product supplied' is a demand proxy, not consumption.")
    jodi = series_list("source='JODI'")
    if not jodi.empty:
        pj = st.selectbox("JODI monthly series (no interpolation to daily)", jodi["series_id"])
        line_chart([pj], pj)

# ------------------------------------------------------------------------------------- Shipping
with tabs[2]:
    st.subheader("Chokepoint transits (IMF PortWatch, daily AIS transit calls)")
    ship = series_list("source='IMF PortWatch'")
    if ship.empty:
        st.info("No PortWatch data yet (`oco refresh --source portwatch`).")
    else:
        sid = st.selectbox("Series", ship["series_id"], index=int((ship["series_id"] == "portwatch.hormuz.n_tanker").idxmax()) if (ship["series_id"] == "portwatch.hormuz.n_tanker").any() else 0)
        bc = sources_config().get("shipping_baseline", {})
        c1, c2, c3 = st.columns(3)
        b0 = c1.date_input("Baseline start (fixed, pre-event)", date.fromisoformat(bc.get("start", "2025-01-01")))
        b1 = c2.date_input("Baseline end", date.fromisoformat(bc.get("end", "2025-12-31")))
        ev = c3.date_input("Event date (marker)", value=None)
        df = observations(con, sid)
        line_chart([sid], ship.set_index("series_id").loc[sid, "name"], date.today() - timedelta(days=540), ev, (b0, b1), rolling7=True)
        res = evaluate_shipping(df, sid, sources_config()["anomaly_rules"]["shipping"], {"start": str(b0), "end": str(b1)}, last_n=14)
        if res:
            st.dataframe(pd.DataFrame([{"window end": r["obs_end"], "7d mean": r["value"], "fired": r["fired"], "why": r["explanation"]} for r in res]),
                         use_container_width=True, hide_index=True)
        latest = df.dropna(subset=["value"])["obs_end"].max() if not df.empty else None
        st.caption(f"Latest observed date: {latest.date() if latest is not None else '—'} (provider lag; no value is inferred for later days). "
                   "Tanker transits are vessel counts, not barrels; capacity is deadweight, not cargo. Missing days are gaps, not zeros.")
        export_button([sid], ship.set_index("series_id").loc[sid, "name"], "ex_sh", start=str(date.today() - timedelta(days=540)),
                      baseline=(str(b0), str(b1)), event_date=str(ev) if ev else None)
    st.markdown("**Radar snapshot occupancy (Sentinel-1, EXPERIMENTAL) — separate axis and unit**")
    snaps = con.execute("SELECT * FROM snapshot_counts ORDER BY acquisition_start").df()
    if snaps.empty:
        st.caption("No processed Sentinel-1 snapshots. These are instantaneous counts per acquisition — never daily transits.")
    else:
        fig = go.Figure()
        for i, (g, gdf) in enumerate(snaps.groupby("comparable_group")):
            fig.add_trace(go.Scatter(x=gdf["acquisition_start"], y=gdf["candidate_count"], mode="markers", name=f"group {g}", marker=dict(size=10, color=COLORS[i % 3])))
        fig.update_layout(height=300, yaxis_title="candidate vessels in one snapshot (count)", plot_bgcolor="#fcfcfb")
        st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------------------------------- Fuel prices
with tabs[3]:
    st.subheader("EU Weekly Oil Bulletin (Monday price snapshots, published later)")
    ob = series_list("source='EU Oil Bulletin'")
    if ob.empty:
        st.info("No Oil Bulletin data yet (`oco refresh --source oil_bulletin`).")
    else:
        ccs = sorted({s.split(".")[1] for s in ob["series_id"]})
        prods = sorted({s.split(".")[2] for s in ob["series_id"]})
        c1, c2 = st.columns(2)
        cc = c1.selectbox("Country", ccs, index=ccs.index("DE") if "DE" in ccs else 0)
        pr = c2.selectbox("Product", prods, index=prods.index("diesel") if "diesel" in prods else 0)
        start = date.today() - timedelta(days=3 * 365)
        line_chart([f"oil_bulletin.{cc}.{pr}.with_tax", f"oil_bulletin.{cc}.{pr}.without_tax"], f"{cc} {pr}: gross vs pretax (EUR/L)", start)
        line_chart([f"derived.oil_bulletin.{cc}.{pr}.taxes"], f"{cc} {pr}: taxes & duties component", start)
        if pr in ("diesel", "euro95"):
            line_chart([f"derived.cost_wedge.{cc}.{pr}"], f"{cc} {pr}: pretax retail − prior-week Brent (COST WEDGE, not profit margin)", start)
        st.caption("Alignment: bulletin Monday price D vs mean Brent EUR/L over D−7..D−3. A crude-to-retail spread covers refining, logistics, "
                   "storage, retail costs, biofuel obligations and margins — it is not a measured profit margin.")
        export_button([f"oil_bulletin.{cc}.{pr}.with_tax", f"oil_bulletin.{cc}.{pr}.without_tax"], f"{cc} {pr} retail price gross vs pretax", "ex_fp", start=str(start))

# ------------------------------------------------------------------------------------- News & evidence
with tabs[4]:
    st.subheader("Evidence inbox")
    cards = con.execute("SELECT card_id, version, kind, relationship, signal_strength, geography, topics, question, payload, created_at "
                        "FROM evidence_cards WHERE status='current' ORDER BY created_at DESC").df()
    rv = review.all_cards()
    if cards.empty:
        st.info("No evidence cards yet.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        kinds = c1.multiselect("Kind", sorted(cards["kind"].unique()), default=sorted(cards["kind"].unique()))
        rels = c2.multiselect("Relationship", sorted(cards["relationship"].unique()), default=sorted(cards["relationship"].unique()))
        sig = c3.multiselect("Signal", sorted(cards["signal_strength"].unique()), default=sorted(cards["signal_strength"].unique()))
        status_f = c4.selectbox("Review status", ["open (not dismissed)", "pinned", "dismissed", "all"])
        topics_all = sorted({t for ts in cards["topics"] for t in json.loads(ts or "[]")})
        topic_f = st.multiselect("Topic", topics_all)
        geo_f = st.text_input("Geography contains")
        f = cards[cards["kind"].isin(kinds) & cards["relationship"].isin(rels) & cards["signal_strength"].isin(sig)]
        if topic_f:
            f = f[f["topics"].map(lambda ts: bool(set(json.loads(ts or "[]")) & set(topic_f)))]
        if geo_f:
            f = f[f["geography"].str.contains(geo_f, case=False, na=False)]
        if status_f == "pinned":
            f = f[f["card_id"].map(lambda c: rv.get(c, {}).get("pinned", False))]
        elif status_f == "dismissed":
            f = f[f["card_id"].map(lambda c: rv.get(c, {}).get("decision") == "dismissed")]
        elif status_f.startswith("open"):
            f = f[f["card_id"].map(lambda c: rv.get(c, {}).get("decision") != "dismissed")]
        for row in f.itertuples():
            r = rv.get(row.card_id, {})
            updated = r.get("reviewed_version") and r["reviewed_version"] < row.version
            label = f"{'📌 ' if r.get('pinned') else ''}[{row.kind}] {row.relationship} · {row.question[:110]}" + ("  (UPDATED since your review)" if updated else "")
            with st.expander(label):
                card = json.loads(row.payload)
                st.markdown(render_card_markdown(card, row.version, DEMO or mode == "DEMO_SYNTHETIC"))
                for ch in card.get("charts", [])[:2]:
                    line_chart([ch["series_id"]], ch["series_id"], date.today() - timedelta(days=180), ch.get("event_date"))
                cc1, cc2, cc3, cc4 = st.columns(4)
                if cc1.button("Pin" if not r.get("pinned") else "Unpin", key=f"pin{row.card_id}"):
                    review.set_card(row.card_id, row.version, pinned=not r.get("pinned"))
                    st.rerun()
                if cc2.button("Dismiss", key=f"dis{row.card_id}"):
                    review.set_card(row.card_id, row.version, decision="dismissed")
                    st.rerun()
                if cc3.button("Mark reviewed", key=f"rev{row.card_id}"):
                    review.set_card(row.card_id, row.version, decision="reviewed")
                    st.rerun()
                if cc4.button("Export card", key=f"exp{row.card_id}"):
                    out = export_card(con, row.card_id, paths.exports, demo=DEMO or mode == "DEMO_SYNTHETIC")
                    st.success(f"Exported to {out['dir']}")
                note = st.text_area("Your annotation", value=r.get("note") or "", key=f"note{row.card_id}")
                if st.button("Save annotation", key=f"save{row.card_id}"):
                    review.set_card(row.card_id, row.version, note=note)
                    st.success("saved")
    st.markdown("---")
    st.markdown("**Add a headline manually**: `oco news add --url URL --title \"…\" [--published ISO]` then `oco analyse`.")
    heads = con.execute("SELECT title, publisher, published_at, discovered_at, discovery_source, url FROM headlines ORDER BY discovered_at DESC LIMIT 300").df()
    st.dataframe(heads, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------------------------- Satellite
with tabs[5]:
    st.subheader("Satellite review (coverage first; all processing local)")
    tested = any(p["connector"] == "cdse_download" and p["kind"] == "authenticated_download" and p["ok"] for p in state.probes())
    regs = geo.regions()
    facs = geo.facilities()
    fig = go.Figure()
    for i, r in enumerate(regs):
        ring = r["geometry"]["coordinates"][0]
        fig.add_trace(go.Scatter(x=[p[0] for p in ring], y=[p[1] for p in ring], mode="lines", name=r["properties"]["id"],
                                 line=dict(color=COLORS[i % 3])))
    for f in facs:
        if f.get("geometry"):
            w, s, e, n = geo.bbox(f["geometry"])
            fig.add_trace(go.Scatter(x=[w, e, e, w, w], y=[s, s, n, n, s], mode="lines", name=f["properties"]["id"]))
    fig.update_layout(height=380, xaxis_title="longitude", yaxis_title="latitude", plot_bgcolor="#fcfcfb",
                      yaxis=dict(scaleanchor="x"), title="Study polygons on a blank lon/lat plane (no map tiles)")
    st.plotly_chart(fig, use_container_width=True)
    unverified = [f["properties"]["name"] for f in facs if not f["properties"].get("verified")]
    if unverified:
        st.warning("Facilities without a verified boundary (no thermal/imagery analysis is run for them): " + ", ".join(unverified))
    aoi_ids = [r["properties"]["id"] for r in regs] + [f["properties"]["id"] for f in facs if f["properties"].get("verified")]
    aoi = st.selectbox("Area", aoi_ids)
    for m in ("Sentinel-1", "Sentinel-2"):
        st.json(coverage_summary(con, aoi, m, 0.9, tested), expanded=False)
    stab = scene_table(con, aoi)
    st.dataframe(stab, use_container_width=True, hide_index=True)
    st.markdown("**Thermal events (FIRMS) — review items, not damage findings**")
    te = con.execute("SELECT facility_id, start_at, n_detections, n_overpasses, sensors, daynight, max_frp, routine_flare_share, classification "
                     "FROM thermal_events ORDER BY start_at DESC").df()
    st.dataframe(te, use_container_width=True, hide_index=True)
    if not te.empty:
        fig = go.Figure()
        for i, (fid, g) in enumerate(te.groupby("facility_id")):
            fig.add_trace(go.Scatter(x=g["start_at"], y=g["max_frp"], mode="markers", name=fid, text=g["classification"], marker=dict(color=COLORS[i % 3], size=9)))
        fig.update_layout(height=280, yaxis_title="max FRP per event (MW)", title="Thermal timeline", plot_bgcolor="#fcfcfb")
        st.plotly_chart(fig, use_container_width=True)
    st.markdown("**Before/after comparisons (Sentinel-2)**")
    comps = con.execute("SELECT * FROM comparisons ORDER BY created_at DESC").df()
    if comps.empty:
        st.caption("No comparisons yet. Requires a verified facility, downloaded L2A scenes and `oco satellite compare-s2`.")
    for c in comps.itertuples():
        outs = json.loads(c.outputs or "{}")
        st.markdown(f"**{c.facility_or_aoi}** event {c.event_date} — valid: **{c.valid}** {json.loads(c.reasons or '[]')}")
        if outs.get("png") and Path(outs["png"]).exists():
            st.image(outs["png"])
    det = con.execute("SELECT * FROM candidate_detections ORDER BY created_at DESC LIMIT 500").df()
    if not det.empty:
        st.markdown("**SAR candidate detections (EXPERIMENTAL, unreviewed unless marked)**")
        st.dataframe(det, use_container_width=True, hide_index=True)
    ap = con.execute("SELECT * FROM assessment_products").df()
    if not ap.empty:
        st.markdown("**Imported public assessment products (CEMS)**")
        st.dataframe(ap, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------------------------- Source health
with tabs[6]:
    st.subheader("Source health")
    h = pd.DataFrame(state.health())
    runs = con.execute("SELECT source, COUNT(*) runs, SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok_runs, MAX(finished_at) last_run "
                       "FROM source_runs GROUP BY source").df()
    if not h.empty:
        cfg = sources_config()["sources"]
        h["observation_age_days"] = h["latest_observation_at"].map(lambda d: (date.today() - date.fromisoformat(d[:10])).days if d else None)
        h["stale_after_days"] = h["source"].map(lambda s: cfg.get(s, {}).get("stale_after_days"))
        h["stale"] = h.apply(lambda r: (r["observation_age_days"] or 0) > (r["stale_after_days"] or 10**6) if r["observation_age_days"] is not None else None, axis=1)
        for col in ("last_attempt_at", "last_success_at", "next_expected_release", "next_due_at"):
            h[col] = h[col].map(berlin)
        h["connector_state"] = h["source"].map(lambda s: state.connector_status(cfg.get(s, {}).get("connector", s))["status"])
        st.dataframe(h.merge(runs, on="source", how="left")[["source", "connector_state", "latest_observation_at", "observation_age_days", "stale_after_days", "stale",
                                                             "last_success_at", "last_attempt_at", "next_expected_release", "next_due_at", "runs", "ok_runs", "last_error"]],
                     use_container_width=True, hide_index=True)
        st.caption("Observation age (how old the newest measurement is) and ingestion timing are separate. Monthly/weekly series are judged by their own stale thresholds.")
    rep = paths.reports / "connector_verification.md"
    if rep.exists():
        st.markdown(rep.read_text())
    else:
        st.info("Run `oco doctor --live` to create the connector verification report.")
    st.markdown("**Recent log**")
    st.dataframe(pd.DataFrame(state.recent_log(200)), use_container_width=True, hide_index=True)
