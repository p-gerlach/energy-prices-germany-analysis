"""Transparent keyword/alias matching between headlines and indicators (both directions).

Headline text is untrusted data: it is lower-cased and searched for configured keywords; nothing in it is
ever executed or treated as an instruction. Matching = "relevant to compare", never "explains".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pandas as pd

from .. import geo
from ..settings import topics_config


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", (text or "").lower()) + " "


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    t = _norm(text)
    hits = []
    for k in keywords:
        k2 = k.lower()
        # short tokens: whole word. Longer ones: must START a word (so German compounds such as
        # "Dieselpreise" match "diesel"), but never match inside a word ("rebrand" must not match "brand").
        if len(k2) <= 4:
            pat = rf"(?<![\wäöüß]){re.escape(k2)}(?![\wäöüß])"
        else:
            pat = rf"(?<![\wäöüß]){re.escape(k2)}"
        if re.search(pat, t):
            hits.append(k)
    return hits


def match_topics(text: str) -> dict[str, list[str]]:
    topics = topics_config()["topics"]
    out = {}
    for key, t in topics.items():
        kws = list(t["keywords"])
        required = list(t.get("require_any", []))
        if key == "refinery_disruption":
            aliases = [a for f in geo.facilities() for a in f["properties"].get("aliases", [])]
            kws += aliases
            required += aliases
        hits = keyword_hits(text, kws)
        # generic event words ("fire", "Brand", "exports") only count when a topic anchor word is also present
        if hits and (not required or keyword_hits(text, required)):
            out[key] = hits
    return out


def claims(text: str) -> set[str]:
    """All claim types present: subset of {'up','down','disruption'}; 'up' and 'down' together cancel."""
    dw = topics_config()["direction_words"]
    t = _norm(text)
    found = {k for k, words in dw.items() if any(w.lower() in t for w in words)}
    if {"up", "down"} <= found:
        found -= {"up", "down"}
        found.add("mixed")
    return found


def claimed_direction(text: str) -> str | None:
    dw = topics_config()["direction_words"]
    t = _norm(text)
    found = {k for k, words in dw.items() if any(w.lower() in t for w in words)}
    if "up" in found and "down" in found:
        return "mixed"
    for k in ("disruption", "up", "down"):
        if k in found:
            return k
    return None


def facilities_mentioned(text: str) -> list[dict]:
    out = []
    for f in geo.facilities():
        if keyword_hits(text, f["properties"].get("aliases", [])):
            out.append(f)
    return out


def window_for(freq: str) -> dict:
    w = topics_config()["windows"]
    return w.get(freq, w["daily"])


def headlines_for_series(con, series_id: str, start: datetime, end: datetime, limit: int = 15) -> pd.DataFrame:
    """Anomaly -> news: headlines in [start, end] matching any topic linked to this series."""
    topics = topics_config()["topics"]
    linked = [k for k, t in topics.items() if series_id in t.get("series", [])]
    if series_id.startswith("oil_bulletin.") or series_id.startswith("derived.cost_wedge") or series_id.startswith("derived.oil_bulletin"):
        linked.append("german_fuel_prices")
    if series_id.startswith("portwatch."):
        linked += ["hormuz_shipping"] if ".hormuz." in series_id else ["red_sea_shipping"]
    if not linked:
        return pd.DataFrame()
    df = con.execute(
        "SELECT headline_id, title, publisher, url, published_at, discovered_at, cluster_id, excerpt FROM headlines "
        "WHERE COALESCE(published_at, discovered_at) BETWEEN ? AND ?", [start, end]).df()
    if df.empty:
        return df
    kws = []
    for k in set(linked):
        kws += topics[k]["keywords"]
    df["hits"] = df.apply(lambda r: keyword_hits(f"{r['title']} {r['excerpt'] or ''}", kws), axis=1)
    df = df[df["hits"].map(len) > 0].copy()
    if df.empty:
        return df
    df["n_hits"] = df["hits"].map(len)
    df["event_time"] = df["published_at"].fillna(df["discovered_at"])
    df = df.sort_values(["n_hits", "event_time"], ascending=[False, False]).drop_duplicates("cluster_id")
    return df.head(limit)
