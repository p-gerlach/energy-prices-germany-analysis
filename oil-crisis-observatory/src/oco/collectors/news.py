"""News discovery: GDELT DOC API, individually admitted RSS feeds, and manual headline entry.

News text is UNTRUSTED DATA. It is stored and displayed as data, never interpreted as instructions,
never executed, and never used to change configuration. Only title, link, publisher, language,
provider timestamps and a short feed-provided excerpt are stored (no paywall scraping, no full text).

Timestamps:
  published_at          only when the publisher/feed states it (published_at_source records where from)
  discovered_at         when THIS tool first saw the item (or GDELT 'seendate' = GDELT's discovery time,
                        recorded as discovery, never as publication)
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from ..http import ContentValidationError
from ..storage.state import utcnow
from ..storage.warehouse import Warehouse
from .base import Collector, RunResult, SchemaChanged

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ocid|cmpid|at_|xtor|wt_|ref$|sara_)", re.I)


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") or host.startswith("amp."):
        host = host.split(".", 1)[1]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/amp/?$", "", p.path).rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(q)), ""))


def clean_text(s: str | None, limit: int = 300) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    s = " ".join(s.split())
    return s[:limit]


_STOP = set("the a an of to in on for and or at by with from is are was were be as its it this that über und der die das ein eine im in von zu mit auf für bei nach".split())


def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[\wäöüß]+", title.lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def upsert_headline(wh: Warehouse, *, url: str, title: str, publisher: str, language: str | None,
                    published_at: datetime | None, published_at_source: str | None, discovered_at: datetime,
                    discovery_source: str, excerpt: str = "", raw_sha256: str | None = None, origin: str) -> tuple[str, bool]:
    """Insert or update metadata; dedupe on canonical URL; assign cluster. Returns (headline_id, is_new)."""
    canon = canonical_url(url)
    hid = hashlib.sha1(canon.encode()).hexdigest()[:16]
    title = clean_text(title, 400)
    excerpt = clean_text(excerpt, 300)
    chash = hashlib.sha1(f"{title}|{excerpt}".encode()).hexdigest()[:16]
    existing = wh.con.execute("SELECT content_hash, published_at, discovered_at FROM headlines WHERE headline_id=?", [hid]).fetchone()
    if existing:
        if existing[0] != chash or (published_at and existing[1] is None):
            # updated metadata: keep earliest discovery, fill publication if newly provided
            # metadata updates only ADD information: an empty title/excerpt never erases a stored one
            wh.con.execute(
                "UPDATE headlines SET title=COALESCE(NULLIF(?, ''), title), excerpt=COALESCE(NULLIF(?, ''), excerpt), "
                "content_hash=?, published_at=COALESCE(published_at, ?), "
                "published_at_source=COALESCE(published_at_source, ?), updated_at=? WHERE headline_id=?",
                [title, excerpt, chash, published_at, published_at_source, utcnow(), hid])
        return hid, False
    cluster = _assign_cluster(wh, hid, title, discovered_at)
    wh.con.execute(
        "INSERT INTO headlines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [hid, canon, url, title, publisher, language, published_at, published_at_source, discovered_at,
         discovery_source, excerpt, chash, cluster, raw_sha256, origin, utcnow()])
    return hid, True


def _assign_cluster(wh: Warehouse, hid: str, title: str, at: datetime, window_h: int = 72, threshold: float = 0.6) -> str:
    toks = title_tokens(title)
    rows = wh.con.execute(
        "SELECT h.cluster_id, h.title FROM headlines h WHERE h.discovered_at >= ? ", [at - timedelta(hours=window_h)]
    ).fetchall()
    for cid, t in rows:
        if jaccard(toks, title_tokens(t)) >= threshold:
            wh.con.execute("UPDATE headline_clusters SET n_members=n_members+1 WHERE cluster_id=?", [cid])
            return cid
    cid = "c" + hid
    wh.con.execute("INSERT INTO headline_clusters VALUES (?,?,?,?,?)", [cid, hid, title, at, 1])
    return cid


# --------------------------------------------------------------------------------------------
class GDELTCollector(Collector):
    key = "gdelt"
    connector = "gdelt"
    API = "https://api.gdeltproject.org/api/v2/doc/doc"
    _last_request = 0.0

    def _throttle(self):
        gap = float(self.cfg.get("min_seconds_between_requests", 6))
        wait = GDELTCollector._last_request + gap - time.monotonic()
        if wait > 0:
            self.ctx.sleep(wait)
        GDELTCollector._last_request = time.monotonic()

    def fetch_window(self, client, query: str, start: datetime, end: datetime, result: RunResult,
                     depth: int = 0) -> list[tuple[dict, bytes]]:
        self._throttle()
        r = client.get(self.API, params={
            "query": query, "mode": "artlist", "format": "json", "sort": "datedesc",
            "maxrecords": int(self.cfg.get("max_records", 250)),
            "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": end.strftime("%Y%m%d%H%M%S")})
        result.n_requests += 1
        text = r.text().strip()
        if not text.startswith("{"):
            low = text.lower()
            if "limit requests" in low or "one every" in low:
                raise ContentValidationError("GDELT rate-limit notice returned with HTTP 200; backing off")
            if not text:
                return []
            raise SchemaChanged(f"GDELT returned non-JSON: {text[:120]!r}")
        try:
            payload = r.json()
        except ValueError as e:
            raise SchemaChanged(f"GDELT JSON invalid: {e}") from e
        arts = payload.get("articles", []) or []
        maxr = int(self.cfg.get("max_records", 250))
        min_win = timedelta(minutes=int(self.cfg.get("min_window_minutes", 30)))
        if len(arts) >= maxr and (end - start) > min_win and depth < 8:
            mid = start + (end - start) / 2
            return (self.fetch_window(client, query, start, mid, result, depth + 1)
                    + self.fetch_window(client, query, mid, end, result, depth + 1))
        if len(arts) >= maxr:
            self.ctx.state.log(self.key, "WARN", f"GDELT window {start}..{end} still at {maxr} results: coverage may be truncated")
        return [(a, r.content) for a in arts]

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        end = utcnow().replace(second=0, microsecond=0)
        hours = int(kw.get("hours") or (24 * 7 if mode == "backfill" else self.cfg.get("lookback_hours", 24)))
        start = end - timedelta(hours=hours)
        with self.ctx.client(self.connector) as client:
            for q in self.cfg["queries"]:
                items = self.fetch_window(client, q, start, end, result)
                stored = {}
                for art, body in items:
                    bh = hashlib.sha256(body).hexdigest()
                    if bh not in stored:
                        stored[bh] = wh.store_raw("gdelt", body, url=f"{self.API}?query={q}", content_type="application/json", ext="json")
                    seen = _parse_gdelt_date(art.get("seendate"))
                    _, new = upsert_headline(
                        wh, url=art["url"], title=art.get("title", ""), publisher=art.get("domain", ""),
                        language=art.get("language"), published_at=None, published_at_source=None,
                        discovered_at=seen or utcnow(), discovery_source=f"gdelt:{q}", raw_sha256=stored[bh], origin="gdelt")
                    result.counts["new" if new else "unchanged"] += 1
                result.raw.extend(stored.values())

    def probe(self):
        res = RunResult(self.key)
        end = utcnow().replace(second=0, microsecond=0)
        with self.ctx.client(self.connector) as client:
            items = self.fetch_window(client, '"strait of hormuz"', end - timedelta(hours=24), end, res)
        return True, f"{len(items)} articles in last 24h for '\"strait of hormuz\"'"


def _parse_gdelt_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------
class RSSCollector(Collector):
    key = "rss"
    connector = "rss"

    def parse(self, content: bytes):
        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            raise SchemaChanged(f"feed not parseable: {str(feed.bozo_exception)[:120]}")
        return feed

    def verify_feeds(self) -> list[dict]:
        """One bounded fetch per candidate feed. Admit only anonymous, well-formed RSS/Atom with items."""
        out = []
        for f in self.cfg["feeds"]:
            try:
                with self.ctx.client(self.connector) as client:
                    r = client.get(f["url"], max_bytes=5_000_000)
                feed = self.parse(r.content)
                ok = len(feed.entries) > 0
                detail = f"{len(feed.entries)} items; title={clean_text(feed.feed.get('title'), 80)!r}"
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
            self.ctx.state.set_feed_admission(f["url"], ok, detail)
            out.append({"url": f["url"], "admitted": ok, "detail": detail})
        return out

    def collect(self, wh: Warehouse, result: RunResult, mode: str = "refresh", **kw):
        admitted = self.ctx.state.admitted_feeds()
        feeds = [f for f in self.cfg["feeds"] if admitted.get(f["url"], {}).get("admitted")]
        if not feeds:
            result.status = "unconfigured"
            result.message = "no RSS feed admitted yet — run `oco news verify-feeds`"
            return
        errors = []
        for f in feeds:
            try:
                with self.ctx.client(self.connector) as client:
                    r = client.get(f["url"], conditional=True, max_bytes=5_000_000)
                result.n_requests += 1
                if r.not_modified:
                    continue
                sha = wh.store_raw("rss", r.content, url=r.url, content_type=r.content_type, ext="xml",
                                   etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"))
                result.raw.append(sha)
                feed = self.parse(r.content)
                now = utcnow()
                for e in feed.entries:
                    link = e.get("link")
                    if not link:
                        continue
                    pub, src = None, None
                    for k in ("published_parsed", "updated_parsed"):
                        if e.get(k):
                            pub = datetime(*e[k][:6], tzinfo=timezone.utc)
                            src = f"feed:{k.split('_')[0]}"
                            break
                    _, new = upsert_headline(
                        wh, url=link, title=e.get("title", ""), publisher=f["publisher"], language=f.get("language"),
                        published_at=pub, published_at_source=src, discovered_at=now, discovery_source=f"rss:{f['url']}",
                        excerpt=e.get("summary", "")[: int(self.cfg.get("max_excerpt_chars", 300)) * 3],
                        raw_sha256=sha, origin="rss")
                    result.counts["new" if new else "unchanged"] += 1
            except Exception as e:  # noqa: BLE001 — one bad feed must not stop the others
                errors.append(f"{f['publisher']}: {type(e).__name__}: {str(e)[:120]}")
        if errors and result.n_requests == 0:
            raise ContentValidationError("; ".join(errors))
        if errors:
            result.message = "partial: " + "; ".join(errors)

    def probe(self):
        res = self.verify_feeds()
        ok = [r for r in res if r["admitted"]]
        return bool(ok), f"{len(ok)}/{len(res)} feeds admitted: " + "; ".join(f"{r['url']}: {r['detail'][:60]}" for r in res)


def add_manual_headline(wh: Warehouse, url: str, title: str, published_at: datetime | None = None,
                        publisher: str | None = None, language: str | None = None) -> str:
    """User-pasted headline (works when discovery APIs are unavailable). Nothing is fetched."""
    hid, _ = upsert_headline(
        wh, url=url, title=title, publisher=publisher or (urlsplit(url).hostname or "manual"), language=language,
        published_at=published_at, published_at_source="user-entered" if published_at else None,
        discovered_at=utcnow(), discovery_source="manual", origin="manual")
    return hid
