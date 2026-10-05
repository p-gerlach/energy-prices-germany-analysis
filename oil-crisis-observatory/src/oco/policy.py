"""Zero-charge access policy: load config/access_policy.yaml and decide allow/deny per request.

Deny by default. A request is allowed only when (scheme, host, port, method, path) matches a route
declared for the *calling connector*. There is intentionally no override flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit

import yaml

from .settings import CONFIG_DIR

REQUIRED_CONNECTOR_KEYS = (
    "title",
    "credential",
    "zero_charge_evidence",
    "checked_at",
    "trial_expiry",
    "payment_required",
    "processing_credit_dependence",
    "licence",
    "attribution",
    "verification_status",
    "routes",
)
VERIFICATION_STATES = {
    "documented_free",
    "anonymous_read_tested",
    "authenticated_download_tested",
    "unconfigured",
    "unavailable",
    "excluded",
}


class PolicyError(Exception):
    """The policy file itself is invalid (fails closed)."""


class PolicyDenied(Exception):
    """A request was blocked before it was sent."""


@dataclass(frozen=True)
class Route:
    host: str
    methods: tuple[str, ...]
    path_regex: re.Pattern
    content_types: tuple[str, ...]
    port: int | None = None
    sends_credentials: bool = False
    max_response_bytes: int | None = None


@dataclass
class Connector:
    key: str
    meta: dict
    routes: list[Route] = field(default_factory=list)

    @property
    def credential(self) -> str:
        return self.meta["credential"]

    @property
    def scan_body(self) -> bool:
        return self.meta.get("scan_body_for_stop_markers", True)


@dataclass
class Policy:
    raw: dict
    connectors: dict[str, Connector]
    excluded: dict[str, str]

    @property
    def stop_markers(self) -> list[str]:
        return [m.lower() for m in self.raw["global"]["stop_markers"]]

    @property
    def stop_statuses(self) -> set[int]:
        return set(self.raw["global"]["stop_statuses"])

    @property
    def forbidden_env_prefixes(self) -> list[str]:
        return list(self.raw["global"]["forbidden_env_prefixes"])

    @property
    def forbidden_query_params(self) -> set[str]:
        return {p.lower() for p in self.raw["global"]["forbidden_query_params"]}

    @property
    def max_response_bytes(self) -> int:
        return int(self.raw["global"]["max_response_bytes"])

    def connector(self, key: str) -> Connector:
        if key not in self.connectors:
            raise PolicyDenied(f"connector '{key}' is not declared in access_policy.yaml (default deny)")
        return self.connectors[key]

    def check(self, connector_key: str, method: str, url: str) -> Route:
        """Return the matching route or raise PolicyDenied. Never consults anything but the file."""
        conn = self.connector(connector_key)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        scheme = parts.scheme.lower()
        method = method.upper()
        if host in self.excluded:
            raise PolicyDenied(f"{host} is explicitly excluded: {self.excluded[host]}")
        if parts.username or parts.password:
            raise PolicyDenied("credentials embedded in URL are not allowed")
        loopback_ok = conn.meta.get("allow_http_loopback") and host in ("127.0.0.1", "localhost")
        if scheme != "https" and not (scheme == "http" and loopback_ok):
            raise PolicyDenied(f"scheme {scheme!r} not allowed for {connector_key}")
        for qk, _ in parse_qsl(parts.query, keep_blank_values=True):
            if qk.lower() in self.forbidden_query_params:
                raise PolicyDenied(f"query parameter {qk!r} is forbidden (e.g. ArcGIS user tokens)")
        path = parts.path or "/"
        for r in conn.routes:
            if r.host != host:
                continue
            if r.port is not None and parts.port != r.port:
                continue
            if r.port is None and parts.port not in (None, 443):
                continue
            if method not in r.methods:
                continue
            if r.path_regex.match(path):
                return r
        raise PolicyDenied(
            f"{method} {scheme}://{host}{_redact_path(path)} is not an approved route for connector '{connector_key}'"
        )


def _redact_path(path: str) -> str:
    # FIRMS puts the key in the path; never echo long alphanumeric tokens in errors.
    return re.sub(r"/[A-Za-z0-9]{24,}(?=/|$)", "/<redacted>", path)


def _build(raw: dict) -> Policy:
    if raw.get("default") != "deny":
        raise PolicyError("access policy must declare default: deny")
    if "allow_paid" in str(raw).lower():
        raise PolicyError("an allow_paid switch is not permitted in the access policy")
    connectors: dict[str, Connector] = {}
    for key, meta in (raw.get("connectors") or {}).items():
        missing = [k for k in REQUIRED_CONNECTOR_KEYS if k not in meta]
        if missing:
            raise PolicyError(f"connector {key} missing keys: {missing}")
        if meta["payment_required"] is not False:
            raise PolicyError(f"connector {key}: payment_required must be false")
        if meta["processing_credit_dependence"] is not False:
            raise PolicyError(f"connector {key}: processing_credit_dependence must be false")
        if str(meta["trial_expiry"]).lower() != "none":
            raise PolicyError(f"connector {key}: trial_expiry must be none")
        if meta["verification_status"] not in VERIFICATION_STATES:
            raise PolicyError(f"connector {key}: bad verification_status")
        routes = []
        for r in meta["routes"]:
            routes.append(
                Route(
                    host=r["host"].lower(),
                    methods=tuple(m.upper() for m in r["methods"]),
                    path_regex=re.compile(r["path_regex"]),
                    content_types=tuple(c.lower() for c in r.get("content_types", [])),
                    port=r.get("port"),
                    sends_credentials=bool(r.get("sends_credentials", False)),
                    max_response_bytes=r.get("max_response_bytes"),
                )
            )
        connectors[key] = Connector(key=key, meta=meta, routes=routes)
    excluded = {e["host"].lower(): e["reason"] for e in raw.get("excluded", [])}
    for c in connectors.values():
        for r in c.routes:
            if r.host in excluded:
                raise PolicyError(f"connector {c.key} routes to excluded host {r.host}")
    return Policy(raw=raw, connectors=connectors, excluded=excluded)


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> Policy:
    p = path or str(CONFIG_DIR / "access_policy.yaml")
    with open(p, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return _build(raw)


def policy_from_dict(raw: dict) -> Policy:
    return _build(raw)
