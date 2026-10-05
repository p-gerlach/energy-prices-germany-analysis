"""Connector verification / preflight report (`oco doctor [--live]`).

Status vocabulary (kept separate; only an actual permitted data fetch upgrades a connector):
  excluded                       route deliberately not allowed (cost policy)
  unconfigured                   free credential missing (user must obtain it; never auto-registered)
  documented_free                policy documents a free route; NOT tested from this machine
  anonymous_read_tested          a bounded anonymous request returned real data
  credential_read_tested         a request with the user's free credential returned real data
  authenticated_download_tested  a raw product download with the user's free login succeeded (CDSE)
  unavailable                    tested and failed (network/egress, provider error, stopped connector)
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import sys

from .collectors import registry
from .collectors.base import Context, NotConfigured, SchemaChanged
from .http import (AuthRejected, CircuitOpen, ConnectorStopped, ContentValidationError, NetworkUnavailable, RateLimited,
                   TransientHTTPError)
from .policy import PolicyDenied
from .settings import ALLOWED_ENV, budgets, env
from .storage.state import utcnow

SOURCE_FOR_CONNECTOR = {"ecb": "ecb_fx", "eia": "eia_spot", "oil_bulletin": "oil_bulletin", "portwatch": "portwatch",
                        "gdelt": "gdelt", "rss": "rss", "jodi": "jodi", "firms": "firms", "cdse_catalogue": "cdse",
                        "sec_edgar": "sec_edgar", "comext": "comext"}


def live_probe(ctx: Context, connector: str) -> tuple[bool, str, str]:
    """Return (ok, detail, kind)."""
    src = SOURCE_FOR_CONNECTOR.get(connector)
    if connector == "cdse_download":
        if not (env("CDSE_USERNAME") and env("CDSE_PASSWORD")):
            return False, "unconfigured: no CDSE General User credential supplied", "unconfigured"
        try:
            from .satellite.download import CDSEAuth
            CDSEAuth(ctx).token()
            return True, "token obtained with your free login (download itself not tested by doctor; run `oco satellite download`)", "credential_check"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:200]}", "credential_check"
    if connector == "naturalearth":
        from .satellite.landmask import ensure_land
        try:
            p = ensure_land(ctx)
            return True, f"downloaded {p.name} ({p.stat().st_size/1e6:.1f} MB)", "anonymous_read"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:200]}", "anonymous_read"
    if not src:
        return False, "no probe defined", "none"
    col = registry.get(ctx, src)
    kind = "credential_read" if col.credential_envs else "anonymous_read"
    try:
        ok, detail = col.probe()
        if ok and not getattr(col, "probe_fetches_data", True):
            kind = "page_only"
        return ok, detail, kind
    except NotConfigured as e:
        return False, f"unconfigured: {e}", "unconfigured"
    except NetworkUnavailable as e:
        return False, f"unavailable from this machine (network/egress): {e}", kind
    except (ConnectorStopped, RateLimited, CircuitOpen, PolicyDenied, SchemaChanged, ContentValidationError,
            TransientHTTPError, AuthRejected) as e:
        return False, f"{type(e).__name__}: {str(e)[:300]}", kind
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:300]}", kind


def classify(connector: str, meta: dict, probe: dict | None, runtime: dict) -> str:
    if meta.get("verification_status") == "excluded":
        return "excluded"
    if runtime.get("status") == "stopped":
        return "unavailable"
    envs = meta.get("credential_env")
    envs = [envs] if isinstance(envs, str) else (envs or [])
    if envs and not all(env(e) for e in envs):
        return "unconfigured"
    if connector == "cdse_download" and probe and probe.get("authenticated_download"):
        return "authenticated_download_tested"
    if probe:
        if probe.get("unconfigured") and not (probe.get("anonymous_read") or probe.get("credential_read")):
            return "unconfigured"
        if probe.get("anonymous_read"):
            return "anonymous_read_tested"
        if probe.get("credential_read"):
            return "credential_read_tested"
        if probe.get("failed"):
            return "unavailable"
    return "documented_free"


def doctor(ctx: Context, live: bool = False, only: list[str] | None = None) -> dict:
    pol = ctx.policy
    if live:
        for key in pol.connectors:
            if only and key not in only:
                continue
            if key == "local_llm" and not env("OCO_LOCAL_LLM_MODEL"):
                continue
            ok, detail, kind = live_probe(ctx, key)
            if kind == "none":
                continue  # nothing to probe (e.g. release-calendar page): stays documented_free / untested
            if kind == "unconfigured" or detail.startswith("unconfigured"):
                ctx.state.record_probe(key, "unconfigured", False, detail)
            else:
                ctx.state.record_probe(key, kind if ok else f"{kind}_failed", ok, detail)
    probes: dict[str, dict] = {}
    for p in ctx.state.probes():
        d = probes.setdefault(p["connector"], {"details": []})
        d["details"].append(p)
        if p["ok"] and p["kind"] in ("anonymous_read", "credential_read", "authenticated_download"):
            d[p["kind"]] = p["checked_at"]
        if p["kind"] == "unconfigured":
            d["unconfigured"] = p["checked_at"]
        elif not p["ok"]:
            d["failed"] = p["checked_at"]
    rows = []
    for key, c in pol.connectors.items():
        runtime = ctx.state.connector_status(key)
        pr = probes.get(key)
        # a later success supersedes an earlier failure and vice versa
        if pr and pr.get("failed"):
            succ = max([v for k, v in pr.items() if k in ("anonymous_read", "credential_read", "authenticated_download")] or [""])
            if succ and succ >= pr["failed"]:
                pr = {k: v for k, v in pr.items() if k != "failed"}
            else:
                pr = {k: v for k, v in pr.items() if k not in ("anonymous_read", "credential_read")}
        status = classify(key, c.meta, pr, runtime)
        rows.append({
            "connector": key, "title": c.meta["title"], "status": status, "credential": c.credential,
            "documented_free_evidence": c.meta["zero_charge_evidence"], "policy_checked_at": c.meta["checked_at"],
            "free_setup": c.meta.get("official_free_setup"), "runtime_state": runtime["status"], "runtime_reason": runtime.get("reason"),
            "last_probe": (pr or {}).get("details", [{}])[-1] if pr else None, "optional": bool(c.meta.get("optional")),
        })
    for host, reason in pol.excluded.items():
        rows.append({"connector": f"(excluded) {host}", "title": reason, "status": "excluded", "credential": "-",
                     "documented_free_evidence": "-", "policy_checked_at": pol.raw["policy_checked_at"], "runtime_state": "-"})
    forbidden_present = sorted(k for k in os.environ if any(k.startswith(p) for p in pol.forbidden_env_prefixes))
    deps = {}
    for m in ("rasterio", "shapely", "scipy", "pyproj"):
        try:
            importlib.import_module(m)
            deps[m] = "installed"
        except ImportError:
            deps[m] = "missing (satellite extra not installed; economic/news modules unaffected)"
    b = budgets()
    disk_free = shutil.disk_usage(ctx.paths.data).free / 1e9
    report = {
        "generated_at": str(utcnow()), "live_probes_run": live, "python": sys.version.split()[0], "platform": platform.platform(),
        "data_dir": str(ctx.paths.data), "demo_mode": ctx.paths.demo, "connectors": rows,
        "credentials_present": {k: bool(env(k)) for k in ("EIA_API_KEY", "FIRMS_MAP_KEY", "CDSE_USERNAME", "CDSE_PASSWORD", "OCO_CONTACT_EMAIL")},
        "forbidden_env_present_but_ignored": forbidden_present, "allowed_env": list(ALLOWED_ENV),
        "satellite_dependencies": deps,
        "budgets": {"disk_gb": b.disk_gb, "download_gb_30d": b.download_gb, "disk_free_gb": round(disk_free, 1),
                    "cdse_downloaded_30d_gb": round(ctx.state.downloaded_bytes("cdse_download") / 1e9, 3)},
    }
    ctx.paths.reports.mkdir(parents=True, exist_ok=True)
    (ctx.paths.reports / "connector_verification.json").write_text(json.dumps(report, indent=2, default=str))
    (ctx.paths.reports / "connector_verification.md").write_text(render_md(report))
    return report


def render_md(r: dict) -> str:
    L = ["# Connector verification report", "",
         f"Generated {r['generated_at']} · live probes run: **{r['live_probes_run']}** · data dir `{r['data_dir']}`", "",
         "Only an actual permitted data fetch makes a connector *tested*. Documentation alone = `documented_free`.", "",
         "| Connector | Status | Credential | Runtime | Last probe |", "|---|---|---|---|---|"]
    for c in r["connectors"]:
        lp = c.get("last_probe") or {}
        L.append(f"| {c['connector']} | **{c['status']}** | {c['credential']} | {c.get('runtime_state')} "
                 f"| {(lp.get('checked_at') or '')[:19]} {(lp.get('detail') or '')[:110].replace('|', '/')} |")
    L += ["", "## Free credentials still needed", ""]
    for c in r["connectors"]:
        if c["status"] == "unconfigured":
            L.append(f"- **{c['connector']}** — obtain yourself at {c.get('free_setup') or c['documented_free_evidence']} and add to `.env`.")
    if r["forbidden_env_present_but_ignored"]:
        L += ["", "## Cloud/billing variables present in the environment (IGNORED by the app)", "",
              ", ".join(r["forbidden_env_present_but_ignored"])]
    L += ["", "## Satellite dependencies", ""] + [f"- {k}: {v}" for k, v in r["satellite_dependencies"].items()]
    L += ["", f"Budgets: {r['budgets']}"]
    return "\n".join(L) + "\n"
