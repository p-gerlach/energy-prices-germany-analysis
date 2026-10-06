"""Build a fully self-contained HTML research page from the local warehouse (`oco build-page`).

No external scripts, fonts or images: charts are drawn by the bundled svgchart.js, so the page renders
offline, inside strict content-security policies, and on any device. All numbers come from stored
observation versions; nothing is computed in the browser except drawing.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..storage.warehouse import observations, series_info
from . import features  # noqa: F401  (registers second-wave sections)
from . import sections

HERE = Path(__file__).parent


def series_points(con, sid: str, start: str | None = None, daily_gaps: bool = False) -> dict | None:
    df = observations(con, sid)
    if df.empty:
        return None
    df = df.dropna(subset=["value"])
    if start:
        df = df[df["obs_end"] >= pd.Timestamp(start)]
    if df.empty:
        return None
    s = df.set_index("obs_end")["value"].sort_index()
    if daily_gaps:
        s = s.asfreq("D")
    return {"x": [d.strftime("%Y-%m-%d") for d in s.index], "y": [None if pd.isna(v) else round(float(v), 5) for v in s.values]}


def rolling7(points: dict, min_days: int = 6) -> dict:
    s = pd.Series(points["y"], index=pd.to_datetime(points["x"]), dtype="float64").asfreq("D")
    r = s.rolling(7, min_periods=min_days).mean()
    return {"x": [d.strftime("%Y-%m-%d") for d in r.index], "y": [None if pd.isna(v) else round(float(v), 3) for v in r.values]}


def band_from(points: dict, start: str, end: str, q=(0.05, 0.95)) -> dict | None:
    s = pd.Series(points["y"], index=pd.to_datetime(points["x"]), dtype="float64").dropna()
    b = s[(s.index >= start) & (s.index <= end)]
    if len(b) < 20:
        return None
    return {"x0": start, "x1": end, "y0": float(b.quantile(q[0])), "y1": float(b.quantile(q[1])),
            "median": float(b.median()), "label": f"{start[:4]} normal range (5th–95th pct)"}


def build_page(con, state, out_path: Path, demo: bool = False) -> Path:
    data = sections.collect(con, state)
    data["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data["demo"] = demo
    js = (HERE / "svgchart.js").read_text(encoding="utf-8") + "\n" + (HERE / "dashboard.js").read_text(encoding="utf-8")
    tpl = (HERE / "page_template.html").read_text(encoding="utf-8")
    img_path = data.pop("_s1_image", None)
    img = base64.b64encode(Path(img_path).read_bytes()).decode() if img_path and Path(img_path).exists() else ""
    payload = json.dumps(data, default=_json_default).replace("</", "<\\/")
    html = tpl.replace("/*__SVGCHART__*/", js).replace("__DATA__", payload).replace("__IMG__", img)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return str(o)
    return str(o)
