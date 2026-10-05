"""Evidence cards: headline -> indicators, anomaly -> reporting, and plain context ("normal") cards.

A card is private research material. It records: the question tested, measurements with observation periods,
previous value, baseline, absolute/percentage change, sample/coverage, the relationship (supports /
complicates / insufficient_evidence / context_only), alternative explanations, contrary observations,
chart references, immutable input version ids, a follow-up question and a short draft note.
Evidence quality, freshness and editorial relevance are reported SEPARATELY; there is no single
"probability of truth".
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ..settings import env, sources_config, topics_config
from ..storage.warehouse import Warehouse, now_utc, observations, series_info, sha1
from . import matching, narrative

KEY_CONTEXT_SERIES = ["eia.brent_spot", "derived.brent_eur_per_barrel", "portwatch.hormuz.n_tanker",
                      "oil_bulletin.DE.diesel.with_tax", "oil_bulletin.DE.diesel.without_tax", "derived.cost_wedge.DE.diesel",
                      "eia.us_commercial_crude_stocks"]
MIN_MOVE_PCT = {"daily": 1.0, "weekly": 1.0, "monthly": 2.0}


def _digits(unit: str) -> int:
    u = (unit or "").lower()
    if "transit" in u or "thousand barrels" in u or "tonnes" in u:
        return 0
    if "per litre" in u:
        return 3
    if "usd per eur" in u:
        return 4
    return 2


def _source_key(sid: str, frequency: str | None = None) -> str:
    if sid.startswith("ecb."):
        return "ecb_fx"
    if sid.startswith("eia."):
        return "eia_weekly" if frequency == "weekly" else "eia_spot"
    if sid.startswith("derived.brent"):
        return "eia_spot"
    if "oil_bulletin" in sid or sid.startswith("derived.cost_wedge"):
        return "oil_bulletin"
    return sid.split(".")[0]


def _stale_days(sid: str, frequency: str | None = None) -> int:
    cfg = sources_config()["sources"].get(_source_key(sid, frequency), {})
    return int(cfg.get("stale_after_days", {"daily": 7, "weekly": 14, "monthly": 100}.get(frequency or "", 14)))


def _event_time(h: dict) -> tuple[datetime, str]:
    if h.get("published_at") is not None and not pd.isna(h.get("published_at")):
        return pd.Timestamp(h["published_at"]).to_pydatetime(), "publisher-stated publication time"
    return pd.Timestamp(h["discovered_at"]).to_pydatetime(), "discovery time (publication time not provided)"


def _latest_anomaly(con, sid: str, start: date, end: date) -> dict | None:
    r = con.execute(
        "SELECT anomaly_id, rule_id, fired, explanation, stats, obs_end FROM anomalies WHERE series_id=? AND status='active' "
        "AND obs_end BETWEEN ? AND ? ORDER BY fired DESC, obs_end DESC LIMIT 1", [sid, start, end]).fetchone()
    if not r:
        return None
    return {"anomaly_id": r[0], "rule_id": r[1], "fired": bool(r[2]), "explanation": r[3],
            "stats": json.loads(r[4]), "obs_end": str(r[5])}


def measurement(con, sid: str, event: datetime | None, as_of=None) -> dict | None:
    info = series_info(con, sid)
    if not info:
        return None
    df = observations(con, sid, as_of=as_of)
    freq = info.get("frequency", "daily")
    w = matching.window_for(freq)
    m = {"series_id": sid, "name": info["name"], "unit": info["unit"], "source": info["source"], "frequency": freq,
         "digits": _digits(info["unit"]), "latest_value": None, "inputs": []}
    obs = df.dropna(subset=["value"]) if not df.empty else df
    if obs.empty:
        m["note"] = "no non-missing observations stored"
        return m
    ev = pd.Timestamp(event.date()) if event else None
    if ev is not None:
        before = obs[obs["obs_end"] < ev]
        after = obs[(obs["obs_end"] >= ev) & (obs["obs_end"] <= ev + timedelta(days=w["after_days"]))]
        ctx = obs[(obs["obs_end"] >= ev - timedelta(days=w["before_days"])) & (obs["obs_end"] < ev)]
        m["post_event"] = not after.empty
        cur = after.iloc[-1] if not after.empty else obs.iloc[-1]
        # a change is only measured ACROSS the event; with no post-event data we show the pre-event reading only
        prev = before.iloc[-1] if (not before.empty and not after.empty) else None
        if after.empty:
            m["note"] = (f"latest observation ({obs.iloc[-1]['obs_end'].date()}) precedes the headline date "
                         f"({ev.date()}); cannot test the reported event yet")
        m["context_window"] = {"from": str((ev - timedelta(days=w['before_days'])).date()), "to": str(ev.date()),
                               "n": int(len(ctx)), "median": float(ctx["value"].median()) if not ctx.empty else None,
                               "min": float(ctx["value"].min()) if not ctx.empty else None,
                               "max": float(ctx["value"].max()) if not ctx.empty else None}
    else:
        cur = obs.iloc[-1]
        prev = obs.iloc[-2] if len(obs) > 1 else None
        m["post_event"] = None
        ctx = obs[obs["obs_end"] >= cur["obs_end"] - timedelta(days=w["before_days"])].iloc[:-1]
        m["context_window"] = {"from": str(ctx["obs_end"].min().date()) if not ctx.empty else None,
                               "to": str(cur["obs_end"].date()), "n": int(len(ctx)),
                               "median": float(ctx["value"].median()) if not ctx.empty else None,
                               "min": float(ctx["value"].min()) if not ctx.empty else None,
                               "max": float(ctx["value"].max()) if not ctx.empty else None}
    m.update({"latest_value": float(cur["value"]), "latest_date": str(cur["obs_end"].date()),
              "latest_period": f"{cur['obs_start'].date()}..{cur['obs_end'].date()}",
              "latest_first_seen": str(cur["first_seen_at"]), "latest_revision": int(cur["revision_no"])})
    m["inputs"].append(cur["version_id"])
    if prev is not None:
        m.update({"previous_value": float(prev["value"]), "previous_date": str(prev["obs_end"].date())})
        m["abs_change"] = m["latest_value"] - m["previous_value"]
        m["pct_change"] = (m["abs_change"] / m["previous_value"] * 100) if m["previous_value"] else None
        m["inputs"].append(prev["version_id"])
    age = (datetime.now(timezone.utc).date() - cur["obs_end"].date()).days
    m["observation_age_days"] = age
    m["freshness"] = "fresh" if age <= _stale_days(sid, freq) else "stale"
    win_start = (ev - timedelta(days=w["before_days"])).date() if ev is not None else cur["obs_end"].date() - timedelta(days=w["before_days"])
    win_end = (ev + timedelta(days=w["after_days"])).date() if ev is not None else cur["obs_end"].date()
    m["anomaly"] = _latest_anomaly(con, sid, win_start, win_end)
    if m["anomaly"]:
        m["inputs"].append(m["anomaly"]["anomaly_id"])
    meta = json.loads(info.get("metadata") or "{}")
    m["attribution"] = meta.get("attribution", info["source"])
    if meta.get("limitations"):
        m["limitations"] = meta["limitations"]
    return m


def _direction(m: dict) -> str | None:
    if m.get("pct_change") is None:
        return None
    thr = MIN_MOVE_PCT.get(m["frequency"], 1.0)
    if abs(m["pct_change"]) < thr:
        return "flat"
    return "up" if m["pct_change"] > 0 else "down"


def _assess_disruption(post: list[dict], contrary: list[str]):
    ship = [m for m in post if m["series_id"].startswith("portwatch.")]
    if not ship:
        return None
    a = ship[0].get("anomaly")
    if a and a["fired"] and (a["stats"].get("baseline_median") or 0) > (ship[0]["latest_value"] or 0):
        return "supports", f"{ship[0]['name']}: the 7-day mean fell below the fixed baseline band (rule fired)."
    if a and not a["fired"] and not a["explanation"].startswith(("suppressed", "not evaluated")):
        contrary.append(f"{ship[0]['name']} stayed within its baseline band: {a['explanation'][:160]}")
        return "complicates", "Reported disruption is not (yet) visible in aggregate transit counts."
    return "insufficient_evidence", "Post-event shipping data are incomplete or not evaluable."


def _assess_direction(claim: str, post: list[dict], contrary: list[str]):
    prices = [m for m in post if not m["series_id"].startswith("portwatch.")]
    if not prices:
        return None
    primary = prices[0]
    for m in prices[1:]:
        dm = _direction(m)
        if dm and dm not in ("flat", claim):
            contrary.append(f"{m['name']} moved {dm} ({narrative.fmt(m.get('pct_change'), 1, signed=True, pct=True)})")
        elif dm == "flat":
            contrary.append(f"{m['name']} barely changed ({narrative.fmt(m.get('pct_change'), 1, signed=True, pct=True)})")
    d = _direction(primary)
    if d is None:
        return "insufficient_evidence", "No comparable pre-event observation to measure a change."
    if d == claim:
        fired = primary.get("anomaly") and primary["anomaly"]["fired"]
        return "supports", (f"{primary['name']} moved {d} ({narrative.fmt(primary['pct_change'], 1, signed=True, pct=True)})"
                            + ("; screening rule fired" if fired else "; no screening rule fired, so the move is within normal variability"))
    return "complicates", (f"Headline implies '{claim}', but {primary['name']} moved {d} "
                           f"({narrative.fmt(primary['pct_change'], 1, signed=True, pct=True)}).")


def assess(claims_found, ms: list[dict]) -> tuple[str, str, list[str]]:
    """Return (relationship, reason, contrary observations). Each claim type is tested only against the
    indicators that can measure it; mixed results are reported as 'complicates', never averaged away."""
    if isinstance(claims_found, str) or claims_found is None:
        claims_found = {claims_found} if claims_found else set()
    post = [m for m in ms if m and m.get("post_event") and m.get("latest_value") is not None]
    contrary: list[str] = []
    if not post:
        return ("insufficient_evidence",
                "No relevant observation is dated on/after the reported event; all available data precede it.", contrary)
    results = []
    if "disruption" in claims_found:
        r = _assess_disruption(post, contrary)
        if r:
            results.append(r)
        else:
            contrary.append("No post-event shipping observation yet (provider lag) — the disruption claim is untested.")
    for c in ("up", "down"):
        if c in claims_found:
            r = _assess_direction(c, post, contrary)
            if r:
                results.append(r)
    if not results:
        return ("context_only", "The headline makes no directional claim that the available post-event indicators can test.", contrary)
    rels = {r[0] for r in results}
    reason = " ".join(r[1] for r in results)
    if "supports" in rels and "complicates" in rels:
        return "complicates", "Mixed: " + reason, contrary
    for rel in ("complicates", "supports", "insufficient_evidence"):
        if rel in rels:
            return rel, reason, contrary
    return "context_only", reason, contrary


def _quality(ms: list[dict], hits: int, cluster_n: int, geography: str) -> dict:
    post = [m for m in ms if m and m.get("post_event")]
    evaluable = [m for m in post if m.get("anomaly") and m["anomaly"]["stats"].get("method") not in (None, "suppressed")]
    eq = "high" if evaluable else ("medium" if post else "low")
    fresh = "fresh" if any(m.get("freshness") == "fresh" for m in ms if m) else "stale"
    rel = "high" if (hits >= 2 and (cluster_n >= 2 or "germany" in geography.lower() or "eu" in geography.lower())) else ("medium" if hits >= 1 else "low")
    return {"evidence_quality": eq, "evidence_quality_reason": f"{len(post)} post-event official measurement(s), {len(evaluable)} with a statistical baseline",
            "freshness": fresh, "editorial_relevance": rel,
            "editorial_relevance_reason": f"{hits} keyword hit(s); syndication cluster size {cluster_n}; geography {geography}"}


VOLATILE_KEYS = {"created_at", "narrative", "narrative_source", "review_flag", "observation_age_days", "freshness",
                 "narrative_llm_draft", "local_llm_error", "supersedes_version"}


def _strip_volatile(x):
    if isinstance(x, dict):
        return {k: _strip_volatile(v) for k, v in x.items() if k not in VOLATILE_KEYS}
    if isinstance(x, list):
        return [_strip_volatile(v) for v in x]
    return x


def _hash_payload(card: dict) -> str:
    """Content hash over everything except volatile fields (age, wording), so a new version means new evidence."""
    return hashlib.sha256(json.dumps(_strip_volatile(card), sort_keys=True, default=str).encode()).hexdigest()[:20]


def save_card(wh: Warehouse, card: dict) -> tuple[int, bool]:
    """Version cards by content: identical content -> no new version; changed inputs -> new version,
    previous version marked superseded (history kept)."""
    h = _hash_payload(card)
    cur = wh.con.execute("SELECT version, content_hash FROM evidence_cards WHERE card_id=? AND status='current'",
                         [card["card_id"]]).fetchone()
    if cur and cur[1] == h:
        return cur[0], False
    version = (cur[0] + 1) if cur else 1
    if cur:
        wh.con.execute("UPDATE evidence_cards SET status='superseded' WHERE card_id=? AND version=?", [card["card_id"], cur[0]])
        card["supersedes_version"] = cur[0]
    wh.con.execute(
        "INSERT INTO evidence_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [card["card_id"], version, card["kind"], card["subject_id"], now_utc(), "current", card["relationship"],
         card["question"], json.dumps(card, default=str), card["narrative"], card["narrative_source"], card.get("review_flag"),
         json.dumps(card["input_version_ids"]), json.dumps(card.get("topics", [])), card.get("geography", ""),
         card.get("signal_strength", "none"), h])
    return version, True


def _finish(card: dict, ctx_client=None) -> dict:
    card["narrative"] = narrative.template_note(card)
    card["narrative_source"] = "template"
    issues = narrative.validate_narrative(card["narrative"], card["calculations"],
                                          [card.get("headline", {}).get("title", ""), card["question"]])
    card["review_flag"] = ("template validation issues: " + "; ".join(issues)) if issues else None
    model = env("OCO_LOCAL_LLM_MODEL")
    if model and ctx_client is not None:
        try:
            text, llm_issues = narrative.local_llm_note(card, ctx_client, model)
            if text:
                card["narrative_llm_draft"] = text
                card["narrative_source"] = "template (local-model draft attached, validated)"
            else:
                card["review_flag"] = (card["review_flag"] or "") + " local-model draft rejected: " + "; ".join(llm_issues)[:300]
        except Exception as e:  # noqa: BLE001 — local model is optional; template remains
            card["local_llm_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return card


# ---------------------------------------------------------------------------------------------
def headline_card(con, h: dict, as_of=None) -> dict | None:
    text = f"{h['title']} {h.get('excerpt') or ''}"
    topics = matching.match_topics(text)
    if not topics:
        return None
    tcfg = topics_config()["topics"]
    event, event_basis = _event_time(h)
    claim_set = matching.claims(h["title"])
    claim = "up" if "up" in claim_set else "down" if "down" in claim_set else ("disruption" if "disruption" in claim_set else None)
    sids = []
    for t in topics:
        for s in tcfg[t].get("series", []):
            if s not in sids:
                sids.append(s)
    ms = [m for m in (measurement(con, s, event, as_of) for s in sids) if m and m.get("latest_value") is not None]
    rel, reason, contrary = assess(claim_set, ms)
    cluster_n = con.execute("SELECT n_members FROM headline_clusters WHERE cluster_id=?", [h.get("cluster_id")]).fetchone()
    geography = ", ".join(sorted({tcfg[t]["geography"] for t in topics}))
    hits = sum(len(v) for v in topics.values())
    alts = []
    for t in topics:
        alts += [a for a in tcfg[t].get("alternatives", []) if a not in alts]
    facs = matching.facilities_mentioned(text)
    sat_note = None
    if any(tcfg[t].get("satellite") for t in topics):
        if facs and any(f["properties"].get("verified") for f in facs):
            sat_note = "Facility is configured and verified: see Satellite Review for thermal timeline and imagery coverage."
        elif facs:
            sat_note = (f"Facility mentioned ({facs[0]['properties']['name']}) but its boundary is NOT verified in "
                        "config/facilities.geojson; satellite checks are not run for unverified coordinates.")
        else:
            sat_note = "No configured facility matched; satellite checks need a verified facility boundary."
    temporal = None
    if rel == "insufficient_evidence" and ms and not any(m.get("post_event") for m in ms):
        latest = max(m["latest_date"] for m in ms)
        temporal = (f"Temporal mismatch: the newest relevant observation is dated {latest}, before the headline's "
                    f"{event_basis} ({event.date()}). Observations that precede an event cannot test it. "
                    "Re-check after the next releases.")
    question = _question(h["title"], claim, topics)
    limitations = ["Correlation in time is not evidence of cause.",
                   "News text is untrusted input; it was matched by keywords only."]
    for m in ms:
        limitations += [l for l in m.get("limitations", []) if l not in limitations]
    if any(m["source"] == "EIA" for m in ms):
        limitations.append("EIA data describe the United States only.")
    calcs = {"measurements": [{k: v for k, v in m.items() if k not in ("inputs",)} for m in ms],
             "relationship_reason": reason, "temporal_note": temporal, "claims_detected": sorted(claim_set),
             "event_time": str(event), "event_time_basis": event_basis}
    inputs = sorted({i for m in ms for i in m["inputs"]})
    card = {
        "card_id": "h-" + h["headline_id"], "kind": "headline", "subject_id": h["headline_id"],
        "created_at": str(now_utc()), "as_of": str(as_of) if as_of else None,
        "headline": {"title": h["title"], "url": h["url"], "publisher": h.get("publisher"),
                     "published_at": str(h.get("published_at")) if h.get("published_at") is not None and not pd.isna(h.get("published_at")) else None,
                     "discovered_at": str(h["discovered_at"]), "discovery_source": h.get("discovery_source")},
        "question": question, "topics": list(topics), "keyword_hits": topics, "geography": geography,
        "relationship": rel, "calculations": calcs, "contrary_observations": contrary, "alternative_explanations": alts,
        "satellite_note": sat_note, "limitations": limitations,
        "charts": [{"series_id": m["series_id"], "event_date": str(event.date())} for m in ms[:3]],
        "input_version_ids": inputs,
        "quality": _quality(ms, hits, cluster_n[0] if cluster_n else 1, geography),
        "signal_strength": "strong" if any(m.get("anomaly") and m["anomaly"]["fired"] for m in ms) else ("weak" if ms else "none"),
        "follow_up": _follow_up(rel, topics, ms),
    }
    return card


def _question(title: str, claim: str | None, topics: dict) -> str:
    labels = ", ".join(topics_config()["topics"][t]["label"] for t in topics)
    if claim == "disruption":
        return f"Do aggregate measurements ({labels}) after the report show the disruption described in: \"{title}\"?"
    if claim in ("up", "down"):
        return f"Did the relevant indicators ({labels}) move {claim} around the time of: \"{title}\"?"
    return f"What do the relevant indicators ({labels}) show around the time of: \"{title}\"?"


def _follow_up(rel: str, topics: dict, ms: list[dict]) -> str:
    if rel == "insufficient_evidence":
        nxt = sorted({m["frequency"] for m in ms}) or ["next"]
        return f"Re-run after the next {'/'.join(nxt)} releases; check whether post-event observations change the picture."
    if rel == "complicates":
        return "Which other sources (company statements, regional prices, satellite coverage) could explain the gap between the report and the data?"
    if "german_fuel_prices" in topics:
        return "How much of the change is taxes vs pretax price, and how does it compare with the euro crude cost lagged one week?"
    return "Is the move persistent over the next releases, and does it appear in independent series?"


def anomaly_card(con, a: dict) -> dict:
    info = series_info(con, a["series_id"])
    obs_end = pd.Timestamp(a["obs_end"])
    m = measurement(con, a["series_id"], None) or {}
    stats = json.loads(a["stats"]) if isinstance(a["stats"], str) else a["stats"]
    w = matching.window_for(info.get("frequency", "daily"))
    heads = matching.headlines_for_series(con, a["series_id"], (obs_end - timedelta(days=w["before_days"])).to_pydatetime(),
                                          (obs_end + timedelta(days=w["after_days"])).to_pydatetime())
    related = [{"title": r.title, "publisher": r.publisher, "url": r.url, "time": str(r.event_time),
                "time_basis": "published" if r.published_at is not None and not pd.isna(r.published_at) else "discovered",
                "relation": "contemporaneous reporting (context only, not an established cause)"} for r in heads.itertuples()] if not heads.empty else []
    tcfg = topics_config()["topics"]
    topics = [k for k, t in tcfg.items() if a["series_id"] in t.get("series", [])]
    normal = []
    for t in topics:
        for s in tcfg[t].get("series", []):
            if s == a["series_id"]:
                continue
            an = _latest_anomaly(con, s, (obs_end - timedelta(days=14)).date(), (obs_end + timedelta(days=7)).date())
            if an and not an["fired"]:
                normal.append(f"{s}: {an['explanation'][:150]}")
    alts = []
    for t in topics:
        alts += [x for x in tcfg[t].get("alternatives", []) if x not in alts]
    meas = {"series_id": a["series_id"], "name": info.get("name"), "unit": info.get("unit"), "frequency": info.get("frequency"),
            "digits": _digits(info.get("unit", "")), "latest_value": a["value"], "latest_date": str(obs_end.date()),
            "latest_period": f"{a['obs_start']}..{a['obs_end']}",
            "previous_value": stats.get("previous_value"), "previous_date": stats.get("previous_date"),
            "abs_change": stats.get("change", (a["value"] - stats["previous_value"]) if stats.get("previous_value") is not None and a["value"] is not None else None),
            "pct_change": stats.get("pct_change"),
            "anomaly": {"rule_id": a["rule_id"], "fired": bool(a["fired"]), "explanation": a["explanation"], "stats": stats},
            "note": "", "observation_age_days": m.get("observation_age_days")}
    calcs = {"measurements": [meas], "relationship_reason": "Screening rule fired; related reporting is listed for context only.",
             "baseline": json.loads(a["baseline"]) if isinstance(a["baseline"], str) else a["baseline"],
             "thresholds": json.loads(a["thresholds"]) if isinstance(a["thresholds"], str) else a["thresholds"],
             "formula": a["formula_version"]}
    inputs = sorted(set(json.loads(a["input_version_ids"]) + [a["anomaly_id"]]))
    card = {
        "card_id": "a-" + sha1(a["rule_id"], a["series_id"], a["obs_start"]), "kind": "anomaly", "subject_id": a["anomaly_id"],
        "created_at": str(now_utc()), "question": f"What explains the unusual reading in {info.get('name')} for {a['obs_start']}..{a['obs_end']}?",
        "topics": topics, "geography": info.get("geography", ""), "relationship": "context_only",
        "calculations": calcs, "related_reporting": related,
        "contrary_observations": normal or ["No related series evaluated in the same period."],
        "alternative_explanations": alts or ["Data revision or reporting artefact", "Seasonal or calendar effect"],
        "limitations": ["Screening thresholds are not calibrated probabilities.",
                        "Contemporaneous headlines are not evidence of cause."] + list(m.get("limitations", [])),
        "charts": [{"series_id": a["series_id"], "event_date": str(obs_end.date())}],
        "input_version_ids": inputs, "signal_strength": "strong",
        "quality": {"evidence_quality": "high" if stats.get("method") == "MAD" else "medium",
                    "evidence_quality_reason": f"baseline method {stats.get('method')}, n={stats.get('n_baseline')}",
                    "freshness": m.get("freshness", "unknown"), "editorial_relevance": "high" if related else "medium",
                    "editorial_relevance_reason": f"{len(related)} related headline cluster(s) in window"},
        "follow_up": "Does the reading persist in the next release, and is it visible in independent series?",
    }
    return card


def context_card(con, sid: str) -> dict | None:
    m = measurement(con, sid, None)
    if not m or m.get("latest_value") is None:
        return None
    a = m.get("anomaly")
    fired = bool(a and a["fired"])
    status = ("screening rule FIRED — see anomaly card" if fired else
              ("no screening rule fired (normal range)" if a else "no screening evaluation available"))
    calcs = {"measurements": [{k: v for k, v in m.items() if k != "inputs"}],
             "relationship_reason": f"Context reading; {status}."}
    card = {
        "card_id": "c-" + sid, "kind": "context", "subject_id": sid, "created_at": str(now_utc()),
        "question": f"Where does {m['name']} stand now relative to its recent range?",
        "topics": [k for k, t in topics_config()["topics"].items() if sid in t.get("series", [])],
        "geography": series_info(con, sid).get("geography", ""), "relationship": "context_only", "calculations": calcs,
        "contrary_observations": [], "alternative_explanations": [], "limitations": list(m.get("limitations", [])),
        "charts": [{"series_id": sid, "event_date": None}], "input_version_ids": sorted(m["inputs"]),
        "signal_strength": "strong" if fired else "none",
        "quality": {"evidence_quality": "high" if a and a["stats"].get("method") not in (None, "suppressed") else "medium",
                    "freshness": m.get("freshness"), "editorial_relevance": "context"},
        "follow_up": "Compare with the same period last year and with related series.",
    }
    return card


def build_cards(wh: Warehouse, headline_days: int = 14, ctx_client=None) -> dict:
    con = wh.con
    counts = {"headline": 0, "anomaly": 0, "context": 0, "new_versions": 0}
    since = now_utc() - timedelta(days=headline_days)
    heads = con.execute("SELECT * FROM headlines WHERE discovered_at >= ? OR origin='manual'", [since]).df()
    for h in heads.to_dict("records"):
        card = headline_card(con, h)
        if card:
            _, new = save_card(wh, _finish(card, ctx_client))
            counts["headline"] += 1
            counts["new_versions"] += int(new)
    an = con.execute("SELECT * FROM anomalies WHERE fired AND status='active' AND as_of IS NULL").df()
    for a in an.to_dict("records"):
        if json.loads(a["stats"]).get("continuation_of"):
            continue
        _, new = save_card(wh, _finish(anomaly_card(con, a), ctx_client))
        counts["anomaly"] += 1
        counts["new_versions"] += int(new)
    for sid in KEY_CONTEXT_SERIES:
        c = context_card(con, sid)
        if c:
            _, new = save_card(wh, _finish(c, ctx_client))
            counts["context"] += 1
            counts["new_versions"] += int(new)
    return counts
