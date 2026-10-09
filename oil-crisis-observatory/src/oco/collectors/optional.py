"""Optional phase-5 adapters (disabled by default in config/sources.yaml).

SEC EDGAR: public JSON (no key; descriptive User-Agent with contact required). Company facts are stored as
observations only for explicitly configured CIKs and XBRL concepts.
Comext: public SDMX 2.1 CSV; query keys must be verified by the user on the Eurostat data browser first.
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd

from ..settings import contact
from ..storage.warehouse import Warehouse
from .base import Collector, NotConfigured, RunResult, SchemaChanged


class SECCollector(Collector):
    key = "sec_edgar"
    connector = "sec_edgar"
    CONCEPTS = {"Revenues": "USD", "NetIncomeLoss": "USD"}

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        if contact() == "unset-contact":
            raise NotConfigured("SEC fair-access rules require a contact in the User-Agent: set OCO_CONTACT_EMAIL in .env")
        companies = self.cfg.get("companies") or []
        if not companies:
            raise NotConfigured("no companies configured under sources.sec_edgar.companies")
        with self.ctx.client(self.connector) as client:
            for c in companies:
                cik = str(c["cik"]).zfill(10)
                r = client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
                result.n_requests += 1
                sha = wh.store_raw("sec_edgar", r.content, url=r.url, content_type=r.content_type, ext="json")
                result.raw.append(sha)
                facts = r.json().get("facts", {}).get("us-gaap", {})
                for concept, unit in self.CONCEPTS.items():
                    entries = (facts.get(concept) or {}).get("units", {}).get(unit, [])
                    entries = [e for e in entries if e.get("form") in ("10-K", "10-Q") and e.get("fp") and e.get("start")]
                    if not entries:
                        continue
                    sid = f"sec.{cik}.{concept}".lower()
                    wh.upsert_series(sid, source="SEC EDGAR", source_key=f"{cik}:{concept}", name=f"{c.get('name', cik)} {concept}",
                                     geography="US", product="company", unit=unit, frequency="quarterly",
                                     description="XBRL company facts as filed (duplicates across filings resolved to latest filed)")
                    best = {}
                    for e in sorted(entries, key=lambda e: e.get("filed", "")):
                        best[(e["start"], e["end"])] = e
                    rows = []
                    seen = set()
                    for (s, en), e in sorted(best.items()):
                        if s in seen:
                            continue
                        seen.add(s)
                        rows.append({"obs_start": s, "obs_end": en, "value": float(e["val"]),
                                     "source_published_at": pd.Timestamp(e["filed"]).tz_localize("UTC").to_pydatetime(),
                                     "attrs": {"accn": e.get("accn"), "form": e.get("form")}})
                        result.saw(date.fromisoformat(en))
                    result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=sha))

    def probe(self):
        with self.ctx.client(self.connector) as client:
            r = client.get("https://data.sec.gov/submissions/CIK0000034088.json")
        return True, f"submissions JSON for {r.json().get('name')}"


class ComextCollector(Collector):
    key = "comext"
    connector = "comext"
    BASE = "https://ec.europa.eu/eurostat/api/comext/dissemination/sdmx/2.1/data/"

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        queries = self.cfg.get("queries") or []
        if not queries:
            raise NotConfigured("no Comext queries configured (sources.comext.queries) — verify keys on the Eurostat data browser first")
        with self.ctx.client(self.connector) as client:
            for q in queries:
                r = client.get(f"{self.BASE}{self.cfg['dataset']}/{q}", params={"format": "SDMX-CSV"})
                result.n_requests += 1
                sha = wh.store_raw("comext", r.content, url=r.url, content_type=r.content_type, ext="csv")
                result.raw.append(sha)
                df = pd.read_csv(io.StringIO(r.text()), dtype=str)
                if not {"TIME_PERIOD", "OBS_VALUE"} <= set(df.columns):
                    raise SchemaChanged(f"Comext CSV lacks TIME_PERIOD/OBS_VALUE: {list(df.columns)[:10]}")
                sid = f"comext.{self.cfg['dataset']}.{q}".lower()
                wh.upsert_series(sid, source="Eurostat Comext", source_key=q, name=f"Comext {q}", geography="EU",
                                 product="trade", unit="as stated in query", frequency="monthly")
                rows = []
                for _, rr in df.iterrows():
                    y, m = map(int, rr["TIME_PERIOD"][:7].split("-"))
                    v = pd.to_numeric(rr["OBS_VALUE"], errors="coerce")
                    end = (pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)).date()
                    rows.append({"obs_start": date(y, m, 1), "obs_end": end, "value": None if pd.isna(v) else float(v)})
                    result.saw(end)
                result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=sha))

    def probe(self):
        raise NotConfigured("Comext probe needs a verified query key in sources.comext.queries")
