"""JODI Oil World Database — official public CSV ZIP downloads (no account).

* ZIP links discovered on the official download page; file hash change => re-ingest (supplementary revisions)
* streamed to disk with a size cap, filtered to configured countries/products/flows/units
* non-numeric OBS_VALUE (e.g. '-', 'x', blanks) is MISSING, never zero; ASSESSMENT_CODE preserved
* aggregation across countries must use a balanced sample (see analysis.indicators.jodi_balanced_sample)
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
from bs4 import BeautifulSoup

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

COLUMNS = {"REF_AREA", "TIME_PERIOD", "ENERGY_PRODUCT", "FLOW_BREAKDOWN", "UNIT_MEASURE", "OBS_VALUE"}
ASSESSMENT = {"1": "comparable", "2": "consult metadata", "3": "not assessed"}


def discover_annual_csvs(html: str, base: str, kind: str = "primary") -> dict[int, str]:
    """Current layout (checked 2026-10-05): one CSV per year, e.g. annual-csv/primary/2025.csv and
    annual-csv/primary/primaryyear2026.csv for the running year. Returns {year: url}."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        m = re.search(rf"/annual-csv/{kind}/[a-z]*?(\d{{4}})\.csv$", urlsplit(href).path)
        if m:
            out[int(m.group(1))] = href
    return out


