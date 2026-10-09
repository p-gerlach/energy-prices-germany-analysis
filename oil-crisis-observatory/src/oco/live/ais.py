"""Live AIS vessel positions from aisstream.io (free key the user creates themselves).

Runs only while `oco ships` runs, on the user's computer: aisstream does not allow browser connections, and the
key must never be exposed to a web page. Message text (ship names, destinations) is untrusted data: it is
cleaned, length-capped and only ever rendered as text.

Interpretation limits shown in the viewer:
* terrestrial receivers only: ships far offshore or near coasts without a receiver are missing
* ships that switch AIS off (common in conflict areas) are invisible; AIS identities can be spoofed
* the map shows the last reported position; movement between reports is not extrapolated
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

URL = "wss://stream.aisstream.io/v0/stream"
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport", "ShipStaticData", "StaticDataReport"]

NAV_STATUS = {0: "under way using engine", 1: "at anchor", 2: "not under command", 3: "restricted manoeuvrability",
              4: "constrained by draught", 5: "moored", 6: "aground", 7: "engaged in fishing", 8: "under way sailing",
              14: "AIS-SART active", 15: "not defined"}

# Maritime Identification Digits (first 3 digits of the MMSI) -> flag state, for the most common registries
MID = {**{m: "Panama" for m in (351, 352, 353, 354, 355, 356, 357, 370, 371, 372, 373, 374)},
       **{m: "Liberia" for m in (636, 637)}, 538: "Marshall Islands", **{m: "Malta" for m in (215, 229, 248, 249, 256)},
       **{m: "Bahamas" for m in (308, 309, 311)}, **{m: "Greece" for m in (237, 239, 240, 241)},
       **{m: "Singapore" for m in (563, 564, 565, 566)}, 477: "Hong Kong", **{m: "China" for m in (412, 413, 414)},
       **{m: "Cyprus" for m in (209, 210, 212)}, **{m: "Norway" for m in (257, 258, 259)},
       **{m: "United Kingdom" for m in (232, 233, 234, 235)}, **{m: "Germany" for m in (211, 218)},
       **{m: "Netherlands" for m in (244, 245, 246)}, **{m: "Denmark" for m in (219, 220)}, 247: "Italy",
       **{m: "France" for m in (226, 227, 228)}, **{m: "Spain" for m in (224, 225)}, 271: "Türkiye", 403: "Saudi Arabia",
       **{m: "United Arab Emirates" for m in (470, 471)}, 422: "Iran", 425: "Iraq", 447: "Kuwait", 466: "Qatar",
       461: "Oman", 408: "Bahrain", 419: "India", **{m: "Japan" for m in (431, 432)}, **{m: "South Korea" for m in (440, 441)},
       **{m: "United States" for m in (303, 338, 366, 367, 368, 369)}, 273: "Russia", 622: "Egypt",
       **{m: "Antigua and Barbuda" for m in (304, 305)}, 255: "Portugal (Madeira)", 205: "Belgium",
       **{m: "Sweden" for m in (265, 266)}, 230: "Finland", 525: "Indonesia", 533: "Malaysia", 574: "Vietnam",
       548: "Philippines", 616: "Comoros", 671: "Togo", 613: "Cameroon", 626: "Gabon", 667: "Sierra Leone",
       **{m: "Tanzania" for m in (674, 677)}, 511: "Palau", 572: "Tuvalu", 518: "Cook Islands", 341: "St Kitts and Nevis",
       **{m: "St Vincent and the Grenadines" for m in (375, 376, 377)}, 312: "Belize", 314: "Barbados", 339: "Jamaica",
       334: "Honduras", 457: "Mongolia", 417: "Sri Lanka", 463: "Pakistan", **{m: "Yemen" for m in (473, 475)},
       621: "Djibouti", 625: "Eritrea", 662: "Sudan", 438: "Jordan", 428: "Israel", 450: "Lebanon", 468: "Syria",
       642: "Libya", 605: "Algeria", 242: "Morocco", 672: "Tunisia", 657: "Nigeria", 710: "Brazil", 316: "Canada",
       345: "Mexico", 503: "Australia", 512: "New Zealand", 261: "Poland", 263: "Portugal", 250: "Ireland", 276: "Estonia",
       275: "Latvia", 277: "Lithuania", 238: "Croatia", 207: "Bulgaria", 264: "Romania", 272: "Ukraine", 620: "Comoros"}


def ship_class(type_code: int | None) -> str:
    """AIS ship-type code -> broad class used for colours and filters."""
    if type_code is None:
        return "unknown"
    t = int(type_code)
    if 80 <= t <= 89:
        return "tanker"
    if 70 <= t <= 79:
        return "cargo"
    if 60 <= t <= 69:
        return "passenger"
    if t == 30:
        return "fishing"
    if t in (31, 32, 33, 34, 50, 51, 52, 53, 54, 55, 58, 59):
        return "service"
    if t in (35,):
        return "military"
    if t in (36, 37):
        return "leisure"
    if 40 <= t <= 49:
        return "high_speed"
    return "other"


def type_text(t: int | None) -> str:
    if t is None:
        return "not reported yet"
    base = {range(20, 30): "wing in ground", range(40, 50): "high-speed craft", range(60, 70): "passenger",
            range(70, 80): "cargo", range(80, 90): "tanker", range(90, 100): "other type"}
    special = {30: "fishing", 31: "towing", 32: "towing (large)", 33: "dredging/underwater ops", 34: "diving ops",
               35: "military ops", 36: "sailing", 37: "pleasure craft", 50: "pilot vessel", 51: "search and rescue",
               52: "tug", 53: "port tender", 54: "anti-pollution", 55: "law enforcement", 58: "medical transport"}
    if t in special:
        return special[t]
    for r, name in base.items():
        if t in r:
            hazard = {1: ", hazard cat. A", 2: ", hazard cat. B", 3: ", hazard cat. C", 4: ", hazard cat. D"}.get(t % 10, "")
            return name + hazard
    return f"code {t}"


_CTRL = re.compile(r"[\x00-\x1f\x7f<>]")


def clean(s, n=40) -> str | None:
    if s is None:
        return None
    s = _CTRL.sub("", str(s)).replace("@", "").strip()
    return s[:n] or None


def _num(v, bad=None, lo=None, hi=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if bad is not None and f == bad:
        return None
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        return None
    return f


class VesselStore:
    """Thread-safe in-memory table of vessels seen in this session (never written to the warehouse)."""

    def __init__(self, track_len: int = 60, stale_after_s: int = 3600):
        self.lock = threading.Lock()
        self.v: dict[int, dict] = {}
        self.track_len = track_len
        self.stale_after_s = stale_after_s
        self.n_messages = 0
        self.last_message_at: float | None = None

    def _get(self, mmsi: int) -> dict:
        x = self.v.get(mmsi)
        if x is None:
            mid = int(str(mmsi)[:3]) if len(str(mmsi)) == 9 else None
            x = self.v[mmsi] = {"mmsi": mmsi, "flag": MID.get(mid), "track": deque(maxlen=self.track_len)}
        return x

    def ingest(self, msg: dict, now: float | None = None) -> bool:
        now = now or time.time()
        kind = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = (msg.get("Message") or {}).get(kind) or {}
        try:
            mmsi = int(meta.get("MMSI") or body.get("UserID"))
        except (TypeError, ValueError):
            return False
        if not (100_000_000 <= mmsi <= 999_999_999):
            return False
        with self.lock:
            self.n_messages += 1
            self.last_message_at = now
            x = self._get(mmsi)
            if meta.get("ShipName") and not x.get("name"):
                x["name"] = clean(meta["ShipName"])
            if kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
                lat = _num(body.get("Latitude"), 91, -90, 90)
                lon = _num(body.get("Longitude"), 181, -180, 180)
                if lat is None or lon is None:
                    return False
                x.update(lat=round(lat, 5), lon=round(lon, 5), sog=_num(body.get("Sog"), 102.3, 0, 102.2),
                         cog=_num(body.get("Cog"), 360, 0, 359.9), hdg=_num(body.get("TrueHeading"), 511, 0, 359),
                         t=now, cls_b=kind != "PositionReport")
                if kind == "PositionReport":
                    ns = body.get("NavigationalStatus")
                    x["nav"] = NAV_STATUS.get(ns) if ns is not None else None
                if kind == "ExtendedClassBPositionReport" and body.get("Type") is not None:
                    x["type"] = int(body["Type"])
                tr = x["track"]
                if not tr or (tr[-1][1], tr[-1][2]) != (x["lat"], x["lon"]):
                    tr.append((int(now), x["lat"], x["lon"]))
                return True
            if kind == "ShipStaticData":
                x["name"] = clean(body.get("Name")) or x.get("name")
                x["callsign"] = clean(body.get("CallSign"), 10)
                imo = body.get("ImoNumber")
                x["imo"] = int(imo) if isinstance(imo, (int, float)) and 1_000_000 <= imo <= 9_999_999 else x.get("imo")
                if body.get("Type") is not None:
                    x["type"] = int(body["Type"])
                x["dest"] = clean(body.get("Destination"))
                dr = _num(body.get("MaximumStaticDraught"), 0, 0, 30)
                x["draught"] = dr
                d = body.get("Dimension") or {}
                L, B = (_num(d.get("A"), 0) or 0) + (_num(d.get("B"), 0) or 0), (_num(d.get("C"), 0) or 0) + (_num(d.get("D"), 0) or 0)
                x["length"], x["beam"] = (L or None), (B or None)
                eta = body.get("Eta") or {}
                if eta.get("Month") and eta.get("Day"):
                    x["eta"] = f"{int(eta['Month']):02d}-{int(eta['Day']):02d} {int(eta.get('Hour') or 0):02d}:{int(eta.get('Minute') or 0):02d} UTC"
                x["static_t"] = now
                return True
            if kind == "StaticDataReport":
                a, b = body.get("ReportA") or {}, body.get("ReportB") or {}
                if a.get("Valid") and a.get("Name"):
                    x["name"] = clean(a["Name"])
                if b.get("Valid"):
                    if b.get("ShipType") is not None:
                        x["type"] = int(b["ShipType"])
                    x["callsign"] = clean(b.get("CallSign"), 10) or x.get("callsign")
                x["static_t"] = now
                return True
        return False

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        with self.lock:
            for mmsi in [m for m, x in self.v.items() if now - x.get("t", x.get("static_t", now)) > self.stale_after_s]:
                del self.v[mmsi]
            for x in self.v.values():
                if "lat" not in x:
                    continue
                t = x.get("type")
                out.append({"m": x["mmsi"], "n": x.get("name"), "la": x["lat"], "lo": x["lon"], "s": x.get("sog"), "c": x.get("cog"),
                            "h": x.get("hdg"), "k": ship_class(t), "ty": t, "tt": type_text(t), "a": round(now - x["t"]),
                            "nav": x.get("nav"), "f": x.get("flag"), "imo": x.get("imo"), "cs": x.get("callsign"), "d": x.get("dest"),
                            "eta": x.get("eta"), "dr": x.get("draught"), "L": x.get("length"), "B": x.get("beam"),
                            "tr": [[p[1], p[2]] for p in x["track"]]})
        return out


class StreamStatus:
    def __init__(self):
        self.state = "starting"
        self.detail = ""
        self.connected_since: str | None = None

    def set(self, state: str, detail: str = ""):
        self.state, self.detail = state, detail[:300]
        if state == "connected":
            self.connected_since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class FatalStreamError(Exception):
    """The provider refused us (bad key, terms, billing wording): stop instead of retrying."""


def subscription(key: str, boxes: list) -> str:
    return json.dumps({"APIKey": key, "BoundingBoxes": boxes, "FilterMessageTypes": MESSAGE_TYPES})


def check_provider_message(msg: dict, stop_markers: list[str]):
    err = msg.get("error") or msg.get("Error")
    if err:
        raise FatalStreamError(f"aisstream refused the subscription: {clean(err, 200)}")
    low = json.dumps(msg).lower()[:2000]
    if msg.get("MessageType") is None and any(m in low for m in stop_markers):
        raise FatalStreamError("provider message mentions billing/plan terms; connector stopped for review")


async def run_stream(policy, key: str, boxes: list, store: VesselStore, status: StreamStatus, stop: threading.Event,
                     connect=None, min_backoff: float = 10, max_backoff: float = 300):
    """Keep one subscription open with exponential backoff; stop for good on a provider refusal."""
    policy.check("aisstream", "GET", URL)  # default-deny guard: only the approved websocket route
    if connect is None:
        from websockets.asyncio.client import connect as ws_connect
        # proxy=True honours HTTPS_PROXY when a network requires one; at home it connects directly
        connect = lambda: ws_connect(URL, max_size=2 ** 20, open_timeout=20, ping_interval=20, ping_timeout=20, proxy=True)  # noqa: E731
    backoff = min_backoff
    while not stop.is_set():
        try:
            status.set("connecting")
            async with connect() as ws:
                await ws.send(subscription(key, boxes))
                status.set("connected")
                backoff = min_backoff
                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    check_provider_message(msg, policy.stop_markers)
                    store.ingest(msg)
        except FatalStreamError as e:
            status.set("stopped", str(e))
            return
        except Exception as e:  # noqa: BLE001 — network trouble: wait and retry, never hammer
            status.set("reconnecting", f"{type(e).__name__}: {str(e)[:150]} — retry in {int(backoff)} s")
        if stop.is_set():
            break
        for _ in range(int(backoff)):
            if stop.is_set():
                break
            await asyncio.sleep(1)
        backoff = min(max_backoff, backoff * 2)
    status.set("stopped", status.detail or "stopped by user")
