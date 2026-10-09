"""Release-aware polling schedule. All internal times UTC; local release rules use IANA time zones so DST
and holiday exceptions are handled. Polling more often does NOT create newer data — rapid checks only run
inside expected release windows and stop once the expected observation has arrived.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import holidays

UTC = timezone.utc


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------------------------------------------------------------- EIA Weekly Petroleum Status Report
def eia_wpsr_release(week_any_day: date, cfg: dict) -> datetime:
    """Release time (UTC) for the WPSR published in the week containing `week_any_day`.

    Normal: Wednesday 10:30 America/New_York. If a US federal holiday falls on Monday–Wednesday of that
    week, the release moves to Thursday 11:00 ET (EIA's usual holiday pattern). Explicit overrides
    copied from https://www.eia.gov/petroleum/supply/weekly/schedule.php always win.
    """
    tz = ZoneInfo(cfg.get("tz", "America/New_York"))
    monday = week_any_day - timedelta(days=week_any_day.weekday())
    overrides = cfg.get("release_overrides") or {}
    for d_str, when in overrides.items():
        d = date.fromisoformat(d_str[:10])
        if monday <= d < monday + timedelta(days=7):
            dt = datetime.fromisoformat(when)
            return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(UTC)
    us_h = holidays.US(years=[monday.year, (monday + timedelta(days=6)).year])
    shifted = any((monday + timedelta(days=i)) in us_h for i in range(3))
    if shifted:
        local = datetime.combine(monday + timedelta(days=3), time(11, 0), tzinfo=tz)
    else:
        local = datetime.combine(monday + timedelta(days=2), _hm(cfg.get("release_time", "10:30")), tzinfo=tz)
    return local.astimezone(UTC)


def eia_expected_week_ending(release_utc: datetime) -> date:
    """WPSR data refer to the week ending the Friday before the release."""
    d = release_utc.astimezone(ZoneInfo("America/New_York")).date()
    return d - timedelta(days=(d.weekday() - 4) % 7 or 7)


# ---------------------------------------------------------------- generic windows
def _in_window(now: datetime, tz: ZoneInfo, window: list[str], weekdays: list[int]) -> bool:
    local = now.astimezone(tz)
    if local.weekday() not in weekdays:
        return False
    return _hm(window[0]) <= local.time() < _hm(window[1])


def _next_window_start(now: datetime, tz: ZoneInfo, window: list[str], weekdays: list[int]) -> datetime:
    local = now.astimezone(tz)
    for i in range(0, 8):
        d = local.date() + timedelta(days=i)
        if d.weekday() not in weekdays:
            continue
        start = datetime.combine(d, _hm(window[0]), tzinfo=tz)
        if start > local:
            return start.astimezone(UTC)
    return now + timedelta(days=1)


def next_due(source_cfg: dict, now: datetime, last_attempt: datetime | None, latest_observation: date | None = None) -> tuple[datetime, str]:
    """Return (next due time UTC, reason). Due immediately if never attempted."""
    sch = source_cfg.get("schedule", {"kind": "interval", "every_min": 1440})
    kind = sch["kind"]
    if last_attempt is None:
        return now, "never attempted"
    if kind == "interval":
        return last_attempt + timedelta(minutes=sch["every_min"]), f"every {sch['every_min']} min"
    if kind in ("business_afternoon", "us_business_hours", "weekday_window"):
        tz = ZoneInfo(sch["tz"])
        weekdays = sch.get("weekdays", [0, 1, 2, 3, 4])
        if kind == "business_afternoon":
            hol = holidays.financial_holidays("ECB", years=now.year) if hasattr(holidays, "financial_holidays") else {}
            if now.astimezone(tz).date() in hol:
                weekdays = []
        if _in_window(now, tz, sch["window"], weekdays):
            return last_attempt + timedelta(minutes=sch["every_min"]), "inside release window"
        nxt = last_attempt + timedelta(minutes=sch.get("otherwise_every_min", 1440))
        ws = _next_window_start(now, tz, sch["window"], sch.get("weekdays", [0, 1, 2, 3, 4]))
        return (ws, "next release window opens") if ws < nxt else (nxt, "outside release window")
    if kind == "eia_wpsr":
        rel = eia_wpsr_release(now.astimezone(ZoneInfo("America/New_York")).date(), {**sch, "release_overrides": source_cfg.get("release_overrides")})
        expected = eia_expected_week_ending(rel)
        have = latest_observation is not None and latest_observation >= expected
        rapid_end = rel + timedelta(hours=sch.get("rapid_window_hours", 3))
        if rel <= now < rapid_end and not have:
            return last_attempt + timedelta(minutes=sch.get("rapid_every_min", 5)), f"rapid checks after {rel:%Y-%m-%d %H:%M} UTC release (waiting for week ending {expected})"
        if now < rel and not have:
            daily = last_attempt + timedelta(minutes=sch.get("otherwise_every_min", 1440))
            return (min(rel, daily), "scheduled release") if rel < daily else (daily, "daily check")
        nxt_rel = eia_wpsr_release((now + timedelta(days=7)).astimezone(ZoneInfo("America/New_York")).date(),
                                   {**sch, "release_overrides": source_cfg.get("release_overrides")})
        daily = last_attempt + timedelta(minutes=sch.get("otherwise_every_min", 1440))
        return (min(daily, nxt_rel), "expected week received; daily revision check" if have else "release window passed; daily check")
    if kind == "release_dates":
        today = now.date().isoformat()
        if today in (source_cfg.get("release_dates") or []):
            return last_attempt + timedelta(minutes=sch.get("every_min", 60)), "scheduled release date"
        return last_attempt + timedelta(minutes=sch.get("otherwise_every_min", 1440)), "no release today"
    return last_attempt + timedelta(days=1), "default daily"


def next_expected_release(key: str, source_cfg: dict, now: datetime) -> datetime | None:
    sch = source_cfg.get("schedule", {})
    if sch.get("kind") == "eia_wpsr":
        rel = eia_wpsr_release(now.astimezone(ZoneInfo("America/New_York")).date(), {**sch, "release_overrides": source_cfg.get("release_overrides")})
        if rel < now:
            rel = eia_wpsr_release((now + timedelta(days=7)).date(), {**sch, "release_overrides": source_cfg.get("release_overrides")})
        return rel
    if key == "oil_bulletin":
        tz = ZoneInfo("Europe/Brussels")
        local = now.astimezone(tz)
        days = (3 - local.weekday()) % 7
        d = local.date() + timedelta(days=days)
        cand = datetime.combine(d, time(8, 0), tzinfo=tz)
        if cand.astimezone(UTC) < now - timedelta(hours=14):
            cand += timedelta(days=7)
        return cand.astimezone(UTC)
    if key == "ecb_fx":
        tz = ZoneInfo("Europe/Berlin")
        local = now.astimezone(tz)
        d = local.date()
        while True:
            cand = datetime.combine(d, time(16, 0), tzinfo=tz)
            if d.weekday() < 5 and cand > local:
                return cand.astimezone(UTC)
            d += timedelta(days=1)
    if sch.get("kind") == "release_dates":
        future = sorted(x for x in (source_cfg.get("release_dates") or []) if x >= now.date().isoformat())
        return datetime.fromisoformat(future[0]).replace(tzinfo=UTC) if future else None
    return None
