"""Collector framework: configuration checks, guarded clients, run bookkeeping, health updates."""
from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

import httpx

from ..http import (AuthRejected, CircuitOpen, ConnectorStopped, ContentValidationError, GuardedClient,
                    NetworkUnavailable, RateLimited, TransientHTTPError)
from ..policy import Policy, PolicyDenied, load_policy
from ..settings import Paths, env, sources_config
from ..storage.state import StateStore, utcnow
from ..storage.warehouse import Warehouse


class NotConfigured(Exception):
    """A free credential the user must supply is missing. The connector stays visibly unconfigured."""


class SchemaChanged(Exception):
    """Provider format changed (headers, units, fields). Fail visibly; store nothing derived."""


@dataclass
class Context:
    paths: Paths
    state: StateStore
    policy: Policy = field(default_factory=load_policy)
    transport: httpx.BaseTransport | None = None     # tests inject httpx.MockTransport
    sleep: Callable[[float], None] = time.sleep

    def client(self, connector: str, secrets=()) -> GuardedClient:
        return GuardedClient(connector, self.state, self.policy, secrets=secrets, transport=self.transport, sleep=self.sleep)


@dataclass
class RunResult:
    source: str
    status: str = "ok"            # ok | not_modified | unconfigured | stopped | paused | unavailable | failed | denied | schema_changed
    counts: dict = field(default_factory=lambda: {"new": 0, "revised": 0, "unchanged": 0})
    latest_observation: date | None = None
    raw: list[str] = field(default_factory=list)
    n_requests: int = 0
    message: str = ""
    revised_series: dict = field(default_factory=dict)
    source_published_at: datetime | None = None

    def add(self, series_id: str, res: dict):
        for k in ("new", "revised", "unchanged"):
            self.counts[k] += res[k]
        if res["revised_keys"]:
            self.revised_series.setdefault(series_id, []).extend(str(d) for d in res["revised_keys"])

    def saw(self, d: date | None):
        if d and (self.latest_observation is None or d > self.latest_observation):
            self.latest_observation = d


class Collector:
    key: str = ""
    connector: str = ""
    credential_envs: tuple[str, ...] = ()

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = sources_config()["sources"].get(self.key, {})

    # --- credentials -------------------------------------------------------------------
    def missing_credentials(self) -> list[str]:
        return [e for e in self.credential_envs if not env(e)]

    def require_credentials(self):
        missing = self.missing_credentials()
        if missing:
            meta = self.ctx.policy.connector(self.connector).meta
            raise NotConfigured(
                f"{self.key}: missing free credential(s) {missing}. Obtain them yourself at "
                f"{meta.get('official_free_setup', meta['zero_charge_evidence'])} and put them in .env. "
                "The application never registers accounts on your behalf."
            )

    def secrets(self) -> list[str]:
        return [env(e) or "" for e in self.credential_envs]

    # --- interface ---------------------------------------------------------------------
    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw) -> None:
        raise NotImplementedError

    def probe(self) -> tuple[bool, str]:
        """One bounded, permitted request proving the route returns real data."""
        raise NotImplementedError


def run_collector(ctx: Context, collector: Collector, mode: str = "refresh", wh: Warehouse | None = None, **kw) -> RunResult:
    """Run one collector with full bookkeeping. Opens the single-writer warehouse unless one is given."""
    res = RunResult(source=collector.key)
    started = utcnow()
    ctx.state.update_health(collector.key, last_attempt_at=started)
    if not collector.cfg.get("enabled", True) and mode != "force":
        res.status, res.message = "disabled", "disabled in config/sources.yaml"
        ctx.state.update_health(collector.key, last_error=res.message)
        return res

    def _do(w: Warehouse):
        try:
            collector.require_credentials()
            collector.collect(w, res, mode=mode, **kw)
        except NotConfigured as e:
            res.status, res.message = "unconfigured", str(e)
        except ConnectorStopped as e:
            res.status, res.message = "stopped", str(e)
        except RateLimited as e:
            res.status, res.message = "paused", str(e)
        except CircuitOpen as e:
            res.status, res.message = "paused", str(e)
        except NetworkUnavailable as e:
            res.status, res.message = "unavailable", str(e)
        except PolicyDenied as e:
            res.status, res.message = "denied", f"blocked by access policy: {e}"
        except SchemaChanged as e:
            res.status, res.message = "schema_changed", str(e)
            ctx.state.log(collector.key, "ERROR", f"SCHEMA CHANGE: {e}")
        except (ContentValidationError, TransientHTTPError, AuthRejected) as e:
            res.status, res.message = "failed", f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001 — never crash the scheduler; record and continue
            res.status, res.message = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        run_id = f"{collector.key}-{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        w.record_run(run_id, collector.key, started, res.status, res.n_requests, res.raw, res.counts,
                     res.latest_observation, res.message[:2000] if res.status not in ("ok", "not_modified") else None)

    if wh is not None:
        _do(wh)
    else:
        with Warehouse.writer(ctx.paths) as w:
            _do(w)

    fields = {"last_result": {"status": res.status, "counts": res.counts, "message": res.message[:500]}}
    if res.status in ("ok", "not_modified"):
        fields["last_success_at"] = utcnow()
        fields["last_error"] = None
        if res.latest_observation:
            prev = ctx.state.health_for(collector.key).get("latest_observation_at")
            if not prev or str(res.latest_observation) >= prev[:10]:
                fields["latest_observation_at"] = str(res.latest_observation)
        if res.source_published_at:
            fields["source_published_at"] = res.source_published_at
    else:
        fields["last_error"] = f"{res.status}: {res.message[:400]}"
    ctx.state.update_health(collector.key, **fields)
    ctx.state.log(collector.key, "INFO" if res.status in ("ok", "not_modified") else "WARN",
                  f"{mode} -> {res.status} new={res.counts['new']} revised={res.counts['revised']} "
                  f"unchanged={res.counts['unchanged']} {res.message[:200]}")
    return res
