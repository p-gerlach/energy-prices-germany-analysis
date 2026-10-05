"""Reproducible 16:9 video exports (Matplotlib PNG + SVG), the CSV of exactly the plotted observations,
and a source/method note (title, units, baseline, observation dates, retrieval dates, attribution, raw hashes).

Rules enforced here:
* one y-axis per panel; series with different units go in separate stacked panels (never dual axes)
* radar SNAPSHOT counts can never be exported with daily-transit units (UnitMismatch)
* missing observations are gaps in the line, never zeros
* demo-mode exports carry a SYNTHETIC watermark
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .storage.warehouse import observations, series_info  # noqa: E402

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a"]  # validated reference slots 1-3 (all-pairs safe)
TEXT, TEXT2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e4e3df"
W, H, DPI = 16, 9, 120  # 1920 x 1080 px


class UnitMismatch(Exception):
    pass


def check_units(units: list[str]):
    """Refuse combinations that would mislabel radar snapshot occupancy as daily transits."""
    low = [u.lower() for u in units]
    snap = [u for u in low if "snapshot" in u]
    if snap and any(("per day" in u or "transit" in u) for u in low if u not in snap):
        raise UnitMismatch("radar snapshot counts cannot share an export with daily transit units; export separately")
    for u in snap:
        if "per day" in u or "transit" in u:
            raise UnitMismatch(f"snapshot counts cannot carry daily-transit units: {u!r}")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=TEXT2, labelsize=13)


def export_series(con, series_ids: list[str], out_dir: Path, title: str, start: str | None = None, end: str | None = None,
                  event_date: str | None = None, baseline: tuple[str, str] | None = None, demo: bool = False,
                  subtitle: str = "") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames, infos = [], []
    for sid in series_ids:
        info = series_info(con, sid)
        if not info:
            continue
        df = observations(con, sid)
        if df.empty:
            continue
        if start:
            df = df[df["obs_end"] >= pd.Timestamp(start)]
        if end:
            df = df[df["obs_end"] <= pd.Timestamp(end)]
        if df.empty:
            continue
        df = df.assign(series_id=sid, series_name=info["name"], unit=info["unit"])
        frames.append(df)
        infos.append(info)
    if not frames:
        raise ValueError(f"no observations to export for {series_ids}")
    check_units([i["unit"] for i in infos])
    # group by unit -> one panel per unit
    units = list(dict.fromkeys(i["unit"] for i in infos))
    fig, axes = plt.subplots(len(units), 1, figsize=(W, H), dpi=DPI, sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    color_of = {i["series_id"]: PALETTE[k % len(PALETTE)] for k, i in enumerate(infos)}
    for ax, unit in zip(axes[:, 0], units):
        _style(ax)
        for df in frames:
            if df["unit"].iloc[0] != unit:
                continue
            sid = df["series_id"].iloc[0]
            s = df.set_index("obs_end")["value"]
            if infos[0]["frequency"] == "daily":
                s = s.asfreq("D")  # gaps stay NaN -> broken line, never zero
            is_transit = "transit" in unit.lower()
            ax.plot(s.index, s.values, color=color_of[sid], linewidth=1.2 if is_transit else 2, alpha=0.55 if is_transit else 1,
                    label=df["series_name"].iloc[0] + (" (daily)" if is_transit else ""))
            if is_transit:
                r7 = s.rolling(7, min_periods=6).mean()  # needs >=6 of 7 days; missing days are not zero
                ax.plot(r7.index, r7.values, color=PALETTE[1], linewidth=2.5, label="7-day mean (needs ≥6 observed days)")
            last = s.dropna()
            if not last.empty:
                ax.plot([last.index[-1]], [last.iloc[-1]], "o", color=color_of[sid], markersize=8,
                        markeredgecolor=SURFACE, markeredgewidth=2)
                ax.annotate(f"{last.iloc[-1]:,.3g}  ({last.index[-1]:%d %b %Y})", (last.index[-1], last.iloc[-1]),
                            xytext=(8, 0), textcoords="offset points", va="center", fontsize=13, color=TEXT)
        if baseline:
            ax.axvspan(pd.Timestamp(baseline[0]), pd.Timestamp(baseline[1]), color="#8a8984", alpha=0.10, lw=0)
        if event_date:
            ax.axvline(pd.Timestamp(event_date), color=TEXT2, linestyle="--", linewidth=1.2)
        ax.set_ylabel(unit, fontsize=13, color=TEXT2)
        if len([d for d in frames if d["unit"].iloc[0] == unit]) > 1 or len(units) > 1 or "transit" in unit.lower():
            ax.legend(frameon=False, fontsize=13, labelcolor=TEXT, loc="upper left")
    axes[-1, 0].xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))
    obs_from = min(f["obs_start"].min() for f in frames).date()
    obs_to = max(f["obs_end"].max() for f in frames).date()
    fig.suptitle(title, x=0.06, y=0.97, ha="left", fontsize=24, fontweight="bold", color=TEXT)
    sub = subtitle or f"Observations {obs_from} to {obs_to}"
    if baseline:
        sub += f" · shaded: baseline {baseline[0]} to {baseline[1]}"
    if event_date:
        sub += f" · dashed: {event_date}"
    fig.text(0.06, 0.915, sub, fontsize=14, color=TEXT2)
    attributions = sorted({json.loads(i.get("metadata") or "{}").get("attribution", i["source"]) for i in infos})
    retrieved = max(pd.Timestamp(f["retrieved_at"].max()) for f in frames)
    foot = " | ".join(attributions) + f" · retrieved up to {retrieved:%Y-%m-%d %H:%M} UTC · Oil Crisis Observatory"
    fig.text(0.06, 0.02, foot, fontsize=11, color=TEXT2)
    if demo:
        fig.text(0.5, 0.5, "SYNTHETIC DEMO DATA — NOT LIVE", fontsize=48, color="#e34948", alpha=0.25,
                 ha="center", va="center", rotation=20)
    fig.tight_layout(rect=(0.04, 0.05, 0.98, 0.9))
    stem = out_dir / _slug(title)
    fig.savefig(f"{stem}.png", facecolor=SURFACE)
    fig.savefig(f"{stem}.svg", facecolor=SURFACE)
    plt.close(fig)
    data = pd.concat(frames)[["series_id", "series_name", "unit", "obs_start", "obs_end", "value", "status", "revision_no",
                              "version_id", "first_seen_at", "retrieved_at", "source_published_at", "raw_sha256"]]
    data.to_csv(f"{stem}.csv", index=False)
    raws = sorted({r for r in data["raw_sha256"].dropna()})
    note = [f"# {title}", "", f"- Series: " + "; ".join(f"`{i['series_id']}` — {i['name']} [{i['unit']}]" for i in infos),
            f"- Observation dates: {obs_from} to {obs_to} (observation period, not publication date)",
            f"- Retrieved (UTC): up to {retrieved:%Y-%m-%d %H:%M}",
            f"- Baseline: {baseline[0]} to {baseline[1]}" if baseline else "- Baseline: none shown",
            f"- Event marker: {event_date}" if event_date else "- Event marker: none",
            f"- Sources/attribution: {'; '.join(attributions)}",
            f"- Raw evidence files (sha256): {', '.join(r[:16] for r in raws[:20])}{' …' if len(raws) > 20 else ''}",
            "- Method: values as published (current vintage); derived series formulas in series metadata; missing observations shown as gaps.",
            "- Plotted data: see the CSV with the same name (includes observation version ids for reproduction)."]
    for i in infos:
        md = json.loads(i.get("metadata") or "{}")
        if md.get("formula"):
            note.append(f"- Formula for `{i['series_id']}`: {md['formula']}")
        for l in md.get("limitations", []) or []:
            note.append(f"- Limitation: {l}")
    if demo:
        note.insert(1, "\n**SYNTHETIC DEMO DATA — NOT LIVE.**\n")
    Path(f"{stem}.md").write_text("\n".join(note) + "\n", encoding="utf-8")
    return {"png": f"{stem}.png", "svg": f"{stem}.svg", "csv": f"{stem}.csv", "note": f"{stem}.md"}


def export_snapshot_counts(con, aoi_id: str, out_dir: Path, unit_label: str = "candidate vessels present in one radar snapshot (count)") -> dict:
    check_units([unit_label if "snapshot" in unit_label.lower() else unit_label + " snapshot"])
    if "per day" in unit_label.lower() or "transit" in unit_label.lower():
        raise UnitMismatch("snapshot counts cannot be exported as daily transits")
    df = con.execute("SELECT * FROM snapshot_counts WHERE aoi_id=? ORDER BY acquisition_start", [aoi_id]).df()
    if df.empty:
        raise ValueError(f"no snapshot counts for {aoi_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(W, H), dpi=DPI)
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    for k, (grp, g) in enumerate(df.groupby("comparable_group")):
        ax.plot(pd.to_datetime(g["acquisition_start"]), g["candidate_count"], "o", markersize=10,
                color=PALETTE[k % 3], label=f"comparable group {grp}")
    ax.set_ylabel(unit_label, fontsize=13, color=TEXT2)
    ax.legend(frameon=False, fontsize=12)
    fig.suptitle(f"Radar snapshot occupancy — {aoi_id} (EXPERIMENTAL)", x=0.06, ha="left", fontsize=22, fontweight="bold")
    fig.text(0.06, 0.02, "Each point = one Sentinel-1 acquisition (instantaneous snapshot). Not daily transits; "
             "not individual vessel tracks. Contains modified Copernicus Sentinel data.", fontsize=11, color=TEXT2)
    stem = out_dir / _slug(f"snapshot-{aoi_id}")
    fig.savefig(f"{stem}.png", facecolor=SURFACE)
    plt.close(fig)
    df.assign(unit=unit_label).to_csv(f"{stem}.csv", index=False)
    return {"png": f"{stem}.png", "csv": f"{stem}.csv"}


def export_card(con, card_id: str, out_root: Path, demo: bool = False) -> dict:
    row = con.execute("SELECT payload, version FROM evidence_cards WHERE card_id=? AND status='current'", [card_id]).fetchone()
    if not row:
        raise KeyError(f"no current card {card_id}")
    card = json.loads(row[0])
    out = out_root / f"{_slug(card_id)}-v{row[1]}"
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for ch in card.get("charts", [])[:3]:
        sid = ch["series_id"]
        info = series_info(con, sid)
        if not info:
            continue
        ev = ch.get("event_date")
        span = {"daily": 120, "weekly": 730, "monthly": 1825}.get(info["frequency"], 365)
        anchor = pd.Timestamp(ev) if ev else pd.Timestamp(datetime.utcnow())
        start = (anchor - timedelta(days=span)).date().isoformat()
        try:
            files[sid] = export_series(con, [sid], out, info["name"], start=start, event_date=ev, demo=demo)
        except ValueError:
            continue
    md = render_card_markdown(card, row[1], demo)
    (out / "card.md").write_text(md, encoding="utf-8")
    (out / "card.json").write_text(json.dumps(card, indent=2, default=str), encoding="utf-8")
    return {"dir": str(out), "charts": files, "card": str(out / "card.md")}


def render_card_markdown(card: dict, version: int, demo: bool = False) -> str:
    L = []
    if demo:
        L.append("> **SYNTHETIC DEMO DATA — NOT LIVE.**\n")
    L.append(f"# Evidence card `{card['card_id']}` (v{version}, {card['kind']})")
    if card.get("headline"):
        h = card["headline"]
        L.append(f"**Headline:** {h['title']}  \n**Source:** {h.get('publisher')} — {h['url']}  \n"
                 f"**Published:** {h.get('published_at') or 'not stated by source'} · **Discovered:** {h['discovered_at']} ({h.get('discovery_source')})")
    L.append(f"\n**Question tested:** {card['question']}\n")
    L.append(f"**Relationship:** `{card['relationship']}` — {card['calculations'].get('relationship_reason', '')}")
    if card["calculations"].get("temporal_note"):
        L.append(f"\n> {card['calculations']['temporal_note']}")
    L.append("\n## Measurements\n")
    L.append("| Series | Latest (period) | Previous | Change | % | Context window | Screening |")
    L.append("|---|---|---|---|---|---|---|")
    for m in card["calculations"].get("measurements", []):
        d = m.get("digits", 2)
        cw = m.get("context_window") or {}
        an = m.get("anomaly") or {}
        L.append("| {n} [{u}] | {v} ({p}) | {pv} ({pd}) | {c} | {pc} | {cw} | {a} |".format(
            n=m["name"], u=m["unit"], v=_f(m.get("latest_value"), d), p=m.get("latest_period", ""),
            pv=_f(m.get("previous_value"), d), pd=m.get("previous_date", ""), c=_f(m.get("abs_change"), d, True),
            pc=_f(m.get("pct_change"), 1, True), cw=(f"n={cw.get('n')}, median {_f(cw.get('median'), d)} ({cw.get('from')}..{cw.get('to')})" if cw else ""),
            a=(f"{an.get('rule_id')}: {'FIRED' if an.get('fired') else 'not fired'}" if an else "n/a")))
    for m in card["calculations"].get("measurements", []):
        if m.get("anomaly"):
            L.append(f"\n- `{m['series_id']}` — {m['anomaly']['explanation']}")
    if card["calculations"].get("formula"):
        L.append(f"\n**Formula:** {card['calculations']['formula']}")
    if card.get("related_reporting"):
        L.append("\n## Related reporting (context only — not established causes)\n")
        for r in card["related_reporting"]:
            L.append(f"- {r['title']} — {r['publisher']} ({r['time_basis']} {r['time']}) {r['url']}")
    L.append("\n## Contrary / normal observations\n")
    L += [f"- {c}" for c in card.get("contrary_observations", [])] or ["- none recorded"]
    L.append("\n## Alternative explanations\n")
    L += [f"- {a}" for a in card.get("alternative_explanations", [])] or ["- none listed"]
    if card.get("satellite_note"):
        L.append(f"\n**Satellite:** {card['satellite_note']}")
    L.append("\n## Limitations\n")
    L += [f"- {x}" for x in card.get("limitations", [])]
    q = card.get("quality", {})
    L.append(f"\n## Assessment dimensions (separate, no single probability)\n\n- Evidence quality: **{q.get('evidence_quality')}** — {q.get('evidence_quality_reason', '')}"
             f"\n- Freshness: **{q.get('freshness')}**\n- Editorial relevance: **{q.get('editorial_relevance')}** — {q.get('editorial_relevance_reason', '')}"
             f"\n- Signal strength: **{card.get('signal_strength')}**")
    L.append(f"\n## Follow-up question\n\n{card.get('follow_up')}")
    L.append(f"\n## Draft research note ({card.get('narrative_source')})\n\n{card.get('narrative')}")
    if card.get("review_flag"):
        L.append(f"\n> REVIEW NEEDED: {card['review_flag']}")
    L.append(f"\n## Input observation versions\n\n{', '.join(card['input_version_ids'])}")
    return "\n".join(L) + "\n"


def _f(v, d=2, signed=False):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "–"
    return f"{v:+,.{d}f}" if signed else f"{v:,.{d}f}"
