"""European Commission Weekly Oil Bulletin.

* Discovers the CURRENT dated XLSX links on the official page (no hard-coded attachment URL).
* Parses the price-history workbook (prices with and without taxes).
* Units are confirmed from the sheet text; "euros per 1,000 litres" values are divided by 1,000 to
  give EUR per litre. If no unit text is present, the unit is accepted ONLY when every product's
  magnitude is consistent with EUR/1000 L, and that inference is recorded in series metadata.
* Header/unit/layout changes raise SchemaChanged — never silently shifted columns.
* Observation date = the Monday "prices in force on" date; the bulletin is published later (normally
  Thursday). Publication time is not invented: it is left empty unless the provider states it.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import pandas as pd
from bs4 import BeautifulSoup

from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

PRODUCT_PATTERNS = {
    "euro95": [r"euro[\s-]*super\s*95", r"euro95"],
    "diesel": [r"automotive\s+gas\s*oil", r"gas\s*oil\s+automobile", r"dieselkraftstoff", r"\bdiesel\b"],
    "heating_oil": [r"heating\s+gas\s*oil", r"gas\s*oil\s+de\s+chauffage", r"heiz[öo]l", r"heating_oil"],
}
WIDE_HEADER = re.compile(r"^(?P<cc>[A-Z]{2}|EU|EUR|EU27|EU_27|EA)_price_(?P<tax>with|wo|without)_tax(?:es)?_(?P<prod>[a-z0-9_]+)$", re.I)
UNIT_PER_1000L = re.compile(r"(1\s*000|1\.000|1,000|1000)\s*(l\b|litres?|liters?|ltr)", re.I)
UNIT_PER_L = re.compile(r"(€|eur|euro)s?\s*(/|per)\s*(l\b|litre|liter)", re.I)
PLAUSIBLE_PER_1000L = (300.0, 5000.0)
EU_ALIASES = {"EU", "EUR", "EU27", "EU_27"}

COUNTRY_NAMES = {
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "croatia": "HR", "cyprus": "CY", "czechia": "CZ",
    "czech republic": "CZ", "denmark": "DK", "estonia": "EE", "finland": "FI", "france": "FR", "germany": "DE",
    "greece": "GR", "hungary": "HU", "ireland": "IE", "italy": "IT", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "malta": "MT", "netherlands": "NL", "poland": "PL", "portugal": "PT", "romania": "RO",
    "slovakia": "SK", "slovenia": "SI", "spain": "ES", "sweden": "SE",
}


@dataclass
class BulletinLink:
    url: str
    filename: str
    label: str
    kind: str  # history | latest_with_taxes | latest_without_taxes | other


def discover_links(html: str, base_url: str) -> list[BulletinLink]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if "/document/download/" not in href:
            continue
        q = parse_qs(urlsplit(href).query)
        filename = unquote(q.get("filename", [""])[0])
        label = " ".join(a.get_text(" ").split())
        blob = f"{filename} {label}".lower()
        if ".xlsx" not in blob and "xlsx" not in blob:
            continue
        if "history" in blob or "historical" in blob:
            kind = "history"
        elif "without tax" in blob or "wo tax" in blob or "without_tax" in blob:
            kind = "latest_without_taxes"
        elif "with tax" in blob or "with_tax" in blob:
            kind = "latest_with_taxes"
        else:
            kind = "other"
        links.append(BulletinLink(href, filename, label, kind))
    if not links:
        raise SchemaChanged("no XLSX download links found on the Weekly Oil Bulletin page (layout changed?)")
    return links


def _match_product(text: str) -> str | None:
    t = str(text).lower()
    for prod, pats in PRODUCT_PATTERNS.items():
        if any(re.search(p, t) for p in pats):
            return prod
    return None


def _to_date(v) -> date | None:
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _sheet_tax(name: str) -> str | None:
    n = name.lower()
    if re.search(r"(wo|without|w/o|excl|net)[\s_.]*tax", n) or "sans tax" in n or "ohne" in n:
        return "without_tax"
    if re.search(r"with[\s_]*tax", n) or "incl" in n:
        return "with_tax"
    return None


def _find_unit(df: pd.DataFrame) -> str | None:
    top = " ".join(str(x) for x in df.head(15).fillna("").values.ravel())
    if UNIT_PER_1000L.search(top):
        return "EUR/1000L"
    if UNIT_PER_L.search(top):
        return "EUR/L"
    return None


def parse_history_workbook(content: bytes, countries: list[str], products: list[str], since: date | None = None) -> pd.DataFrame:
    """Return long frame: country, product, tax, obs_date, value_eur_per_litre, unit_source."""
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl")
    except Exception as e:  # noqa: BLE001
        raise SchemaChanged(f"Oil Bulletin workbook unreadable: {e}") from e
    frames = []
    diagnostics = []
    for name, df in sheets.items():
        tax = _sheet_tax(name)
        if tax is None or df.empty:
            diagnostics.append(f"sheet {name!r}: skipped (no tax marker in name)")
            continue
        out = _parse_wide(df, tax) if _has_wide_header(df) else _parse_blocks(df, tax)
        if out is None:
            diagnostics.append(f"sheet {name!r}: no recognised layout; first row {list(df.iloc[0].astype(str))[:6]}")
            continue
        frames.append(out)
    if not frames:
        raise SchemaChanged("Oil Bulletin history workbook layout not recognised: " + "; ".join(diagnostics)[:800])
    long = pd.concat(frames, ignore_index=True)
    long["country"] = long["country"].replace({a: "EU" for a in EU_ALIASES})
    long = long[long["country"].isin(countries) & long["product"].isin(products)]
    if since:
        long = long[long["obs_date"] >= since]
    if long.empty:
        raise SchemaChanged("Oil Bulletin workbook parsed but no configured country/product rows found")
    _validate_magnitudes(long)
    return long.drop_duplicates(["country", "product", "tax", "obs_date"], keep="last")


def _has_wide_header(df: pd.DataFrame) -> bool:
    for i in range(min(10, len(df))):
        if sum(bool(WIDE_HEADER.match(str(c).strip())) for c in df.iloc[i].values) >= 3:
            return True
    return False


def _parse_wide(df: pd.DataFrame, tax_hint: str) -> pd.DataFrame | None:
    hdr_row = next(i for i in range(min(10, len(df))) if sum(bool(WIDE_HEADER.match(str(c).strip())) for c in df.iloc[i].values) >= 3)
    header = [str(c).strip() for c in df.iloc[hdr_row].values]
    unit = _find_unit(df)
    date_col = None
    for j, h in enumerate(header):
        if re.search(r"date|in force|prices in force", h, re.I):
            date_col = j
            break
    if date_col is None:
        date_col = 0
    recs = []
    prod_map = {"euro95": "euro95", "diesel": "diesel", "heating_oil": "heating_oil", "heatingoil": "heating_oil"}
    cols = []
    for j, h in enumerate(header):
        m = WIDE_HEADER.match(h)
        if not m:
            continue
        tax = "with_tax" if m.group("tax").lower() == "with" else "without_tax"
        prod = prod_map.get(m.group("prod").lower())
        if prod:
            cols.append((j, m.group("cc").upper(), prod, tax))
    if not cols:
        return None
    for i in range(hdr_row + 1, len(df)):
        d = _to_date(df.iat[i, date_col])
        if d is None:
            continue
        for j, cc, prod, tax in cols:
            v = pd.to_numeric(str(df.iat[i, j]).replace(",", "").strip(), errors="coerce")
            recs.append((cc, prod, tax, d, None if pd.isna(v) else float(v)))
    out = pd.DataFrame(recs, columns=["country", "product", "tax", "obs_date", "raw_value"])
    return _apply_unit(out, unit)


def _parse_blocks(df: pd.DataFrame, tax: str) -> pd.DataFrame | None:
    # header row: contains at least two product names
    hdr_row = None
    for i in range(min(40, len(df))):
        hits = [_match_product(c) for c in df.iloc[i].values]
        if sum(h is not None for h in hits) >= 2:
            hdr_row = i
            break
    if hdr_row is None:
        return None
    colmap = {}
    for j, c in enumerate(df.iloc[hdr_row].values):
        p = _match_product(c)
        if p and p not in colmap.values():
            colmap[j] = p
    unit = _find_unit(df)
    recs = []
    country = None
    for i in range(hdr_row + 1, len(df)):
        first = df.iat[i, 0]
        d = _to_date(first)
        if d is None:
            label = str(first).strip()
            if re.fullmatch(r"[A-Z]{2}|EU|EUR|EU27", label):
                country = label
            elif label.lower() in COUNTRY_NAMES:
                country = COUNTRY_NAMES[label.lower()]
            continue
        if country is None:
            continue
        for j, prod in colmap.items():
            v = pd.to_numeric(str(df.iat[i, j]).replace(",", "").strip(), errors="coerce")
            recs.append((country, prod, tax, d, None if pd.isna(v) else float(v)))
    if not recs:
        return None
    out = pd.DataFrame(recs, columns=["country", "product", "tax", "obs_date", "raw_value"])
    return _apply_unit(out, unit)


def _apply_unit(out: pd.DataFrame, unit: str | None) -> pd.DataFrame:
    if unit == "EUR/L":
        out["value_eur_per_litre"] = out["raw_value"]
        out["unit_source"] = "sheet text: EUR per litre"
        return out
    vals = out["raw_value"].dropna()
    if unit == "EUR/1000L":
        out["unit_source"] = "sheet text: EUR per 1000 litres (divided by 1000)"
    else:
        med = vals.median() if not vals.empty else float("nan")
        if not (PLAUSIBLE_PER_1000L[0] <= med <= PLAUSIBLE_PER_1000L[1]):
            raise SchemaChanged(f"Oil Bulletin unit not stated in sheet and magnitude (median {med}) is not EUR/1000L-like; refusing to guess")
        out["unit_source"] = "INFERRED: no unit text in sheet; magnitudes consistent with EUR per 1000 litres (divided by 1000)"
    out["value_eur_per_litre"] = out["raw_value"] / 1000.0
    return out


def _validate_magnitudes(long: pd.DataFrame):
    for (prod, tax), g in long.groupby(["product", "tax"]):
        med = g["value_eur_per_litre"].dropna().median()
        if pd.isna(med):
            continue
        if not (0.2 <= med <= 5.0):
            raise SchemaChanged(f"implausible {prod} {tax} median {med:.3f} EUR/L — unit or column shift suspected")


def parse_latest_workbook(content: bytes, countries: list[str], products: list[str]) -> pd.DataFrame:
    """Single-week workbook: country rows, product columns, 'prices in force on <date>' header."""
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl")
    except Exception as e:  # noqa: BLE001
        raise SchemaChanged(f"Oil Bulletin weekly workbook unreadable: {e}") from e
    frames = []
    for name, df in sheets.items():
        text = " ".join(str(x) for x in df.head(12).fillna("").values.ravel())
        m = re.search(r"(\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{4}-\d{2}-\d{2})", text)
        if not m:
            continue
        d = _to_date(m.group(1).replace(".", "/"))
        tax = _sheet_tax(name) or _sheet_tax(text) or None
        if d is None or tax is None:
            continue
        hdr_row = None
        for i in range(min(25, len(df))):
            if sum(_match_product(c) is not None for c in df.iloc[i].values) >= 2:
                hdr_row = i
                break
        if hdr_row is None:
            continue
        colmap = {}
        for j, c in enumerate(df.iloc[hdr_row].values):
            p = _match_product(c)
            if p and p not in colmap.values():
                colmap[j] = p
        unit = _find_unit(df)
        recs = []
        for i in range(hdr_row + 1, len(df)):
            label = str(df.iat[i, 0]).strip()
            cc = COUNTRY_NAMES.get(label.lower()) or (label if re.fullmatch(r"[A-Z]{2}|EU|EUR", label) else None)
            if label.lower().startswith(("eu average", "eu weighted", "eu 27", "eu27", "moyenne")):
                cc = "EU"
            if not cc:
                continue
            for j, prod in colmap.items():
                v = pd.to_numeric(str(df.iat[i, j]).replace(",", "").strip(), errors="coerce")
                recs.append((cc, prod, tax, d, None if pd.isna(v) else float(v)))
        if recs:
            frames.append(_apply_unit(pd.DataFrame(recs, columns=["country", "product", "tax", "obs_date", "raw_value"]), unit))
    if not frames:
        raise SchemaChanged("Oil Bulletin weekly workbook layout not recognised")
    long = pd.concat(frames, ignore_index=True)
    long["country"] = long["country"].replace({a: "EU" for a in EU_ALIASES})
    long = long[long["country"].isin(countries) & long["product"].isin(products)]
    _validate_magnitudes(long)
    return long


def series_id(cc: str, prod: str, tax: str) -> str:
    return f"oil_bulletin.{cc}.{prod}.{tax}"


PRODUCT_NAMES = {"euro95": "Euro-super 95 petrol", "diesel": "automotive diesel", "heating_oil": "heating gas oil"}


class OilBulletinCollector(Collector):
    key = "oil_bulletin"
    connector = "oil_bulletin"

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        countries, products = self.cfg["countries"], self.cfg["products"]
        attribution = self.ctx.policy.connector("oil_bulletin").meta["attribution"]
        with self.ctx.client(self.connector) as client:
            page = client.get(self.cfg["page"])
            result.n_requests += 1
            wh.store_raw("oil_bulletin", page.content, url=page.url, retrieved_at=page.retrieved_at,
                         content_type=page.content_type, ext="html", note="bulletin landing page")
            links = discover_links(page.text(), self.cfg["page"])
            wanted = [l for l in links if l.kind == "history"]
            if not wanted:
                wanted = [l for l in links if l.kind.startswith("latest")]
            if not wanted:
                raise SchemaChanged(f"no history or latest workbook link recognised among {[l.filename for l in links][:6]}")
            frames = []
            for link in wanted:
                r = client.get(link.url, conditional=True)
                result.n_requests += 1
                if r.not_modified:
                    continue
                sha = wh.store_raw("oil_bulletin", r.content, url=r.url, final_url=r.final_url, retrieved_at=r.retrieved_at,
                                   content_type=r.content_type, ext="xlsx", etag=r.headers.get("etag"),
                                   last_modified=r.headers.get("last-modified"), note=f"{link.kind}: {link.filename}")
                result.raw.append(sha)
                since = date.fromisoformat(self.cfg.get("history_since", "2015-01-01"))
                if link.kind == "history":
                    df = parse_history_workbook(r.content, countries, products, since)
                else:
                    df = parse_latest_workbook(r.content, countries, products)
                df["raw_sha"] = sha
                df["filename"] = link.filename
                frames.append(df)
        if not frames:
            result.status = "not_modified"
            return
        long = pd.concat(frames, ignore_index=True).drop_duplicates(["country", "product", "tax", "obs_date"], keep="first")
        for (cc, prod, tax), g in long.groupby(["country", "product", "tax"]):
            sid = series_id(cc, prod, tax)
            wh.upsert_series(
                sid, source="EU Oil Bulletin", source_key=f"{cc}_{prod}_{tax}",
                name=f"{cc} {PRODUCT_NAMES[prod]} retail price {'incl.' if tax == 'with_tax' else 'excl.'} taxes",
                geography=cc, product=prod, unit="EUR per litre", frequency="weekly",
                description="Weekly Oil Bulletin; observation = Monday 'prices in force on' date; published later (normally Thursday)",
                metadata={"unit_source": g["unit_source"].iloc[0], "attribution": attribution, "tax_basis": tax},
            )
            rows = []
            for _, rr in g.sort_values("obs_date").iterrows():
                d = rr["obs_date"]
                v = rr["value_eur_per_litre"]
                rows.append({"obs_start": d, "obs_end": d, "value": None if pd.isna(v) else float(v),
                             "attrs": {"raw_value": None if pd.isna(rr["raw_value"]) else float(rr["raw_value"]),
                                       "file": rr["filename"],
                                       "expected_publication": str(d + timedelta(days=3)) + " (normally Thursday; not provider-stated)"}})
                result.saw(d)
            result.add(sid, wh.ingest_observations(sid, rows, raw_sha256=g["raw_sha"].iloc[0]))

    def probe(self):
        with self.ctx.client(self.connector) as client:
            page = client.get(self.cfg["page"])
            links = discover_links(page.text(), self.cfg["page"])
            hist = [l for l in links if l.kind == "history"] or [l for l in links if l.kind.startswith("latest")]
            if not hist:
                return False, f"page reachable but no recognised workbook among {[l.filename for l in links][:5]}"
            r = client.get(hist[0].url)
        df = (parse_history_workbook if hist[0].kind == "history" else parse_latest_workbook)(
            r.content, self.cfg["countries"], self.cfg["products"])
        return True, f"{hist[0].filename}: latest observation {df['obs_date'].max()} ({len(df)} rows)"
