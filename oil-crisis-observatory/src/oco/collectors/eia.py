"""EIA API v2 (free user-supplied key). Spot prices and Weekly Petroleum Status Report series.

* routes/series are validated against the API's own facet metadata and saved as a manifest
* pagination via offset/length until `total` rows are read
* recent periods are re-requested on every refresh to capture revisions
* US data are US evidence; "product supplied" is a demand proxy, not observed consumption
"""
from __future__ import annotations

from datetime import date, timedelta

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

API = "https://api.eia.gov/v2/"
PAGE = 5000


class EIACollector(Collector):
    connector = "eia"
    credential_envs = ("EIA_API_KEY",)

    def _key(self) -> str:
        return self.secrets()[0]

    def validate_manifest(self, client, result: RunResult) -> dict:
        """Check every configured series id exists in the route's facet list; return {facet: meta}."""
        route = self.cfg["route"]
        r = client.get(f"{API}{route}/facet/series", params={"api_key": self._key()})
        result.n_requests += 1
        payload = r.json()
        facets = (payload.get("response") or {}).get("facets")
        if facets is None:
            raise SchemaChanged(f"EIA facet metadata for {route} missing 'response.facets': {str(payload)[:200]}")
        known = {f.get("id"): f for f in facets}
        missing = [s["facet"] for s in self.cfg["series"] if s["facet"] not in known]
        if missing:
            raise SchemaChanged(f"EIA route {route} no longer lists series {missing}; update config/sources.yaml after checking the API browser")
        return {s["facet"]: known[s["facet"]] for s in self.cfg["series"]}

    def fetch_series(self, client, facet: str, start: str, result: RunResult) -> tuple[list[dict], list[bytes]]:
        route = self.cfg["route"]
        rows: list[dict] = []
        bodies: list[bytes] = []
        offset = 0
        total = None
        while True:
            params = {
                "api_key": self._key(),
                "frequency": self.cfg["eia_frequency"],
                "data[0]": "value",
                "facets[series][]": facet,
                "start": start,
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": PAGE,
            }
            r = client.get(f"{API}{route}/data", params=params)
            result.n_requests += 1
            bodies.append(r.content)
            payload = r.json()
            if "error" in payload:
                raise SchemaChanged(f"EIA error: {str(payload['error'])[:200]}")
            resp = payload.get("response") or {}
            data = resp.get("data")
            if data is None:
                raise SchemaChanged("EIA response lacks response.data")
            total = int(resp.get("total", len(data)))
            rows.extend(data)
            offset += len(data)
            if not data or offset >= total:
                break
            if offset > 200_000:
                raise SchemaChanged("EIA pagination did not converge (safety stop)")
        if total is not None and len(rows) != total:
            raise SchemaChanged(f"EIA pagination incomplete: {len(rows)} of {total}")
        return rows, bodies

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        attribution = self.ctx.policy.connector("eia").meta["attribution"]
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            manifest = self.validate_manifest(client, result)
            for s in self.cfg["series"]:
                if mode == "backfill":
                    start = self.cfg.get("backfill_start", "2015-01-01")
                else:
                    start = (date.today() - timedelta(days=int(self.cfg.get("revision_overlap_days", 30)))).isoformat()
                if self.cfg["eia_frequency"] == "weekly":
                    start = start[:10]
                rows, bodies = self.fetch_series(client, s["facet"], start, result)
                units = {str(r.get("units")) for r in rows if r.get("units")}
                wh.upsert_series(
                    s["id"], source="EIA", source_key=f"{self.cfg['route']}:{s['facet']}", name=s["name"],
                    geography=s.get("geography", "US"), product=s.get("product", ""), unit=s["unit"],
                    frequency=self.cfg["frequency"],
                    description=(manifest.get(s["facet"]) or {}).get("name", ""),
                    metadata={"eia_units": sorted(units), "measure": s.get("measure"), "attribution": attribution,
                              "note": "US data — US evidence only" + ("; demand proxy, not consumption" if "supplied" in s["id"] else "")},
                )
                _check_units(s, units)
                # EIA echoes request parameters (including api_key) in its JSON: scrub before storing evidence.
                bodies = [b.replace(self._key().encode(), b"<redacted>") for b in bodies]
                shas = [wh.store_raw("eia", b, url=f"{API}{self.cfg['route']}/data?facet={s['facet']}&page={i}",
                                     content_type="application/json", ext="json") for i, b in enumerate(bodies)]
                result.raw.extend(shas)
                obs = []
                seen = set()
                for r in rows:
                    d = date.fromisoformat(str(r["period"])[:10])
                    if d in seen:
                        continue
                    seen.add(d)
                    v = r.get("value")
                    try:
                        val = float(v) if v not in (None, "", "NA", "--") else None
                    except (TypeError, ValueError):
                        val = None
                    if s.get("measure") in ("flow", "rate") and self.cfg["eia_frequency"] == "weekly":
                        start_d, end_d = d - timedelta(days=6), d
                    else:
                        start_d, end_d = d, d
                    obs.append({"obs_start": start_d, "obs_end": end_d, "value": val,
                                "status": "ok" if val is not None else "missing", "attrs": {"period": str(r["period"])}})
                    result.saw(end_d)
                result.add(s["id"], wh.ingest_observations(s["id"], obs, raw_sha256=shas[0] if shas else None))

    def probe(self):
        self.require_credentials()
        res = RunResult(self.key)
        with self.ctx.client(self.connector, secrets=self.secrets()) as client:
            self.validate_manifest(client, res)
            s = self.cfg["series"][0]
            start = (date.today() - timedelta(days=21)).isoformat()
            rows, _ = self.fetch_series(client, s["facet"], start, res)
        if not rows:
            return False, "EIA returned zero rows for the last 21 days"
        return True, f"{s['facet']} latest period {rows[-1]['period']} value {rows[-1].get('value')} {rows[-1].get('units')}"


def _check_units(s: dict, units: set[str]):
    expected = {"USD per barrel": {"$/BBL"}, "USD per gallon": {"$/GAL"}, "thousand barrels": {"MBBL"}, "thousand barrels per day": {"MBBL/D"},
                "percent": {"%", "PERCENT"}}
    exp = expected.get(s["unit"])
    if exp and units and not units <= exp:
        raise SchemaChanged(f"EIA units for {s['facet']} changed: got {units}, expected {exp}")


class EIASpotCollector(EIACollector):
    key = "eia_spot"


class EIAWeeklyCollector(EIACollector):
    key = "eia_weekly"


class EIAProductsCollector(EIACollector):
    key = "eia_products"
