"""Guarded HTTP client: the only way the application talks to the network.

Order of checks for every hop (initial request and every redirect):
  1. connector not stopped / paused / circuit-open (persistent state)
  2. URL matches an approved route of the calling connector (config/access_policy.yaml)  -> else PolicyDenied
  3. credentials only on routes marked sends_credentials; stripped on any other hop
Response handling:
  * 402, billing/trial/credit wording, unexpected auth demand  -> ConnectorStopped (persisted; human reset needed)
  * 429                                                         -> backoff (Retry-After honoured) then pause; never upgrade
  * 5xx / timeout / connection error                            -> bounded exponential backoff with jitter, circuit breaker
  * HTML or wrong content type where data expected              -> ContentValidationError (nothing is stored)
  * size limits enforced while streaming
All URLs written to logs, manifests or exceptions are redacted.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .policy import Policy, PolicyDenied, load_policy
from .settings import contact
from .storage.state import StateStore, utcnow

SENSITIVE_PARAMS = {"api_key", "map_key", "key", "password", "token", "access_token", "refresh_token", "client_secret"}
REDIRECT_CODES = {301, 302, 303, 307, 308}


class ConnectorStopped(Exception):
    """Connector disabled (fail closed). Requires explicit human review to reset."""


class RateLimited(Exception):
    """Provider rate limit reached; connector paused. The only response is to wait."""


class ContentValidationError(Exception):
    """Response did not look like the declared data format (e.g. an HTML error page with HTTP 200)."""


class NetworkUnavailable(Exception):
    """Host unreachable from this machine (DNS, proxy/egress policy, offline). Not a provider verdict."""


class CircuitOpen(Exception):
    """Too many consecutive failures; host cool-down in effect."""


class AuthRejected(Exception):
    """An approved credential was rejected (401). Caller may refresh an approved token once."""


class TransientHTTPError(Exception):
    pass


def redact(url: str, secrets: Iterable[str] = ()) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    q = [(k, "<redacted>" if k.lower() in SENSITIVE_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    out = urlunsplit((parts.scheme, netloc, parts.path, urlencode(q, safe="[]/:,'() "), ""))
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "<redacted>")
    out = re.sub(r"/[A-Za-z0-9]{28,}(?=/|$)", "/<redacted>", out)
    return out


@dataclass
class FetchResult:
    connector: str
    url: str  # redacted
    final_url: str  # redacted
    status: int
    content_type: str
    headers: dict
    retrieved_at: datetime
    content: bytes = b""
    path: Path | None = None
    sha256: str | None = None
    size: int = 0
    not_modified: bool = False
    redirects: list[str] = field(default_factory=list)

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.content)


def _ctype(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "").split(";")[0].strip().lower()


class GuardedClient:
    def __init__(
        self,
        connector: str,
        state: StateStore,
        policy: Policy | None = None,
        secrets: Iterable[str] = (),
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
        timeout: float = 45.0,
        max_backoff: float = 300.0,
    ):
        self.connector = connector
        self.policy = policy or load_policy()
        self.conn_policy = self.policy.connector(connector)  # raises if undeclared
        self.state = state
        self.secrets = [s for s in secrets if s]
        self.sleep = sleep
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        ua = self.policy.raw["global"]["user_agent"].format(contact=contact())
        # trust_env=True: honour the machine's proxy and CA settings (needed behind corporate proxies).
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=20.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            trust_env=transport is None,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------------------------
    def _check_state(self):
        st = self.state.connector_status(self.connector)
        now = utcnow()
        if st["status"] == "stopped":
            raise ConnectorStopped(f"{self.connector} is stopped: {st['reason']} (run `oco connectors reset {self.connector}` after review)")
        if st["status"] == "paused" and st["paused_until"] and datetime.fromisoformat(st["paused_until"]) > now:
            raise RateLimited(f"{self.connector} paused until {st['paused_until']}: {st['reason']}")
        if st["circuit_open_until"] and datetime.fromisoformat(st["circuit_open_until"]) > now:
            raise CircuitOpen(f"{self.connector} circuit open until {st['circuit_open_until']}")

    def _r(self, url: str) -> str:
        return redact(url, self.secrets)

    def _stop(self, reason: str):
        self.state.stop_connector(self.connector, reason)
        raise ConnectorStopped(f"{self.connector}: {reason}")

    def _scan_markers(self, body: bytes) -> str | None:
        text = body[:200_000].decode("utf-8", errors="ignore").lower()
        for m in self.policy.stop_markers:
            if m in text:
                return m
        return None

    def _scan_throttle(self, body: bytes) -> str | None:
        text = body[:50_000].decode("utf-8", errors="ignore").lower()
        for m in self.policy.raw["global"].get("throttle_markers", []):
            if m.lower() in text:
                return m
        return None

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), self.max_backoff)
            except ValueError:
                try:
                    dt = parsedate_to_datetime(ra)
                    return max(0.0, min((dt - datetime.now(timezone.utc)).total_seconds(), self.max_backoff))
                except (TypeError, ValueError):
                    pass
        # no Retry-After: wait at least 10 s so a strict provider (e.g. GDELT: 1 request / 5 s) can recover
        return max(10.0, self._backoff(attempt))

    def _backoff(self, attempt: int) -> float:
        return min(self.max_backoff, (2 ** attempt) * 2.0) * (0.5 + random.random() / 2)

    # ------------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        params: dict | list | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        auth_header: str | None = None,
        conditional: bool = False,
        stream_to: Path | None = None,
        max_bytes: int | None = None,
        expect_content: tuple[str, ...] | None = None,
    ) -> FetchResult:
        self._check_state()
        full_url = str(httpx.URL(url, params=params)) if params else url
        method = method.upper()
        route = self.policy.check(self.connector, method, full_url)  # PolicyDenied before any I/O
        if auth_header and not route.sends_credentials:
            raise PolicyDenied(f"credentials may not be sent on route {route.host} for {self.connector}")
        limit = max_bytes or route.max_response_bytes or self.policy.max_response_bytes
        hdrs = dict(headers or {})
        url_key = self._r(full_url)
        if conditional and method == "GET":
            v = self.state.get_validators(url_key)
            if v.get("etag"):
                hdrs["If-None-Match"] = v["etag"]
            if v.get("last_modified"):
                hdrs["If-Modified-Since"] = v["last_modified"]

        attempt = 0
        while True:
            try:
                result = self._send_following_redirects(method, full_url, route, hdrs, auth_header, data, json_body,
                                                       stream_to, limit, expect_content)
            except (httpx.ProxyError, httpx.ConnectError) as e:
                # proxy CONNECT denial or DNS failure: environment problem, not a provider decision
                msg = str(e)
                if isinstance(e, httpx.ProxyError) or "403" in msg or "Name or service not known" in msg:
                    self.state.log(self.connector, "WARN", f"network unavailable for {url_key}: {msg[:200]}")
                    raise NetworkUnavailable(f"{self.connector}: host unreachable from this machine ({msg[:160]})") from e
                err: Exception = e
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError, TransientHTTPError) as e:
                err = e
            except _RateLimitSignal as rl:
                if attempt >= self.max_retries:
                    until = utcnow() + timedelta(seconds=max(rl.wait, 3600))
                    self.state.pause_connector(self.connector, until, "HTTP 429 rate limit persisted; waiting (no upgrade)")
                    raise RateLimited(f"{self.connector}: rate limited; paused until {until.isoformat()}") from None
                self.state.log(self.connector, "INFO", f"429 from {url_key}; backing off {rl.wait:.0f}s")
                self.sleep(rl.wait)
                attempt += 1
                continue
            else:
                self.state.record_success(self.connector)
                if conditional and not result.not_modified:
                    self.state.set_validators(url_key, result.headers.get("etag"), result.headers.get("last-modified"), result.sha256)
                return result
            # transient error path
            if attempt >= self.max_retries:
                opened = self.state.record_failure(self.connector)
                self.state.log(self.connector, "ERROR", f"giving up on {url_key}: {type(err).__name__}: {str(err)[:200]}"
                               + (" (circuit opened)" if opened else ""))
                raise TransientHTTPError(f"{self.connector}: {type(err).__name__}: {str(err)[:200]}") from err
            self.sleep(self._backoff(attempt))
            attempt += 1

    def get(self, url: str, **kw) -> FetchResult:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> FetchResult:
        return self.request("POST", url, **kw)

    # ------------------------------------------------------------------------------------
    def _send_following_redirects(self, method, url, route, hdrs, auth_header, data, json_body, stream_to, limit, expect_content):
        redirects: list[str] = []
        current_url, current_route, current_method = url, route, method
        for _hop in range(6):
            h = dict(hdrs)
            if auth_header and current_route.sends_credentials:
                h["Authorization"] = auth_header
            req = self._client.build_request(current_method, current_url, headers=h,
                                             data=data if current_method == "POST" else None,
                                             json=json_body if current_method == "POST" else None)
            resp = self._client.send(req, stream=True)
            try:
                if resp.status_code in REDIRECT_CODES:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise ContentValidationError("redirect without Location header")
                    nxt = urljoin(current_url, loc)
                    if resp.status_code == 303:
                        current_method = "GET"
                    try:
                        current_route = self.policy.check(self.connector, current_method, nxt)
                    except PolicyDenied as e:
                        self.state.log(self.connector, "ERROR", f"blocked redirect to {self._r(nxt)}")
                        raise PolicyDenied(f"redirect to unapproved destination blocked: {e}") from None
                    redirects.append(self._r(nxt))
                    current_url = nxt
                    continue
                return self._handle_final(resp, current_url, current_route, url, redirects, stream_to, limit, expect_content, auth_header)
            finally:
                resp.close()
        raise ContentValidationError("too many redirects")

    def _read_limited(self, resp: httpx.Response, limit: int, stream_to: Path | None):
        h = hashlib.sha256()
        size = 0
        if stream_to is not None:
            stream_to.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=stream_to.parent, prefix=".part-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > limit:
                            raise ContentValidationError(f"response exceeds size limit ({limit} bytes)")
                        h.update(chunk)
                        fh.write(chunk)
                os.replace(tmp, stream_to)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return b"", size, h.hexdigest()
        buf = bytearray()
        for chunk in resp.iter_bytes(1 << 16):
            size += len(chunk)
            if size > limit:
                raise ContentValidationError(f"response exceeds size limit ({limit} bytes)")
            buf.extend(chunk)
        h.update(buf)
        return bytes(buf), size, h.hexdigest()

    def _handle_final(self, resp, final_url, route, orig_url, redirects, stream_to, limit, expect_content, auth_header):
        status = resp.status_code
        ctype = _ctype(resp)
        if status in self.policy.stop_statuses:
            self._stop(f"HTTP {status} (payment/entitlement) from {route.host} — connector disabled, nothing purchased")
        if status == 429:
            raise _RateLimitSignal(self._retry_after(resp, 0))
        if status in (401, 403):
            body, _, _ = self._read_limited(resp, 1_000_000, None)
            marker = self._scan_markers(body)
            if marker:
                self._stop(f"HTTP {status} with billing/entitlement wording ({marker!r})")
            throttle = self._scan_throttle(body) if status == 403 else None
            if throttle:
                # provider firewall rate/abuse block (not a login or payment demand): pause, never hammer, never upgrade
                until = utcnow() + timedelta(minutes=30)
                self.state.pause_connector(self.connector, until, f"HTTP 403 firewall throttle ({throttle!r}); waiting")
                raise RateLimited(f"{self.connector}: provider firewall throttled requests; paused until {until.isoformat()}")
            if self.conn_policy.credential == "none":
                self._stop(f"unexpected authentication requirement (HTTP {status}) on an anonymous route")
            if status == 401:
                raise AuthRejected(f"{self.connector}: credential rejected (HTTP 401)")
            self._stop(f"HTTP 403 for an approved credential — possible entitlement change; review before re-enabling")
        if status == 304:
            return FetchResult(self.connector, self._r(orig_url), self._r(final_url), status, ctype, dict(resp.headers),
                               utcnow(), not_modified=True, redirects=redirects)
        if status >= 500:
            raise TransientHTTPError(f"HTTP {status} from {route.host}")
        if status >= 400:
            body, _, _ = self._read_limited(resp, 1_000_000, None)
            marker = self._scan_markers(body)
            if marker:
                self._stop(f"HTTP {status} with billing/entitlement wording ({marker!r})")
            detail = ""
            try:  # surface a provider's short JSON error text (e.g. OAuth "Invalid user credentials"); never echo secrets
                import json as _json
                j = _json.loads(body[:20_000])
                detail = str(j.get("error_description") or j.get("error") or "")[:160] if isinstance(j, dict) else ""
            except ValueError:
                pass
            raise ContentValidationError(f"HTTP {status} from {route.host}" + (f": {self._r(detail)}" if detail else ""))
        # ---- 2xx -----------------------------------------------------------------------
        allowed = expect_content or route.content_types
        if allowed and ctype not in allowed:
            body, _, _ = self._read_limited(resp, 2_000_000, None)
            marker = self._scan_markers(body)
            if marker:
                self._stop(f"unexpected {ctype or 'untyped'} page with billing/entitlement wording ({marker!r})")
            raise ContentValidationError(f"unexpected content type {ctype!r} from {route.host} (expected {list(allowed)})")
        content, size, sha = self._read_limited(resp, limit, stream_to)
        if self.conn_policy.scan_body and stream_to is None and ctype in ("text/html", "text/plain"):
            marker = self._scan_markers(content)
            if marker:
                self._stop(f"response contains billing/entitlement wording ({marker!r})")
        return FetchResult(
            connector=self.connector,
            url=self._r(orig_url),
            final_url=self._r(final_url),
            status=status,
            content_type=ctype,
            headers={k.lower(): v for k, v in resp.headers.items() if k.lower() in ("etag", "last-modified", "date", "content-length", "content-type", "content-disposition")},
            retrieved_at=utcnow(),
            content=content,
            path=stream_to,
            sha256=sha,
            size=size,
            redirects=redirects,
        )


class _RateLimitSignal(Exception):
    def __init__(self, wait: float):
        self.wait = wait
