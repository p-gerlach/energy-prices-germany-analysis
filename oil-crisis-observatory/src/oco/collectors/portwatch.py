"""IMF PortWatch daily chokepoint transit calls — anonymous read-only queries of ONE approved layer.

No ArcGIS account, no token, no SDK. Schema (field names, date type, maxRecordCount) is resolved from the
layer's own metadata. Chokepoint ids are resolved from the data, not assumed.

Interpretation limits carried into series metadata:
* tanker transits are vessel counts, not barrels; deadweight capacity is not measured cargo
* aggregate AIS-based counts; no vessel identification; AIS coverage gaps exist
* the latest observed date is published as-is (provider lag), never extrapolated to today
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

REQUIRED_FIELDS = {"date", "portid", "portname"}


class PortWatchCollector(Collector):
    key = "portwatch"
    connector = "portwatch"

    @property
    def layer(self) -> str:
        return self.cfg["layer_url"]

    def layer_metadata(self, client, result: RunResult | None = None) -> dict:
        r = client.get(self.layer, params={"f": "json"})
        if result:
            result.n_requests += 1
        meta = r.json()
        if "error" in meta:
            raise SchemaChanged(f"PortWatch layer metadata error: {str(meta['error'])[:200]}")
        fields = {f["name"]: f.get("type") for f in meta.get("fields", [])}
        missing = REQUIRED_FIELDS - set(fields)
        if missing:
            raise SchemaChanged(f"PortWatch layer lacks fields {missing}")
        want = set(self.cfg["fields"])
        if not want <= set(fields):
            raise SchemaChanged(f"PortWatch layer lacks configured fields {want - set(fields)}")
        return {"fields": fields, "max_records": int(meta.get("maxRecordCount") or 1000), "raw": r}

    def resolve_chokepoints(self, client, result: RunResult | None = None) -> dict[str, tuple[str, str]]:
        r = client.get(self.layer + "/query", params={
            "where": "1=1", "outFields": "portid,portname", "returnDistinctValues": "true",
            "returnGeometry": "false", "f": "json"})
        if result:
            result.n_requests += 1
        payload = r.json()
        if "error" in payload:
            raise SchemaChanged(f"PortWatch distinct query error: {str(payload['error'])[:200]}")
        found = {}
        names = {f["attributes"]["portname"]: f["attributes"]["portid"] for f in payload.get("features", [])}
        lower = {k.lower(): (k, v) for k, v in names.items() if k}
        for cp in self.cfg["chokepoints"]:
            for n in cp["names"]:
                if n.lower() in lower:
                    found[cp["slug"]] = (lower[n.lower()][1], lower[n.lower()][0])
                    break
        missing = [cp["slug"] for cp in self.cfg["chokepoints"] if cp["slug"] not in found]
        if missing:
            raise SchemaChanged(f"PortWatch chokepoints not found by name: {missing}; available: {sorted(names)[:30]}")
        return found

    def query_range(self, client, portid: str, start: date, date_type: str, page_size: int, result: RunResult) -> tuple[list[dict], list[bytes]]:
        if date_type == "esriFieldTypeDate":
            where = f"portid='{portid}' AND date >= TIMESTAMP '{start:%Y-%m-%d} 00:00:00'"
        else:
            where = f"portid='{portid}' AND date >= '{start:%Y-%m-%d}'"
        feats, bodies = [], []
        offset = 0
        while True:
            r = client.get(self.layer + "/query", params={
                "where": where, "outFields": "*", "orderByFields": "date ASC", "returnGeometry": "false",
                "resultOffset": offset, "resultRecordCount": page_size, "f": "json"})
            result.n_requests += 1
            payload = r.json()
            if "error" in payload:
                raise SchemaChanged(f"PortWatch query error: {str(payload['error'])[:200]}")
            bodies.append(r.content)
            batch = [f["attributes"] for f in payload.get("features", [])]
            feats.extend(batch)
            offset += len(batch)
            if not batch or not payload.get("exceededTransferLimit"):
                break
            if offset > 50_000:
                raise SchemaChanged("PortWatch pagination safety stop")
        return feats, bodies

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        attribution = self.ctx.policy.connector("portwatch").meta["attribution"]
        with self.ctx.client(self.connector) as client:
            meta = self.layer_metadata(client, result)
            wh.store_raw("portwatch", meta["raw"].content, url=meta["raw"].url, content_type="application/json",
                         ext="json", note="layer metadata")
            date_type = meta["fields"]["date"]
            page = min(int(self.cfg.get("page_size", 1000)), meta["max_records"])
            cps = self.resolve_chokepoints(client, result)
            for slug, (portid, portname) in cps.items():
                if mode == "backfill":
                    start = date.fromisoformat(self.cfg.get("backfill_start", "2019-01-01"))
                else:
                    start = date.today() - timedelta(days=int(self.cfg.get("revision_overlap_days", 30)))
                feats, bodies = self.query_range(client, portid, start, date_type, page, result)
                shas = [wh.store_raw("portwatch", b, url=f"{self.layer}/query?portid={portid}&page={i}",
                                     content_type="application/json", ext="json") for i, b in enumerate(bodies)]
                result.raw.extend(shas)
                by_day: dict[date, dict] = {}
                for a in feats:
                    d = _parse_date(a["date"], date_type, a)
                    by_day[d] = a  # last one wins if duplicated
                for field, fmeta in self.cfg["fields"].items():
                    sid = f"portwatch.{slug}.{field}"
                    wh.upsert_series(
                        sid, source="IMF PortWatch", source_key=f"{portid}:{field}", name=f"{portname}: {fmeta['name']}",
                        geography=portname, product="shipping", unit=fmeta["unit"], frequency="daily",
                        description="Daily AIS-based transit calls (IMF PortWatch). Aggregate counts; not vessel-level; provider lag of several days.",
                        metadata={"attribution": attribution, "portid": portid,
                                  "limitations": ["tanker transits are not barrels", "capacity is deadweight, not cargo",
                                                  "AIS coverage gaps", "no vessel identification from aggregates"]},
                    )
                    rows = []
                    for d, a in sorted(by_day.items()):
                        v = a.get(field)
                        rows.append({"obs_start": d, "obs_end": d, "value": None if v is None else float(v),
                                     "attrs": {"ObjectId": a.get("ObjectId")}})
                        result.saw(d)
                    result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=shas[0] if shas else None))

    def probe(self):
        with self.ctx.client(self.connector) as client:
            meta = self.layer_metadata(client)
            cps = self.resolve_chokepoints(client)
            portid, name = cps.get("hormuz") or next(iter(cps.values()))
            res = RunResult(self.key)
            feats, _ = self.query_range(client, portid, date.today() - timedelta(days=21), meta["fields"]["date"], 100, res)
        if not feats:
            return False, f"{name}: no rows in last 21 days"
        last = feats[-1]
        d = _parse_date(last["date"], meta["fields"]["date"], last)
        return True, f"{name} ({portid}) latest {d}: n_tanker={last.get('n_tanker')} n_total={last.get('n_total')}"


def _parse_date(v, date_type: str, attrs: dict) -> date:
    # Prefer explicit year/month/day fields: the epoch timestamps are local-midnight shifted
    # (e.g. 21:00 UTC of the previous day), so converting them naively would shift every day by one.
    if all(k in attrs and attrs[k] is not None for k in ("year", "month", "day")):
        return date(int(attrs["year"]), int(attrs["month"]), int(attrs["day"]))
    if date_type == "esriFieldTypeDate" or isinstance(v, (int, float)):
        dt = datetime.fromtimestamp(float(v) / 1000, tz=timezone.utc)
        # round to nearest day boundary to absorb timezone offsets
        return (dt + timedelta(hours=12)).date()
    return date.fromisoformat(str(v)[:10])


def dumps(x) -> str:
    return json.dumps(x, default=str)
