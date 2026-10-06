"""Command-line interface: `oco --help`."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .collectors import registry
from .settings import get_paths, sources_config


def _ctx(demo: bool):
    from .pipeline import make_context

    return make_context(get_paths(demo=demo))


@click.group()
@click.option("--demo", is_flag=True, help="Use the separate SYNTHETIC demo data directory (data_demo/).")
@click.pass_context
def main(c, demo):
    """Oil Crisis Observatory — zero-charge research tool (local only)."""
    c.obj = {"demo": demo}


# ------------------------------------------------------------------ health / verification
@main.command()
@click.option("--live", is_flag=True, help="Make ONE bounded permitted request per connector to verify it.")
@click.option("--connector", "connectors", multiple=True, help="Limit live probes to these connectors.")
@click.pass_context
def doctor(c, live, connectors):
    """Check configuration, credentials, budgets and (with --live) connector access."""
    from .verification import doctor as run

    ctx = _ctx(c.obj["demo"])
    r = run(ctx, live=live, only=list(connectors) or None)
    click.echo((ctx.paths.reports / "connector_verification.md").read_text())
    click.echo(f"Report: {ctx.paths.reports / 'connector_verification.md'}")


@main.command()
@click.pass_context
def status(c):
    """Source health: last attempt/success, latest observation, next expected release."""
    ctx = _ctx(c.obj["demo"])
    for h in ctx.state.health():
        res = json.loads(h["last_result"]) if h.get("last_result") else {}
        click.echo(f"{h['source']:<13} status={res.get('status', '-'):<12} latest_obs={str(h.get('latest_observation_at') or '-')[:10]:<10} "
                   f"last_success={str(h.get('last_success_at') or '-')[:16]:<16} next_release={str(h.get('next_expected_release') or '-')[:16]}")
        if h.get("last_error"):
            click.echo(f"{'':13} last_error: {h['last_error'][:160]}")


@main.group()
def connectors():
    """Inspect or reset connector state (stopped connectors need explicit human review)."""


@connectors.command("list")
@click.pass_context
def connectors_list(c):
    ctx = _ctx(c.obj["demo"])
    for k in ctx.policy.connectors:
        st = ctx.state.connector_status(k)
        click.echo(f"{k:<16} {st['status']:<8} {st.get('reason') or ''}")


@connectors.command("reset")
@click.argument("name")
@click.pass_context
def connectors_reset(c, name):
    """Re-enable a stopped/paused connector AFTER you reviewed why it stopped. Never enables paid routes."""
    ctx = _ctx(c.obj["demo"])
    ctx.policy.connector(name)
    ctx.state.reset_connector(name)
    click.echo(f"{name} reset to active. If the provider now requires payment, it will stop again on the next request.")


# ------------------------------------------------------------------ collection
def _print_results(res):
    for k, r in res.items():
        if k == "_analysis":
            click.echo(f"analysis: {json.dumps(r, default=str)[:600]}")
            continue
        click.echo(f"{k:<13} {r.status:<14} new={r.counts['new']} revised={r.counts['revised']} unchanged={r.counts['unchanged']} "
                   f"requests={r.n_requests} latest_obs={r.latest_observation} {r.message[:300]}")


@main.command()
@click.option("--source", "sources", multiple=True, help="Source key(s); default: all enabled economic + news sources.")
@click.option("--no-analyse", is_flag=True)
@click.pass_context
def refresh(c, sources, no_analyse):
    """Incremental update with a revision-overlap window."""
    from .pipeline import refresh as run

    if c.obj["demo"]:
        raise click.UsageError("demo data are synthetic fixtures; refresh only works on live data (omit --demo)")
    keys = list(sources) or [k for k in registry.ECONOMIC + registry.NEWS if sources_config()["sources"].get(k, {}).get("enabled", True)]
    _print_results(run(_ctx(False), keys, analyse_after=not no_analyse))


@main.command()
@click.option("--source", "sources", multiple=True, required=True)
@click.option("--days", type=int, default=None, help="For GDELT/FIRMS/CDSE: bounded lookback in days.")
@click.pass_context
def backfill(c, sources, days):
    """Bounded historical backfill (start dates in config/sources.yaml)."""
    from .pipeline import refresh as run

    if c.obj["demo"]:
        raise click.UsageError("backfill works on live data only")
    kw = {"days": days} if days else {}
    if days and "gdelt" in sources:
        kw["hours"] = days * 24
    _print_results(run(_ctx(False), list(sources), mode="backfill", **kw))


@main.command()
@click.option("--as-of", "as_of", default=None, help="Replay: use only data vintages known at this UTC time (ISO).")
@click.pass_context
def analyse(c, as_of):
    """Derived indicators, anomaly screening, thermal events and evidence cards."""
    from .pipeline import analyse as run

    ts = datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc) if as_of else None
    click.echo(json.dumps(run(_ctx(c.obj["demo"]), ts), indent=2, default=str))


@main.command()
@click.option("--series", required=True)
@click.option("--rule", required=True, type=click.Choice(["price_daily", "price_weekly", "shipping_7d"]))
@click.option("--start", required=True)
@click.option("--end", required=True)
@click.pass_context
def backtest(c, series, rule, start, end):
    """Alert frequency of a screening rule over a held-out period (NOT a real-time backtest)."""
    from .analysis.anomalies import backtest as bt
    from .storage.warehouse import open_snapshot

    con = open_snapshot(_ctx(c.obj["demo"]).paths)
    click.echo(json.dumps(bt(con, series, rule, start, end), indent=2, default=str))


@main.command("run-scheduler")
@click.option("--source", "sources", multiple=True)
@click.option("--tick", default=30, help="Seconds between schedule evaluations.")
@click.option("--max-cycles", type=int, default=None, help="Stop after N cycles (testing).")
@click.pass_context
def run_scheduler_cmd(c, sources, tick, max_cycles):
    """Foreground release-aware collector. Stop with Ctrl+C or `oco stop-scheduler`. Nothing runs after exit."""
    from .pipeline import run_scheduler

    if c.obj["demo"]:
        raise click.UsageError("the scheduler collects live data only")
    run_scheduler(_ctx(False), list(sources) or None, max_cycles=max_cycles, tick_seconds=tick, echo=click.echo)


@main.command("stop-scheduler")
def stop_scheduler_cmd():
    """Send SIGTERM to a running scheduler (via its PID file)."""
    from .pipeline import stop_scheduler

    click.echo(stop_scheduler(get_paths()))


# ------------------------------------------------------------------ tankerkoenig
@main.group()
def tankerkoenig():
    """Live German pump prices (Tankerkönig / MTS-K)."""


@tankerkoenig.command("build-panel")
def tk_build_panel():
    """One-time: choose a fixed panel of stations around the configured city centres."""
    from .collectors.tankerkoenig import TankerkoenigCollector

    p = TankerkoenigCollector(_ctx(False)).build_panel()
    from collections import Counter
    click.echo(f"panel with {len(p['stations'])} stations: {dict(Counter(s['city'] for s in p['stations']))}")


# ------------------------------------------------------------------ news
@main.group()
def news():
    """News feed admission and manual headlines."""


@news.command("verify-feeds")
@click.pass_context
def verify_feeds(c):
    """One bounded fetch per candidate RSS feed; admit only working anonymous feeds."""
    from .collectors.news import RSSCollector

    for r in RSSCollector(_ctx(False)).verify_feeds():
        click.echo(f"{'ADMITTED' if r['admitted'] else 'not admitted':<13} {r['url']}  {r['detail'][:120]}")


@news.command("add")
@click.option("--url", required=True)
@click.option("--title", required=True)
@click.option("--published", default=None, help="Publication time as stated by the publisher (ISO), if known.")
@click.option("--publisher", default=None)
@click.pass_context
def news_add(c, url, title, published, publisher):
    """Paste a headline manually (works when discovery APIs are unavailable). Nothing is fetched."""
    from .collectors.news import add_manual_headline
    from .storage.warehouse import Warehouse

    pub = datetime.fromisoformat(published).replace(tzinfo=timezone.utc) if published else None
    ctx = _ctx(c.obj["demo"])
    with Warehouse.writer(ctx.paths) as wh:
        hid = add_manual_headline(wh, url, title, pub, publisher)
    click.echo(f"stored headline {hid}; run `oco analyse` to build its evidence card (card id h-{hid})")


# ------------------------------------------------------------------ cards & export
@main.command()
@click.option("--kind", default=None)
@click.pass_context
def cards(c, kind):
    """List current evidence cards."""
    from .storage.warehouse import open_snapshot

    con = open_snapshot(_ctx(c.obj["demo"]).paths)
    if con is None:
        raise click.ClickException("no snapshot yet — run refresh/analyse first")
    q = "SELECT card_id, version, kind, relationship, signal_strength, question FROM evidence_cards WHERE status='current'"
    args = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    for r in con.execute(q + " ORDER BY created_at DESC", args).fetchall():
        click.echo(f"{r[0]:<26} v{r[1]} {r[2]:<8} {r[3]:<22} {r[4]:<6} {r[5][:90]}")


@main.command()
@click.option("--card-id", default=None)
@click.option("--series", default=None, help="Comma-separated series ids for a chart export.")
@click.option("--title", default=None)
@click.option("--start", default=None)
@click.option("--event-date", default=None)
@click.option("--baseline", default=None, help="start,end")
@click.pass_context
def export(c, card_id, series, title, start, event_date, baseline):
    """16:9 PNG/SVG + CSV + source note for a card or a set of series."""
    from .exports import export_card, export_series
    from .storage.warehouse import open_snapshot

    ctx = _ctx(c.obj["demo"])
    con = open_snapshot(ctx.paths)
    if con is None:
        raise click.ClickException("no snapshot yet")
    if card_id:
        click.echo(json.dumps(export_card(con, card_id, ctx.paths.exports, demo=ctx.paths.demo), indent=2))
    elif series:
        b = tuple(baseline.split(",")) if baseline else None
        click.echo(json.dumps(export_series(con, series.split(","), ctx.paths.exports, title or series, start=start,
                                            event_date=event_date, baseline=b, demo=ctx.paths.demo), indent=2))
    else:
        raise click.UsageError("give --card-id or --series")


@main.command()
@click.pass_context
def digest(c):
    """Print the story finder (and save it to data/reports/story_finder.md)."""
    from .analysis.digest import build_digest, digest_markdown
    from .storage.warehouse import open_snapshot

    ctx = _ctx(c.obj["demo"])
    md = digest_markdown(build_digest(open_snapshot(ctx.paths)))
    (ctx.paths.reports / "story_finder.md").write_text(md, encoding="utf-8")
    click.echo(md)


# ------------------------------------------------------------------ self-contained page
@main.command("build-page")
@click.option("--out", default=None, help="Output HTML path (default data/exports/observatory.html).")
@click.pass_context
def build_page_cmd(c, out):
    """Write a fully self-contained HTML research page (charts work offline; no external requests)."""
    from .storage.warehouse import open_snapshot
    from .web.page import build_page

    ctx = _ctx(c.obj["demo"])
    con = open_snapshot(ctx.paths)
    if con is None:
        raise click.ClickException("no snapshot yet — run `oco refresh` first")
    p = build_page(con, ctx.state, Path(out) if out else ctx.paths.exports / "observatory.html", demo=ctx.paths.demo)
    click.echo(f"wrote {p} ({p.stat().st_size/1e6:.2f} MB). Open it in any browser; it needs no internet connection.")


# ------------------------------------------------------------------ live ships
@main.command()
@click.option("--area", "areas", multiple=True, help="Area preset from config/sources.yaml (ais_live.areas) or 'world'. Repeatable.")
@click.option("--port", default=8765, show_default=True)
@click.option("--lan", is_flag=True, help="Also reachable from your phone on the same Wi-Fi (http://<computer IP>:PORT).")
@click.option("--no-browser", is_flag=True, help="Do not open the map in your browser.")
@click.pass_context
def ships(c, areas, port, lan, no_browser):
    """Live ship positions on a map (needs your own free AISSTREAM_API_KEY). Runs until Ctrl+C."""
    from .live.server import serve

    if c.obj["demo"]:
        raise click.ClickException("live ships have no demo mode: they are either live or not shown")
    ctx = _ctx(False)
    def open_browser():
        if not no_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
    try:
        serve(ctx, list(areas), port=port, lan=lan, echo=click.echo, on_ready=open_browser)
    except (RuntimeError, KeyError) as e:
        raise click.ClickException(str(e))


@main.command()
@click.option("--port", default=8501)
@click.pass_context
def dashboard(c, port):
    """Start the local Streamlit dashboard (reads the published snapshot; Ctrl+C to stop)."""
    app = Path(__file__).parent / "dashboard" / "app.py"
    args = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(port), "--server.address", "127.0.0.1",
            "--browser.gatherUsageStats", "false", "--client.toolbarMode", "minimal",
            "--server.headless", "true", "--", *(["--demo"] if c.obj["demo"] else [])]
    subprocess.run(args, check=False)


# ------------------------------------------------------------------ satellite
@main.group()
def satellite():
    """Coverage reports, bounded raw downloads, local S1/S2 processing, CEMS import."""


@satellite.command("coverage")
@click.option("--aoi", default=None)
@click.option("--min-fraction", default=0.9)
@click.pass_context
def sat_coverage(c, aoi, min_fraction):
    from . import geo
    from .satellite.coverage import coverage_summary
    from .storage.warehouse import open_snapshot

    ctx = _ctx(c.obj["demo"])
    con = open_snapshot(ctx.paths)
    if con is None:
        raise click.ClickException("no snapshot yet — run `oco refresh --source cdse`")
    tested = any(p["connector"] == "cdse_download" and p["kind"] == "authenticated_download" and p["ok"] for p in ctx.state.probes())
    ids = [aoi] if aoi else [r["properties"]["id"] for r in geo.regions()] + [f["properties"]["id"] for f in geo.facilities(True)]
    for a in ids:
        for m in ("Sentinel-1", "Sentinel-2"):
            click.echo(json.dumps(coverage_summary(con, a, m, min_fraction, tested), default=str))


@satellite.command("download")
@click.option("--product-id", "product_ids", multiple=True, required=True)
@click.pass_context
def sat_download(c, product_ids):
    """Queue + run bounded raw downloads (needs YOUR free CDSE login in .env)."""
    from .satellite.download import process_queue, queue_product
    from .storage.warehouse import Warehouse

    ctx = _ctx(False)
    for p in product_ids:
        queue_product(ctx, p, "manual request")
    with Warehouse.writer(ctx.paths) as wh:
        for r in process_queue(ctx, wh, limit=len(product_ids)):
            click.echo(json.dumps(r))


@satellite.command("detect-s1")
@click.option("--product-id", required=True)
@click.option("--aoi", required=True)
@click.pass_context
def sat_detect(c, product_id, aoi):
    """EXPERIMENTAL candidate-vessel snapshot count for one downloaded GRD product."""
    from .satellite.run import run_s1
    from .storage.warehouse import Warehouse

    ctx = _ctx(False)
    with Warehouse.writer(ctx.paths) as wh:
        click.echo(json.dumps(run_s1(ctx, wh, product_id, aoi), indent=2, default=str))


@satellite.command("compare-s1")
@click.option("--aoi", required=True)
@click.option("--before", required=True)
@click.option("--after", required=True)
@click.pass_context
def sat_compare_s1(c, aoi, before, after):
    from .satellite.run import snapshot_comparison
    from .storage.warehouse import open_snapshot

    click.echo(json.dumps(snapshot_comparison(open_snapshot(_ctx(False).paths), aoi, before, after), indent=2, default=str))


@satellite.command("compare-s2")
@click.option("--facility", required=True)
@click.option("--pre", "pre", multiple=True, required=True)
@click.option("--post", required=True)
@click.option("--event-date", required=True)
@click.pass_context
def sat_compare_s2(c, facility, pre, post, event_date):
    from .satellite.run import run_s2
    from .storage.warehouse import Warehouse

    ctx = _ctx(False)
    with Warehouse.writer(ctx.paths) as wh:
        click.echo(json.dumps(run_s2(ctx, wh, facility, list(pre), post, event_date), indent=2, default=str))


@satellite.command("review-detection")
@click.argument("detection_id")
@click.option("--confirm/--reject", default=None, required=True)
@click.pass_context
def sat_review(c, detection_id, confirm):
    from .satellite.s1_detect import precision_report
    from .storage.warehouse import Warehouse

    ctx = _ctx(False)
    with Warehouse.writer(ctx.paths) as wh:
        wh.con.execute("UPDATE candidate_detections SET review_status=? WHERE detection_id=?",
                       ["confirmed" if confirm else "rejected", detection_id])
        click.echo(json.dumps(precision_report(wh.con), indent=2))


@satellite.command("cems-import")
@click.option("--file", "file", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--activation", required=True, help="e.g. EMSR123")
@click.option("--title", required=True)
@click.option("--product-date", required=True)
@click.option("--licence", required=True)
@click.option("--source-url", required=True)
@click.pass_context
def sat_cems(c, file, activation, title, product_date, licence, source_url):
    """Import an ALREADY-PUBLISHED Copernicus EMS product you downloaded yourself."""
    from .satellite.run import cems_import
    from .storage.warehouse import Warehouse

    ctx = _ctx(False)
    with Warehouse.writer(ctx.paths) as wh:
        click.echo(cems_import(ctx, wh, file, activation, title, product_date, licence, source_url))


# ------------------------------------------------------------------ demo
@main.command("demo-build")
def demo_build():
    """Build SYNTHETIC, visibly labelled fixtures in data_demo/ (never mixed with live data)."""
    from .demo import build

    p = build()
    click.echo(f"SYNTHETIC demo data written to {p}. View with: oco --demo dashboard")


if __name__ == "__main__":
    main()