def discover_zip_links(html: str, base: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        h = href.lower()
        if not h.endswith(".zip") or "csv" not in h:
            continue
        if "primary" in h and "world" in h:
            out.setdefault("primary", href)
        elif "secondary" in h and "world" in h:
            out.setdefault("secondary", href)
    if not out:
        raise SchemaChanged("no world primary/secondary CSV ZIP links found on the JODI download page")
    return out


def _csv_openers(path_or_bytes):
    """Yield (name, opener) for each CSV inside a ZIP, or for a single plain CSV file/bytes."""
    if isinstance(path_or_bytes, (str, Path)) and str(path_or_bytes).lower().endswith(".csv"):
        yield str(path_or_bytes), lambda: open(path_or_bytes, "rb")
        return
    if isinstance(path_or_bytes, bytes) and not path_or_bytes.startswith(b"PK"):
        yield "csv", lambda: io.BytesIO(path_or_bytes)
        return
    zf = zipfile.ZipFile(path_or_bytes if isinstance(path_or_bytes, (str, Path)) else io.BytesIO(path_or_bytes))
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise SchemaChanged("JODI ZIP contains no CSV")
    for n in names:
        yield n, (lambda n=n: zf.open(n))


def parse_jodi_zip(path_or_bytes, countries, products, flows, units, since: str) -> pd.DataFrame:
    """Parse a JODI ZIP or a single yearly CSV (same column layout)."""
    frames = []
    for n, opener in _csv_openers(path_or_bytes):
        with opener() as fh:
            head = pd.read_csv(fh, nrows=5, dtype=str)
        missing = COLUMNS - set(head.columns)
        if missing:
            raise SchemaChanged(f"JODI CSV {n} lacks columns {missing}; has {list(head.columns)}")
        with opener() as fh:
            for chunk in pd.read_csv(fh, dtype=str, chunksize=200_000):
                m = (chunk["REF_AREA"].isin(countries) & chunk["ENERGY_PRODUCT"].isin(products)
                     & chunk["FLOW_BREAKDOWN"].isin(flows) & chunk["UNIT_MEASURE"].isin(units)
                     & (chunk["TIME_PERIOD"] >= since))
                if m.any():
                    frames.append(chunk[m])
    if not frames:
        return pd.DataFrame(columns=list(COLUMNS) + ["ASSESSMENT_CODE"])
    df = pd.concat(frames, ignore_index=True)
    df["value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df["status"] = df["value"].isna().map({True: "missing", False: "ok"})
    if "ASSESSMENT_CODE" not in df.columns:
        df["ASSESSMENT_CODE"] = None
    return df


def series_id(row) -> str:
    return f"jodi.{row.REF_AREA}.{row.ENERGY_PRODUCT}.{row.FLOW_BREAKDOWN}.{row.UNIT_MEASURE}".lower()


class JODICollector(Collector):
    key = "jodi"
    connector = "jodi"
    probe_fetches_data = False   # probe reads the download page only; a full ZIP fetch is a real refresh

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        attribution = self.ctx.policy.connector("jodi").meta["attribution"]
        with self.ctx.client(self.connector) as client:
            page = client.get(self.cfg["page"])
            result.n_requests += 1
            since_year = int(str(self.cfg.get("since", "2015-01"))[:4])
            targets = []
            for kind in self.cfg.get("files", ["primary"]):
                yearly = discover_annual_csvs(page.text(), self.cfg["page"], kind)
                if not yearly:
                    raise SchemaChanged(f"no annual-csv/{kind}/<year>.csv links found on the JODI download page")
                years = sorted(y for y in yearly if y >= since_year)
                if mode != "backfill":
                    years = years[-2:]  # refresh: current + previous year (revisions); backfill: all since `since`
                targets += [(f"{kind}-{y}", yearly[y]) for y in years]
            for label, url in targets:
                tmp = Path(tempfile.mkdtemp(dir=self.ctx.paths.state)) / f"jodi_{label}.csv"
                r = client.get(url, conditional=True, stream_to=tmp,
                               max_bytes=int(self.cfg.get("max_zip_mb", 300)) * 1024 * 1024)
                result.n_requests += 1
                if r.not_modified:
                    continue
                known = wh.con.execute("SELECT 1 FROM raw_files WHERE sha256=?", [r.sha256]).fetchone()
                sha = wh.store_raw("jodi", None, url=r.url, final_url=r.final_url, retrieved_at=r.retrieved_at,
                                   content_type=r.content_type, ext="csv", existing_path=tmp, sha256=r.sha256,
                                   etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"), note=label)
                result.raw.append(sha)
                shutil.rmtree(tmp.parent, ignore_errors=True)  # temp copy (moved into raw/ unless already stored)
                if known and mode != "backfill":
                    continue  # identical file already ingested
                stored = self.ctx.paths.raw / wh.con.execute("SELECT rel_path FROM raw_files WHERE sha256=?", [sha]).fetchone()[0]
                df = parse_jodi_zip(stored, self.cfg["countries"], self.cfg["products"], self.cfg["flows"],
                                    self.cfg["units"], self.cfg.get("since", "2015-01"))
                for sid_row, g in df.groupby(["REF_AREA", "ENERGY_PRODUCT", "FLOW_BREAKDOWN", "UNIT_MEASURE"]):
                    ra, prod, flow, unit = sid_row
                    sid = f"jodi.{ra}.{prod}.{flow}.{unit}".lower()
                    wh.upsert_series(sid, source="JODI", source_key=f"{ra}/{prod}/{flow}/{unit}",
                                     name=f"{ra} {prod} {flow} ({unit})", geography=ra, product=prod.lower(),
                                     unit={"KBD": "thousand barrels per day", "KBBL": "thousand barrels"}.get(unit, unit),
                                     frequency="monthly", description="JODI Oil World Database (national submissions)",
                                     metadata={"attribution": attribution, "assessment_codes": ASSESSMENT})
                    rows = []
                    for _, rr in g.sort_values("TIME_PERIOD").iterrows():
                        y, m = map(int, str(rr["TIME_PERIOD"])[:7].split("-"))
                        start = date(y, m, 1)
                        end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
                        rows.append({"obs_start": start, "obs_end": end,
                                     "value": None if pd.isna(rr["value"]) else float(rr["value"]),
                                     "status": rr["status"],
                                     "attrs": {"assessment_code": rr.get("ASSESSMENT_CODE"), "raw_value": rr["OBS_VALUE"]}})
                        result.saw(end)
                    result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=sha))

    def probe(self):
        with self.ctx.client(self.connector) as client:
            page = client.get(self.cfg["page"])
        yearly = discover_annual_csvs(page.text(), self.cfg["page"], "primary")
        if not yearly:
            return False, "no annual-csv/primary/<year>.csv links on the download page"
        return True, f"download page lists primary yearly CSVs {min(yearly)}-{max(yearly)} (download itself not performed in probe)"


def month_label(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}", s))
