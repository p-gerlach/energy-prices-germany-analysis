"""Orchestration: refresh/backfill, analysis, and the foreground scheduler loop.

The scheduler is a normal foreground process. It does nothing once it exits, it installs no service,
and a sleeping computer does not collect. Start with `oco run-scheduler`, stop with Ctrl+C or
`oco stop-scheduler` (SIGTERM via the PID file).
"""
from __future__ import annotations

import os
import signal
import time
from datetime import date, datetime, timedelta

from .analysis import anomalies, cards, indicators
from .collectors import registry
from .collectors.base import Context, run_collector
from .schedule import next_due, next_expected_release
from .settings import Paths, sources_config
from .storage.state import StateStore, parse_iso, utcnow
from .storage.warehouse import Warehouse, WriterBusy


def make_context(paths: Paths) -> Context:
    return Context(paths=paths, state=StateStore(paths.jobs_db))


def refresh(ctx: Context, keys: list[str], mode: str = "refresh", analyse_after: bool = True, **kw) -> dict:
    results = {}
    analysed = False
    with Warehouse.writer(ctx.paths) as wh:
        for k in keys:
            col = registry.get(ctx, k)
            res = run_collector(ctx, col, mode=mode, wh=wh, **kw)
            results[k] = res
            cfg = sources_config()["sources"].get(k, {})
            nr = next_expected_release(k, cfg, utcnow())
            if nr:
                ctx.state.update_health(k, next_expected_release=nr)
        # analyse only when stored observations actually changed (e.g. not on every 10-minute pump poll)
        changed = any(r.status == "ok" and (r.counts["new"] or r.counts["revised"]) for r in results.values() if hasattr(r, "counts"))
        if analyse_after and changed:
            results["_analysis"] = analyse_wh(ctx, wh)
            analysed = True
    if analysed:
        rebuild_page(ctx)
    return results


def analyse_wh(ctx: Context, wh: Warehouse, as_of: datetime | None = None) -> dict:
    out = {}
    if as_of is None:
        out["derived"] = {k: v for k, v in indicators.compute_derived(wh).items()}
        from .analysis import extras
        out["extras"] = extras.compute_all(wh)
    out["anomalies"] = anomalies.run_anomalies(wh, as_of=as_of)
    if as_of is None:
        from .satellite import thermal
        out["thermal"] = thermal.build_thermal_events(wh)
        client = None
        from .settings import env
        if env("OCO_LOCAL_LLM_MODEL"):
            try:
                client = ctx.client("local_llm")
            except Exception:  # noqa: BLE001
                client = None
        out["cards"] = cards.build_cards(wh, ctx_client=client)
    return out


def rebuild_page(ctx: Context):
    """Regenerate the self-contained research page from the freshly published snapshot (never fatal)."""
    try:
        from .storage.warehouse import open_snapshot
        from .web.page import build_page
        con = open_snapshot(ctx.paths)
        if con is not None:
            build_page(con, ctx.state, ctx.paths.exports / "observatory.html", demo=ctx.paths.demo)
            con.close()
    except Exception as e:  # noqa: BLE001
        ctx.state.log(None, "WARN", f"research page not rebuilt: {type(e).__name__}: {e}")


def analyse(ctx: Context, as_of: datetime | None = None) -> dict:
    with Warehouse.writer(ctx.paths) as wh:
        out = analyse_wh(ctx, wh, as_of)
    if as_of is None:
        rebuild_page(ctx)
    return out


# ------------------------------------------------------------------------------------------
_stop = False


def _handle(sig, frame):  # noqa: ARG001
    global _stop
    _stop = True


def run_scheduler(ctx: Context, keys: list[str] | None = None, max_cycles: int | None = None, tick_seconds: int = 30,
                  sleep=time.sleep, now_fn=utcnow, echo=print) -> int:
    """Foreground loop. Returns number of collector runs executed."""
    global _stop
    _stop = False
    pidf = ctx.paths.scheduler_pid
    if pidf.exists():
        try:
            other = int(pidf.read_text().strip())
            os.kill(other, 0)
            raise RuntimeError(f"scheduler already running (pid {other}); stop it with `oco stop-scheduler`")
        except (ProcessLookupError, ValueError):
            pidf.unlink(missing_ok=True)
    pidf.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    srcs = sources_config()["sources"]
    keys = keys or [k for k, v in srcs.items() if v.get("enabled", True)]
    runs = 0
    cycles = 0
    echo(f"[scheduler] started pid {os.getpid()} for {keys}. Collection happens only while this process runs and the computer is awake.")
    try:
        while not _stop:
            now = now_fn()
            due = []
            for k in keys:
                h = ctx.state.health_for(k)
                lo = h.get("latest_observation_at")
                nd, why = next_due(srcs[k], now, parse_iso(h.get("last_attempt_at")), date.fromisoformat(lo[:10]) if lo else None)
                ctx.state.update_health(k, next_due_at=nd)
                if nd <= now:
                    st = ctx.state.connector_status(registry.COLLECTORS[k].connector)
                    if st["status"] == "stopped":
                        continue
                    due.append((k, why))
            if due:
                try:
                    res = refresh(ctx, [k for k, _ in due])
                    for k, why in due:
                        r = res[k]
                        echo(f"[scheduler] {now:%Y-%m-%d %H:%M} {k} ({why}) -> {r.status} "
                             f"new={r.counts['new']} rev={r.counts['revised']} {r.message[:100]}")
                        runs += 1
                except WriterBusy:
                    echo("[scheduler] warehouse busy (another writer); retrying next tick")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            for _ in range(tick_seconds):
                if _stop:
                    break
                sleep(1)
    finally:
        pidf.unlink(missing_ok=True)
        echo("[scheduler] stopped. No collection happens until it is started again.")
    return runs


def stop_scheduler(paths: Paths) -> str:
    pidf = paths.scheduler_pid
    if not pidf.exists():
        return "no scheduler PID file — nothing running"
    pid = int(pidf.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidf.unlink(missing_ok=True)
        return f"stale PID file removed (pid {pid} not running)"
    for _ in range(30):
        time.sleep(1)
        if not pidf.exists():
            return f"scheduler pid {pid} stopped"
    return f"sent SIGTERM to {pid}; it will stop after the current task"
